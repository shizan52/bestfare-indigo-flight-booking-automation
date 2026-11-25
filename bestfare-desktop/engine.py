"""The booking engine of the desktop app: ONE background thread with its own asyncio loop, so the window never freezes.

The window talks to it with plain calls (submit, stop, request_fares, ...) and hears back through `emit(event)` - a callback the
window turns into a Qt signal. Events are small dicts, all in the customer's words (nothing about how the work is done):

  {"type": "started",  "id": booking id, "grp": group id, "offer": "offer1|offer2"}
  {"type": "stage",    "id": ..., "stage": one of STAGES}          (that stage is now running; earlier ones are done)
  {"type": "note",     "id": ..., "text": str}                      ("Your flight: ...", "Offer applied ...", "Details checked")
  {"type": "choose",   "id": ..., "grp": ..., "flights": [flights.public(option)], "seconds": CHOICE_WAIT_S}
                                                                    (the flights found, each with its lowest fare: the traveller picks one)
  {"type": "chosen",   "id": ..., "flight": option}                 (the pick was made - or {"flight": None} when none was chosen in time)
  {"type": "finished", "id": ..., "grp": ..., "record": the saved booking}
  {"type": "fares",    "origin","dest","start","days","fares": {iso: [price, L|M|H]}, "error": None|"busy"|"unavailable", "empty": bool}
  {"type": "log",      "level", "text", "t"}                        (debug mode only: the technical story)

Rules carried over from the live server (they are why it works):
  * one booking at a time (an IndiGo session lives in the browser's cookies) - the rest wait in a queue;
  * a booking that hangs is stopped after 7 minutes; a browser that breaks is repaired and the booking tried once more.

What is NOT carried over: the lock-out timer ("IndiGo is busy, wait 15 minutes"). When IndiGo will not take the start of a booking, the engine handles it by
itself, calmly (see RECOVERY): the page keeps moving like a person for a while and asks again; then a fresh browser after a short rest; then a longer rest.
The traveller sees one friendly line, never a countdown, and the app is never locked: after the last try the next press of Book simply starts again.

The booking browser is never started when the app opens: only when a fare calendar or a booking needs it, it works out of sight
(its window lives off-screen, a guard hides any pop-up), and it closes again a few seconds after the work is done.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Awaitable, Callable, Optional

import paths

paths.use_engine()
import aiohttp  # noqa: E402
import speed  # noqa: E402
from cdp import CDPBrowser, CDPError  # noqa: E402
from human import HumanInput  # noqa: E402
from indigo_client import (BookingRequest, BookingResult, IndigoBlocked, IndigoError, Pace,  # noqa: E402
                           Passenger, open_session, pick_trusted_tab, run_booking)
from psp_flow import run_psp  # noqa: E402

import browser as bmod  # noqa: E402
import fares  # noqa: E402
import flights  # noqa: E402
import forms  # noqa: E402
import gate as gatemod  # noqa: E402
import verify  # noqa: E402
from extension import ExtensionSession  # noqa: E402
from store import Store  # noqa: E402

log = logging.getLogger("engine")

STAGES = ("browser", "warmup", "search", "hold", "travellers", "pnr")
STAGE_TEXT = {"browser": "Starting your booking", "warmup": "Getting ready", "search": "Finding your flights",
              "hold": "Applying your offer", "travellers": "Adding the travellers", "pnr": "Confirming your booking"}
BOOKING_TIMEOUT_S = 420              # the working time of one booking (the time the traveller spends choosing a flight is not counted)
CHOICE_WAIT_S = 300                  # how long the list of flights waits for the traveller: the fares on it stay valid for a few minutes
OLD_LOCKOUT_KEY = "pace"             # the lock-out timer older versions kept in the store: removed at every start, nothing reads it any more
FARE_FRESH_S = 600
FARES_OFF_S = 90                     # after the fare calendar was refused it rests this long (only the calendar: a booking never waits for it)
IDLE_CLOSE_S = 90.0                  # the browser closes this long after the last fare request
AFTER_BOOKING_CLOSE_S = 0.0          # ... and at once when a booking is done (the PNR is there): no browser stays behind, every booking starts in a fresh one
IDLE_POLL_S = 1.0                    # how often the browser's idle timer is looked at
WARM_NEEDED = (22.0, 38.0)           # the natural activity a cold page needs before its first call (the shared engine's own numbers)
WARM_TARGET_S = 36.0                 # while the traveller fills the form, the page is kept alive until it has this much
WARM_CHUNK_S = 4.0                   # ... in short pieces, so a booking never waits long for the browser
WARM_FRESH_S = 150.0                 # activity older than this no longer counts as warm-up
WARM_POLL_S = 1.0
KEEP_OPEN_MAX_S = 480.0              # a browser opened for the fare calendar is kept for the traveller filling the form, this long at most
SLOW_NOTE = "The internet connection looks slow — this may take a little longer."

# ── when IndiGo will not take the START of a booking (nothing is chosen or held yet): the engine deals with it by itself ──────────────────────────────
# There is no "wait 15 minutes" and no lock: the booking simply goes on, with one calm line on the screen. Each step is (a fresh browser?, the rest before it in
# seconds, the natural activity the page has before its first call in seconds). Every step is gentler than a person pressing the button again: IndiGo is
# never asked in a hurry, and never more than a handful of times.
RECOVERY = (
    (False, (2.0, 4.0), (26.0, 40.0)),         # 1) the same page keeps moving like a person for a good while, then asks again
    (True, (25.0, 40.0), (32.0, 46.0)),        # 2) the browser is closed, left alone for a moment and opened again, a longer warm-up
    (True, (95.0, 140.0), (38.0, 54.0)),       # 3) a longer rest, a fresh start again
)
RATE_LIMIT_REST_FACTOR = 2.0                   # an explicit "slow down" (HTTP 429) is given rests twice as long
NET_PROBE_HOST = "www.goindigo.in"             # "does this computer reach IndiGo at all?" - a plain connection to its front door, no request is made
NET_WAIT_S = 120.0                             # no internet at all: wait for it this long (that is not IndiGo's doing; nothing is spent on it)
NET_POLL_S = 4.0
NET_WAITS = 3
CAUTION_S = 20 * 60                            # after a booking IndiGo would not take, the next ones start more patiently for this long
WARM_CAUTIOUS = (30.0, 46.0)                   # ... and so does a computer IndiGo has never seen succeed (a new installation): the first search is the delicate one
SEASONED_KEY = "seasoned"                      # set once a search was answered: this installation is known to IndiGo
EARLY = STAGES.index("search")                 # a refusal up to here means nothing is chosen or held: starting over costs nothing
CALM_NOTE_FIRST = "IndiGo is taking a little longer than usual — trying again for you."
CALM_NOTE_AGAIN = "Still working on it — IndiGo is slow to answer right now. You don't need to do anything."

Emit = Callable[[dict], None]
Runner = Callable[[str, BookingRequest, str], Awaitable[BookingResult]]


class BrowserGone(Exception):
    pass


@dataclass
class Refusal:
    """IndiGo would not take a try: how (kind: fetch / 429 / 401 / 403 / 5xx / denied / session), in which words, and whether nothing is chosen or held yet."""
    kind: str
    text: str = ""
    early: bool = True
    where: str = ""                                  # the call that was refused (token / search / sell ...), for the short note the server keeps


def request_from(rec: dict) -> BookingRequest:
    day = datetime.strptime(rec["travel_date"], "%Y-%m-%d").strftime("%d-%m-%Y")
    return BookingRequest(origin=rec["origin"], destination=rec["dest"], date=day,
                          passengers=[Passenger(first_name=t["first"], last_name=t["last"], gender=t["gender"],
                                                kind=t.get("kind") or "adult", dob=t.get("dob") or "")
                                      for t in rec["travellers"]],
                          offer=rec["offer"], contact_email=rec["email"], contact_phone=rec["phone"])


def _proxy_in_use() -> bool:
    """Is this computer set up to reach the internet through a proxy (environment, or Windows' own settings - what the browser follows)?
    (Only modules the frozen app already has are used here: a release must never need a module the installed shell does not contain.)"""
    import os
    if any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")):
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            try:
                if winreg.QueryValueEx(k, "ProxyEnable")[0]:
                    return True
            except OSError:
                pass
            try:
                return bool(winreg.QueryValueEx(k, "AutoConfigURL")[0])
            except OSError:
                return False
    except (ImportError, OSError):
        return False


def friendly(err: str) -> str:
    """Why a booking failed, in the traveller's words. Nothing about how the work is done; technical detail stays out of sight. Never a timer, never "wait":
    by the time a booking ends like this the engine has already tried again by itself, and pressing Book starts afresh at once."""
    e = (err or "").lower()
    if err.startswith("!"):                                   # already written for people (BrowserProblem)
        return err[1:]
    if "stopped by you" in e:
        return "You stopped this booking."
    if "no flights" in e:
        return "IndiGo has no seats on this route and date. Please try another date."
    if "no flight was chosen" in e:
        return "No flight was chosen, so nothing was booked. Please try again and pick a flight."
    if "server error" in e or "could not start a session" in e:
        return "IndiGo is not responding at the moment, so this booking could not be completed. Nothing was booked or charged — please press Book again."
    if "cooling down" in e or "refused" in e or "429" in e or "blocked" in e or "session not accepted" in e:
        return "IndiGo is not responding well right now, so this booking could not be completed. Nothing was booked or charged — please press Book again."
    if "timed out" in e:
        return "This is taking longer than usual. Please try again."
    if "fare could not be held" in e or "hold returned" in e:
        return "That fare is no longer available — it may have just sold out. Please try again or pick another date."
    if "passenger details rejected" in e:
        return "IndiGo did not accept the traveller details. Please check the names and try again."
    if "browser" in e:
        return "Something went wrong. Please try again."
    return "The booking could not be completed. Please try again."


# ── the technical story, for debug mode only ─────────────────────────────────────────────────────────────────────
_API_RE = re.compile(r"^indigo (\S+)\s+-> (\S+) \((\d+) ms\)")
_API_WORDS = {"token": "Started a session with IndiGo", "search": "Searched IndiGo's flights", "sell": "Held the fare",
              "addpax": "Added the travellers", "itinerary": "Read the booking from IndiGo", "pay-init": "Created the payment order",
              "calendar": "Loaded the fare calendar"}
_COVERED = ("cheapest ", "coupon ", "PNR ")          # already told in plain words by the progress events


SECRET_CODES: set[str] = set()          # the promo codes of the bookings that are running (never written anywhere)


def activity_line(name: str, msg: str, levelno: int) -> Optional[tuple[str, str]]:
    """One log record of the shared engine -> (level, words) for the DEBUG Activity page, or None to skip it.
    Offer codes never reach the screen, not even in debug."""
    level = "error" if levelno >= 40 else "warn" if levelno >= 30 else "info"
    m = (msg or "").strip()
    if not m:
        return None
    text = m
    if name == "indigo_client":
        r = _API_RE.match(m)
        if r:
            step, status, ms = r.groups()
            word = _API_WORDS.get(step, step)
            if status.startswith("BLOCKED"):
                level, text = "error", f"{word}: IndiGo refused the request ({ms} ms) — it wants us to slow down"
            elif status in ("200", "201"):
                level, text = "info", f"{word} — OK ({ms} ms)"
            else:
                level, text = "warn", f"{word} — IndiGo answered with code {status} ({ms} ms)"
        elif m.startswith(_COVERED):
            return None
    elif name == "engine" and m.startswith("booking "):
        if " done:" in m:
            return None
        if " failed: " in m:
            level, text = "info", "Technical detail: " + m.split(" failed: ", 1)[1]
    elif name == "psp_flow":
        text = "Payment page: " + m
    for code in SECRET_CODES:
        text = text.replace(code, "the offer code")
    return level, text[:240]


class _ActivityLog(logging.Handler):
    """DEBUG mode only: every line the engine logs, as words for the Activity page."""

    def __init__(self, engine: "Engine"):
        super().__init__(level=logging.INFO)
        self.engine = engine

    def emit(self, record: logging.LogRecord) -> None:
        try:
            res = activity_line(record.name, record.getMessage(), record.levelno)
        except Exception:  # noqa: BLE001
            return
        if res:
            self.engine.emit({"type": "log", "level": res[0], "text": res[1], "t": record.created})


class _Progress(logging.Handler):
    """Turns the shared engine's log lines into the progress shown on screen (no change to the engine needed). What it says to the
    customer is only what they care about: the stage, the best fare, the offer."""

    def __init__(self, engine: "Engine"):
        super().__init__(level=logging.INFO)
        self.engine = engine

    def emit(self, record: logging.LogRecord) -> None:
        bid = self.engine.current
        if not bid or record.name != "indigo_client":
            return
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        en = self.engine
        if msg.startswith("indigo token") and "-> 200" in msg:
            en._stage(bid, "search")
        elif msg.startswith("indigo search") and "-> 200" in msg:
            en._mark_seasoned()                     # IndiGo answered the delicate first search: this installation is known to it now
            if not en.expect_choice:                # (with a flight list, "Applying your offer" starts once the traveller has chosen)
                en._stage(bid, "hold")
        elif msg.startswith("cheapest"):
            amount = msg.rsplit("Rs", 1)[-1].strip()
            stops = "non-stop" if "stops=0" in msg else "with a stop"
            en._note(bid, f"Best fare found: {forms.money(amount)} ({stops})")
        elif msg.startswith("indigo sell") and "-> 200" in msg:
            en._stage(bid, "travellers")
        elif msg.startswith("infant fee"):
            try:
                fee, total = (x.strip() for x in msg.replace("infant fee Rs", "").split(" total Rs"))
                en._note(bid, f"The infant fee {forms.money(fee)} is added to the fare you chose — the total is {forms.money(total)}.")
            except ValueError:
                pass
        elif msg.startswith("coupon"):
            if "applied=True" in msg:
                saved = msg.rsplit("discount Rs", 1)[-1].rstrip(")").strip() if "discount Rs" in msg else ""
                try:
                    en._note(bid, "Offer applied" + (f" — you save {forms.money(saved)}" if float(saved) > 0 else ""))
                except ValueError:
                    en._note(bid, "Offer applied")
            elif "applied=False" in msg:
                en._note(bid, "IndiGo did not apply the offer to this flight — it will be the regular fare.")
        elif msg.startswith("indigo addpax") and "-> 200" in msg:
            en._stage(bid, "pnr")
        if msg.startswith("indigo ") and speed.Net.slow() and bid not in en._slow_noted:
            en._slow_noted.add(bid)
            en._note(bid, SLOW_NOTE)


class Engine:
    def __init__(self, store: Store, emit: Emit, edge: Optional[bmod.LocalEdge] = None, runner: Optional[Runner] = None,
                 debug: bool = False, idle_close_s: float = IDLE_CLOSE_S, after_booking_close_s: float = AFTER_BOOKING_CLOSE_S,
                 gate=None):
        self.store = store
        self.gate = gate or gatemod.DevGate()                # who may book (see gate.py): with a licence, the SERVER decides
        self._passes: dict[str, gatemod.Pass] = {}
        self._emit = emit
        self.edge = edge or bmod.LocalEdge()
        self._runner: Runner = runner or self._book_once
        self.debug = debug                                  # debug: visible browser window + the technical Activity
        self.idle_close_s = idle_close_s
        self.after_booking_close_s = after_booking_close_s
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.current: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._queue: Optional[asyncio.Queue] = None
        self._lock: Optional[asyncio.Lock] = None           # the browser is used by one thing at a time
        self._waiting: list[str] = []
        self._ready = threading.Event()
        self._close_at = 0.0
        self._warmed = False
        self.expect_choice = False                          # a real booking shows the flight list: the offer step comes after the pick
        self.keep_warm = True                               # keep the page alive while the traveller fills the form (see _keep_warm_loop)
        self._warm_launch = -1                              # the browser launch the warm-up credit belongs to
        self._warm_credit = 0.0                             # seconds of natural activity the page has had
        self._warm_at = 0.0
        self._opened_at = 0.0
        self._slow_noted: set[str] = set()
        self._fares_tasks: set = set()
        self._reached = -1                                  # the furthest booking stage (index in STAGES) of the try that is running
        self._diag: dict[str, list[str]] = {}               # per booking: what IndiGo refused (kind@call), for the short note the server keeps
        self._warm_override: Optional[tuple[float, float]] = None      # the warm-up the next try must use (set by the calm recovery)
        self._cautious_until = 0.0                          # until when a booking starts patiently (after one IndiGo would not take)
        self._seasoned = self._was_ever_used()              # has this installation ever had a search answered by IndiGo?
        Pace.lockout = False                                # no lock-out in this app: a refusal is handled by the booking itself (see RECOVERY)
        self._stage_times: list[tuple[str, float]] = []
        self._choice: Optional[tuple[str, asyncio.Future, set]] = None      # (booking id, the answer to wait for, the keys on offer)
        self._paused_s = 0.0                                # time spent waiting for the traveller's choice (not part of the work time)
        self._pause_since: Optional[float] = None
        self._fare_cache: dict[tuple, tuple[float, fares.FareResult]] = {}
        self._fares_off_until = 0.0
        self._progress = _Progress(self)
        self._activity = _ActivityLog(self)

    # ── life cycle ───────────────────────────────────────────────────────────────────────────────────
    def start(self) -> None:
        n = self.store.interrupted()
        if n:
            log.warning("%d booking(s) were interrupted by the last close", n)
        self._forget_old_lockout()
        threading.Thread(target=self._report_unreported, name="gate-reconcile", daemon=True).start()
        for name in ("indigo_client", "psp_flow", "engine", "browser", "extension"):
            lg = logging.getLogger(name)
            if name == "indigo_client":
                lg.addHandler(self._progress)
            if self.debug:
                lg.addHandler(self._activity)
            lg.setLevel(logging.INFO)
        self.thread = threading.Thread(target=self._thread_main, name="booking-engine", daemon=True)
        self.thread.start()
        self._ready.wait(10)

    def _thread_main(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._queue = asyncio.Queue()
        self._lock = asyncio.Lock()
        self.loop.create_task(self._worker())
        self.loop.create_task(self._idle_loop())
        self.loop.create_task(self._keep_warm_loop())
        self.loop.create_task(self._wipe_loop())
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            for t in asyncio.all_tasks(self.loop):
                t.cancel()
            try:
                self.loop.run_until_complete(asyncio.sleep(0.05))
            except Exception:  # noqa: BLE001
                pass
            self.loop.close()

    def call(self, coro) -> concurrent.futures.Future:
        assert self.loop is not None, "engine not started"
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def shutdown(self, close_browser: bool = True) -> None:
        if not self.loop or not self.thread:
            return
        if self._task and not self._task.done():
            self.loop.call_soon_threadsafe(self._task.cancel)
        if close_browser:
            try:
                self.call(self.edge.close()).result(10)
            except Exception:  # noqa: BLE001
                log.warning("could not close the browser", exc_info=True)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(8)
        for name in ("indigo_client", "psp_flow", "engine", "browser", "extension"):
            logging.getLogger(name).removeHandler(self._progress)
            logging.getLogger(name).removeHandler(self._activity)

    # ── events ───────────────────────────────────────────────────────────────────────────────────────
    def emit(self, ev: dict) -> None:
        try:
            self._emit(ev)
        except Exception:  # noqa: BLE001
            log.exception("event handler failed")

    def _stage(self, bid: str, stage: str) -> None:
        self._stage_times.append((stage, time.monotonic()))
        if stage in STAGES:
            self._reached = max(self._reached, STAGES.index(stage))
        self.emit({"type": "stage", "id": bid, "stage": stage})

    def _note(self, bid: str, text: str) -> None:
        self.emit({"type": "note", "id": bid, "text": text})

    # ── no lock-out; and what a new installation needs ────────────────────────────────────────────────
    def _forget_old_lockout(self) -> None:
        """Older versions kept a lock-out timer in the store and read it back at every start (its 'blocks in a row' counter was never cleared by a success, so
        single refusals grew into hours). There is none any more - and a leftover one must not keep anybody waiting after the update."""
        Pace.lockout, Pace.cooldown_until, Pace.strikes, Pace.on_trip = False, 0.0, 0, None
        try:
            if self.store.setting(OLD_LOCKOUT_KEY) is not None:
                self.store.set_setting(OLD_LOCKOUT_KEY, None)
                log.info("removed the old lock-out timer")
        except Exception:  # noqa: BLE001
            log.debug("could not remove the old lock-out timer", exc_info=True)

    def _was_ever_used(self) -> bool:
        """Has this installation had a booking go through (or a search answered by IndiGo)? An installation IndiGo has never seen succeed is the one whose first
        search is refused most often (a new computer, a new browser profile): it starts more patiently until it has."""
        try:
            if self.store.setting(SEASONED_KEY):
                return True
            return any(r.get("status") == "done" for r in self.store.bookings(200))
        except Exception:  # noqa: BLE001 - when unsure, nobody is slowed down
            return True

    def _mark_seasoned(self) -> None:
        """IndiGo answered a search: this installation is known to it, and the patient mode after a refusal is over."""
        self._cautious_until = 0.0
        if self._seasoned:
            return
        self._seasoned = True
        try:
            self.store.set_setting(SEASONED_KEY, {"at": time.time()})
        except Exception:  # noqa: BLE001
            log.debug("could not save the seasoned mark", exc_info=True)

    # ── commands from the window (any thread) ─────────────────────────────────────────────────────────
    def submit(self, order: forms.Order) -> tuple[str, list[str]]:
        """Save the booking of a checked order and queue it. Returns (group id, [booking id])."""
        assert self.loop is not None
        grp = uuid.uuid4().hex[:8]
        travellers = order.records()
        assert order.day is not None
        ids = [self.store.add_booking(grp=grp, origin=order.origin, dest=order.dest, travel_date=order.day.isoformat(),
                                      offer=offer, travellers=travellers, phone=order.phone, email=order.email)
               for offer in order.bookings]
        for bid in ids:
            self.loop.call_soon_threadsafe(self._enqueue, bid)
        return grp, ids

    def _enqueue(self, bid: str) -> None:
        assert self._queue is not None
        self._waiting.append(bid)
        self._queue.put_nowait(bid)

    def stop(self, bid: str) -> None:
        """Stop a running booking, or drop one that is still waiting."""
        assert self.loop is not None

        def _do() -> None:
            if self.current == bid and self._task and not self._task.done():
                self._task.cancel()
            elif bid in self._waiting:
                self._waiting.remove(bid)
                self.store.update(bid, status="failed", error="You stopped this booking.", finished_at=time.time())
                self.emit({"type": "finished", "id": bid, "grp": (self.store.get(bid) or {}).get("grp"),
                           "record": self.store.get(bid)})
        self.loop.call_soon_threadsafe(_do)

    def choose_flight(self, bid: str, key: str) -> None:
        """The traveller picked a flight from the list that was shown for booking `bid`."""
        assert self.loop is not None
        self.loop.call_soon_threadsafe(self._resolve_choice, bid, key)

    def _resolve_choice(self, bid: str, key: str) -> None:
        if self._choice and self._choice[0] == bid and key in self._choice[2] and not self._choice[1].done():
            self._choice[1].set_result(key)

    async def ask_flight(self, bid: str, options: list[dict]) -> Optional[str]:
        """Show the flights to the traveller and wait for the pick. Returns the chosen flight's key - or None when none was chosen
        in time (nothing is held then). Stopping the booking cancels the wait."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._choice = (bid, fut, {o["key"] for o in options})
        self._pause_since = time.monotonic()
        rec = self.store.get(bid) or {}
        self.emit({"type": "choose", "id": bid, "grp": rec.get("grp"), "flights": [flights.public(o) for o in options],
                   "seconds": CHOICE_WAIT_S})
        key: Optional[str] = None
        try:
            key = await asyncio.wait_for(fut, CHOICE_WAIT_S)
        except asyncio.TimeoutError:
            self._note(bid, "No flight was chosen in time.")
        finally:
            self._choice = None
            if self._pause_since is not None:
                self._paused_s += time.monotonic() - self._pause_since
                self._pause_since = None
        picked = next((o for o in options if o["key"] == key), None) if key else None
        self.emit({"type": "chosen", "id": bid, "flight": flights.public(picked) if picked else None})
        if picked:
            self._stage(bid, "hold")                                       # the traveller chose: now the fare is held with the offer
            save = flights.saving(picked)
            self._note(bid, f"Your flight: {flights.line(picked)} · {flights.money(picked)}")
            if save > 0:
                self._note(bid, f"This fare includes your offer — you save {forms.money(save)}")
        return key

    async def _within_work_time(self, coro, seconds: float):
        """Like asyncio.wait_for, but the time the traveller spends choosing a flight does not count against `seconds`."""
        task = asyncio.ensure_future(coro)
        started = time.monotonic()
        self._paused_s, self._pause_since = 0.0, None
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.25)
                if done:
                    return task.result()
                now = time.monotonic()
                paused = self._paused_s + (now - self._pause_since if self._pause_since is not None else 0.0)
                if now - started - paused >= seconds * max(1.0, min(2.0, speed.Net.scale())):     # (a slow line gets up to twice as long)
                    raise asyncio.TimeoutError()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def request_fares(self, origin: str, dest: str, start: date, days: int = fares.WINDOW_DAYS) -> None:
        """Ask for the lowest fare of each day (for the date calendar). The answer comes back as a "fares" event."""
        if self.loop is not None:
            fut = self.call(self._fares_job(origin, dest, start, days))
            self._fares_tasks.add(fut)
            fut.add_done_callback(self._fares_tasks.discard)

    @property
    def busy(self) -> bool:
        return bool(self.current) or bool(self._waiting)

    # ── the booking browser: only when needed, out of sight, closed soon after ────────────────────────
    def _touch(self, seconds: float) -> None:
        self._close_at = time.monotonic() + seconds

    async def _browser(self, note: Optional[Callable[[str], None]] = None) -> str:
        before = getattr(self.edge, "launches", 0)
        ep = await self.edge.ensure(note=note, quiet=not self.debug)
        if getattr(self.edge, "launches", 0) != before:
            self._warmed = False                               # a fresh page: warm it up like a person before the first call
            self._opened_at = time.monotonic()
        elif not self._opened_at:
            self._opened_at = time.monotonic()
        self._touch(self.idle_close_s)
        return ep

    def release_browser(self) -> None:
        """Close the booking browser now (unless a booking needs it): used when the window is locked - a stopped key keeps nothing running."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._touch, 0.0)

    def keep_open(self, seconds: float = 90.0) -> None:
        """The traveller is busy with the form while the browser (opened for the fare calendar) waits: keep it a little longer, so
        it is still there - and warm - when Book now is pressed. Never starts a browser; ends a few minutes after it was opened."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._extend_open, seconds)

    def _extend_open(self, seconds: float) -> None:
        if not self._close_at:
            return
        now = time.monotonic()
        limit = max(self._close_at, (self._opened_at or now) + KEEP_OPEN_MAX_S)
        self._close_at = min(max(self._close_at, now + seconds), limit)

    # ── warm-up: the page needs ~30 s of natural activity before its first call. Done while the traveller types, not after ──
    def _add_warm(self, seconds: float) -> None:
        launch = getattr(self.edge, "launches", 0)
        if launch != self._warm_launch:
            self._warm_launch, self._warm_credit = launch, 0.0
        self._warm_credit += seconds
        self._warm_at = time.monotonic()

    def warm_credit(self) -> float:
        """Seconds of natural activity the CURRENT page has had recently."""
        if getattr(self.edge, "launches", 0) != self._warm_launch or time.monotonic() - self._warm_at > WARM_FRESH_S:
            return 0.0
        return self._warm_credit

    def warmup_for(self) -> Optional[tuple[float, float]]:
        """What is still missing of the natural activity before the first call: the full range for a cold page, a few seconds for
        one that has been kept alive (the total activity is the same - it just happened earlier, while the traveller typed)."""
        credit = self.warm_credit()
        if self._patient():                                    # (a new installation, or IndiGo would not take the last booking: more activity before the first call)
            lo, hi = WARM_CAUTIOUS
            if credit < 8.0:
                return (lo, hi)
            if credit >= hi:
                return (3.0, 6.0)
            return (max(3.0, lo - credit), max(6.0, hi - credit))
        if credit < 8.0:
            return None
        lo, hi = WARM_NEEDED
        if credit >= lo:
            return (1.0, 2.5)                                  # (the page has had all the activity it needs: only a moment more, so the first call is not a jump)
        return (max(2.0, lo - credit), max(4.0, hi - credit))

    def _patient(self) -> bool:
        """Should this booking start more patiently (a longer warm-up before IndiGo is asked anything)?"""
        return (not self._seasoned) or time.monotonic() < self._cautious_until

    def _warm_target(self) -> float:
        return WARM_CAUTIOUS[1] if self._patient() else WARM_TARGET_S

    def _should_warm(self) -> bool:
        return bool(self.keep_warm and self._close_at and time.monotonic() < self._close_at and not self.current and not self._waiting
                    and getattr(self.edge, "endpoint", None) and self._lock is not None and not self._lock.locked()
                    and self.warm_credit() < self._warm_target())

    async def _warm_chunk(self) -> None:
        ep = self.edge.endpoint
        async with CDPBrowser(ep) as b:
            info = pick_trusted_tab(await b.pages())
            if info is None:
                return
            tab = await b.open_tab(info)
            try:
                w, h = await tab.viewport()
                await HumanInput(tab, w, h).wander(WARM_CHUNK_S)
            finally:
                await tab.close()
        self._add_warm(WARM_CHUNK_S)

    async def _keep_warm_loop(self) -> None:
        assert self._lock is not None
        while True:
            await asyncio.sleep(WARM_POLL_S)
            try:
                if not self._should_warm():
                    continue
                async with self._lock:
                    if self._should_warm_locked():
                        await self._warm_chunk()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - looking busy must never matter: a failed piece is simply skipped
                log.debug("keep-warm piece failed", exc_info=True)
                await asyncio.sleep(3.0)

    def _should_warm_locked(self) -> bool:
        """(the same check, once the browser's lock is ours: a booking may have started while we waited for it)"""
        return bool(self.keep_warm and self._close_at and not self.current and not self._waiting and getattr(self.edge, "endpoint", None))

    async def _idle_loop(self) -> None:
        assert self._lock is not None
        while True:
            await asyncio.sleep(IDLE_POLL_S)
            if self._close_at and time.monotonic() >= self._close_at and not self._lock.locked() and not self.current and not self._waiting:
                async with self._lock:
                    if self._close_at and time.monotonic() >= self._close_at:
                        self._close_at = 0.0
                        try:
                            await self.edge.close()
                        except Exception:  # noqa: BLE001
                            log.debug("could not close the browser", exc_info=True)

    async def _wipe_loop(self) -> None:
        """Personal details of finished bookings are wiped 2 hours after they finish."""
        while True:
            try:
                n = self.store.wipe_personal()
                if n:
                    log.info("wiped the personal details of %d finished booking(s)", n)
            except Exception:  # noqa: BLE001
                log.debug("wipe failed", exc_info=True)
            await asyncio.sleep(600)

    # ── the fare calendar ─────────────────────────────────────────────────────────────────────────────
    async def _fares_job(self, origin: str, dest: str, start: date, days: int) -> None:
        assert self._lock is not None
        key = (origin, dest, start.isoformat(), days)

        def send(r: fares.FareResult) -> None:
            self.emit({"type": "fares", "origin": origin, "dest": dest, "start": start.isoformat(), "days": days,
                       "fares": {d: [p, c] for d, (p, c) in r.fares.items()}, "empty": r.empty, "error": r.error})

        hit = self._fare_cache.get(key)
        if hit and time.monotonic() - hit[0] < FARE_FRESH_S:
            return send(hit[1])
        if self.current or time.monotonic() < self._fares_off_until:
            return send(fares.FareResult(error="busy"))
        async with self._lock:
            result = fares.FareResult(error="unavailable")
            try:
                ep = await self._browser()
                async with CDPBrowser(ep) as b:
                    info = pick_trusted_tab(await b.pages())
                    if info is None:
                        raise BrowserGone("no page")
                    tab = await b.open_tab(info)
                    try:
                        if not self._warmed:
                            w, h = await tab.viewport()
                            spent = random.uniform(18, 26) if self._patient() else random.uniform(6, 9)      # (a new installation's first contact is the delicate one)
                            await HumanInput(tab, w, h).wander(spent)
                            self._add_warm(spent)
                            self._warmed = True
                        await Pace.wait()
                        result = await fares.fetch(tab, origin, dest, start, days)
                    finally:
                        await tab.close()
            except IndigoBlocked:
                result = fares.FareResult(error="busy")
            except (bmod.BrowserProblem, BrowserGone, CDPError, OSError, asyncio.TimeoutError, aiohttp.ClientError):
                log.info("fares could not be loaded", exc_info=True)
                result = fares.FareResult(error="unavailable")
            self._touch(self.idle_close_s)
        if result.error == "busy":
            self._fares_off_until = time.monotonic() + FARES_OFF_S      # the calendar rests a moment (a booking never waits for it)
        elif not result.error:
            self._fare_cache[key] = (time.monotonic(), result)
        send(result)

    # ── the bookings ──────────────────────────────────────────────────────────────────────────────────
    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            bid = await self._queue.get()
            if bid not in self._waiting:
                continue                                          # stopped while waiting
            self._waiting.remove(bid)
            rec = self.store.get(bid)
            if not rec or rec["status"] != "waiting":
                continue
            self.current = bid
            self.store.update(bid, status="running")
            self.emit({"type": "started", "id": bid, "grp": rec["grp"], "offer": rec["offer"]})
            self._task = asyncio.ensure_future(self._process(rec))
            try:
                res = await self._task
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():          # the app is closing
                    raise
                res = BookingResult(success=False, error="stopped by you")
            except Exception as e:  # noqa: BLE001
                log.exception("booking %s crashed", bid)
                res = BookingResult(success=False, error=f"internal error: {type(e).__name__}")
            finally:
                self.current = None
                self._task = None
            self._save(bid, res)
            self._touch(self.after_booking_close_s)                 # done: the browser closes itself in a few seconds
            self.emit({"type": "finished", "id": bid, "grp": rec["grp"], "record": self.store.get(bid)})

    def _log_timing(self, bid: str) -> None:
        """Where the time of this booking went (debug story / log only)."""
        marks = self._stage_times
        if len(marks) < 2:
            return
        parts = []
        for i, (name, t0) in enumerate(marks):
            t1 = marks[i + 1][1] if i + 1 < len(marks) else time.monotonic()
            parts.append(f"{name} {t1 - t0:.0f}s")
        log.info("booking %s timing: %s (total %.0f s, without the time spent choosing %.0f s)", bid, ", ".join(parts),
                 time.monotonic() - marks[0][1], self._paused_s)

    def _save(self, bid: str, res: BookingResult) -> None:
        self._log_timing(bid)
        self._report(bid, res)
        if res.success and res.pnr:
            log.info("booking %s done: %s %s", bid, res.kind, res.pnr)
            self.store.update(bid, status="done", pnr=res.pnr, kind=res.kind or "order_id", fare=res.fare_amount,
                              discount=res.discount, coupon_applied=res.coupon_applied, hold_until=res.hold_until,
                              pay_url=res.pay_url, finished_at=time.time(), verified=getattr(res, "verified", None),
                              verify_note=getattr(res, "verify_note", None),
                              flight=flights.line(res.flight) if getattr(res, "flight", None) else None)
        else:
            log.warning("booking %s failed: %s", bid, res.error)
            self.store.update(bid, status="failed", error=friendly(res.error or ""),
                              fare=res.fare_amount, finished_at=time.time())

    # ── telling the server how it ended (credits are returned for a failure, within the server's limits) ────────────
    def _report(self, bid: str, res: BookingResult) -> None:
        ticket = self._passes.pop(bid, None)
        if ticket is None:
            return
        SECRET_CODES.discard(ticket.promo)
        if not ticket.cloud_id:
            return
        outcome = "done" if (res.success and res.pnr) else "failed"
        note = self._diag_note(bid, outcome == "done")
        reason = note if outcome == "done" else ((note + ": " if note else "") + friendly(res.error or ""))
        reason = reason[:80]
        threading.Thread(target=self._report_now, args=(bid, ticket.cloud_id, outcome, res.pnr if outcome == "done" else None, reason),
                         name="gate-report", daemon=True).start()

    def _report_now(self, bid: str, cloud_id: str, outcome: str, pnr: Optional[str], reason: str) -> bool:
        """Until the server has been told (a slow or broken connection is waited out), then it is marked as reported."""
        for wait in (0, 3, 8, 20, 45):
            if wait:
                time.sleep(wait)
            try:
                answer = self.gate.finish(cloud_id, outcome, pnr, reason)
                taken = int((self.store.get(bid) or {}).get("credits_used") or 0)
                back = taken if (outcome == "failed" and isinstance(answer, dict) and answer.get("refunded")) else 0
                self.store.update(bid, cloud_state="reported", credits_back=back)
                self.emit({"type": "account", "back": back})
                return True
            except Exception as e:  # noqa: BLE001
                code = getattr(e, "code", "")
                if code in ("not_found", "bad_request"):                  # the server will never accept this report: do not try for ever
                    self.store.update(bid, cloud_state="reported")
                    return True
                log.info("the result of a booking could not be reported yet (%s)", code or type(e).__name__)
        return False                                                      # it stays 'reserved': the next start tries again

    def _report_unreported(self) -> None:
        """At start: bookings whose result never reached the server (the app was closed, the connection was down)."""
        try:
            for rec in self.store.unreported():
                if rec["status"] in ("waiting", "running"):
                    continue
                outcome = "done" if rec["status"] == "done" and rec.get("pnr") else "failed"
                reason = "" if outcome == "done" else (rec.get("error") or "the app was closed")[:70]
                self._report_now(rec["id"], rec["cloud_id"], outcome, rec.get("pnr") if outcome == "done" else None, reason)
        except Exception:  # noqa: BLE001
            log.debug("reconcile failed", exc_info=True)

    async def _process(self, rec: dict) -> BookingResult:
        req = request_from(rec)
        try:                                                  # nothing starts unless the gate allows it (and the credits are taken first)
            ticket = await asyncio.get_running_loop().run_in_executor(None, self.gate.begin, rec)
        except gatemod.GateRefused as e:
            return BookingResult(success=False, error="!" + e.message)
        self._passes[rec["id"]] = ticket
        if ticket.cloud_id:
            self.store.update(rec["id"], cloud_id=ticket.cloud_id, cloud_state="reserved", credits_used=int(ticket.charged or 0))
        SECRET_CODES.add(ticket.promo)
        return await self._until_it_works(rec["id"], req, ticket.promo)

    # ── a booking IndiGo will not take is tried again by the engine itself: gently, bounded, and without ever telling anybody to wait ───────
    def _refusal_in(self, res: BookingResult) -> Optional[Refusal]:
        """A try that ended without an exception, only because IndiGo would not even start a session: a refusal too (an early one: nothing is chosen)."""
        if not res.success and self._reached <= EARLY and (res.error or "").lower().startswith("could not start a session"):
            return Refusal("session", res.error or "", early=True)
        return None

    async def _until_it_works(self, bid: str, req: BookingRequest, promo: str) -> BookingResult:
        """The booking, tried again BY ITSELF whenever IndiGo will not take the start of it (see RECOVERY). Up to that point nothing is chosen or held, so starting over
        costs the traveller nothing. A refusal LATER in the flow is never answered by starting over (the choice would be lost): the late calls repeat themselves
        calmly instead (ExtensionSession.call). The credit was taken once, at the gate, and is given back by the server if in the end it does not work."""
        plan = list(RECOVERY)
        repairs = 0
        net_waits = 0
        self._warm_override = None
        while True:
            self._reached = -1
            try:
                res = await self._within_work_time(self._runner(bid, req, promo), BOOKING_TIMEOUT_S)
            except asyncio.TimeoutError:                    # (a TimeoutError is an OSError: it must be caught first)
                return BookingResult(success=False, error="booking timed out")
            except bmod.BrowserDenied as e:
                refusal = Refusal("denied", str(e))
            except bmod.BrowserProblem as e:
                return BookingResult(success=False, error="!" + str(e))
            except IndigoBlocked as e:
                refusal = Refusal(e.kind or "fetch", str(e), early=self._reached <= EARLY, where=e.name or "")
            except IndigoError as e:
                m = re.match(r"browser error during (\S+?):", str(e))
                if m and self._reached <= EARLY:                      # (the page did not answer at all: a fresh start, like a denied page)
                    refusal = Refusal("page", str(e), where=m.group(1))
                else:
                    return BookingResult(success=False, error=str(e))
            except (BrowserGone, CDPError, OSError, aiohttp.ClientError) as e:
                repairs += 1
                log.warning("booking browser problem (try %d): %s", repairs, e)
                if repairs >= 2:
                    return BookingResult(success=False, error=f"booking browser unavailable: {str(e)[:100]}")
                self._note(bid, "Taking a little longer than usual…")
                self._stage(bid, "browser")
                await asyncio.sleep(3)
                continue
            else:
                refusal = self._refusal_in(res)
                if refusal is None:
                    self._check_details(bid, req, promo, res)
                    return res
            # The try did not work.
            if refusal.kind == "fetch" and not await self._online():          # (nothing to do with IndiGo: this computer has no connection - wait for it, spend nothing)
                net_waits += 1
                if refusal.early and net_waits <= NET_WAITS and await self._wait_for_internet(bid):
                    continue
                return BookingResult(success=False, error="!The internet connection seems to be down. Nothing was booked or charged — please check it and press Book again.")
            self._diag.setdefault(bid, []).append(f"{refusal.kind}@{refusal.where or '?'}")          # (a short note for the server: what was refused, how often)
            if not refusal.early or not plan:
                return self._gave_up(refusal)
            await self._calm(bid, refusal, plan.pop(0), first=len(plan) == len(RECOVERY) - 1)

    def _diag_note(self, bid: str, success: bool) -> str:
        """The few words the server keeps beside the booking (the admin sees them, the traveller never does): what IndiGo refused and how it ended -
        'worked on try 2 (fetch@search)' or '4 tries (fetch@search, 403@search)'. Empty when nothing was refused."""
        tags = self._diag.pop(bid, [])
        if not tags:
            return ""
        kinds = ", ".join(list(dict.fromkeys(tags))[:3])
        return f"worked on try {len(tags) + 1} ({kinds})" if success else f"{len(tags)} tries ({kinds})"

    def _gave_up(self, refusal: Refusal) -> BookingResult:
        """The patience is used up (or it happened too late to start over). No lock, no timer: the booking ends with a plain message and the next press of Book starts
        at once - only a little more patiently (a longer warm-up for a while)."""
        self._cautious_until = time.monotonic() + CAUTION_S
        log.warning("IndiGo would not take this booking (%s: %s)", refusal.kind, refusal.text)
        return BookingResult(success=False, retryable=True, error=f"IndiGo refused the request ({refusal.text})")

    async def _calm(self, bid: str, refusal: Refusal, step: tuple, first: bool) -> None:
        """One automatic recovery step: one friendly line, a rest (with the browser closed when the step wants a fresh one), and the warm-up the next try will use."""
        fresh, rest, warm = step
        if refusal.kind in ("denied", "page") and not fresh:   # (the page itself is IndiGo's "denied" page, or does not answer: asking again on it is pointless - start afresh, after a rest)
            fresh, rest = True, RECOVERY[min(1, len(RECOVERY) - 1)][1]
        self._note(bid, CALM_NOTE_FIRST if first else CALM_NOTE_AGAIN)
        self._stage(bid, "warmup")
        seconds = random.uniform(*rest) * (RATE_LIMIT_REST_FACTOR if refusal.kind == "429" else 1.0)
        log.warning("IndiGo would not take the start of this booking (%s): %s after %.0f s, with %.0f-%.0f s of natural activity first",
                    refusal.kind, "a fresh start" if fresh else "the same page again", seconds, warm[0], warm[1])
        if fresh:
            assert self._lock is not None
            async with self._lock:
                try:
                    await self.edge.close()
                except Exception:  # noqa: BLE001
                    log.debug("could not close the browser for a fresh start", exc_info=True)
        await asyncio.sleep(seconds)
        self._warm_override = warm

    async def _online(self) -> bool:
        """Does this computer reach IndiGo at all? A plain connection to its front door - no request is made, nothing is asked."""
        if _proxy_in_use():                                  # (behind a proxy a direct connection proves nothing - the browser may be online while this is not: assume it is)
            return True
        try:
            _r, w = await asyncio.wait_for(asyncio.open_connection(NET_PROBE_HOST, 443), 6.0)
        except (OSError, asyncio.TimeoutError):
            return False
        w.close()
        try:
            await w.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _wait_for_internet(self, bid: str) -> bool:
        """No connection at all: wait for it (a minute or two at most), say so once, spend nothing. True = it is back."""
        self._note(bid, "Waiting for the internet connection…")
        end = asyncio.get_running_loop().time() + NET_WAIT_S
        while asyncio.get_running_loop().time() < end:
            await asyncio.sleep(NET_POLL_S)
            if await self._online():
                self._note(bid, "The connection is back — carrying on.")
                return True
        return False

    def _check_details(self, bid: str, req: BookingRequest, promo: str, res: BookingResult) -> None:
        """The PNR exists: compare IndiGo's own record with what the traveller typed (names, route, date, phone, email, offer)."""
        itin, res.itinerary = res.itinerary, None                  # (the record holds personal data: it is not kept)
        if not (res.success and res.pnr and res.kind == "record_locator"):
            return
        try:
            status, note = verify.summarize(verify.check(req, itin, promo, getattr(res, "flight", None)))
        except Exception:  # noqa: BLE001 - checking must never spoil a booking that exists
            log.warning("could not check the booking record", exc_info=True)
            return
        res.verified, res.verify_note = status, note                # type: ignore[attr-defined]
        if status == "ok":
            self._note(bid, "Details checked with IndiGo ✓")
        elif status == "mismatch":
            self._note(bid, note)

    async def _book_once(self, bid: str, req: BookingRequest, promo: str) -> BookingResult:
        """One real booking: the browser starts now (out of sight), the work is done, the browser closes soon after."""
        assert self._lock is not None
        self.expect_choice = True
        self._stage_times = []
        self._stage(bid, "browser")
        for fut in list(self._fares_tasks):                    # a booking comes first: the fare calendar's work gives way
            fut.cancel()
        async with self._lock:
            ep = await self._browser(note=lambda text: self._note(bid, text))
            self._stage(bid, "warmup")
            try:
                steps: list = []
                async with CDPBrowser(ep) as b:
                    await bmod.tidy(b)
                    try:
                        tab, sess = await open_session(b, steps, session_cls=ExtensionSession)
                    except CDPError as e:
                        raise BrowserGone(str(e)) from e

                    async def psp() -> None:
                        # a pop-up window (the bank's) only ever opens here: the guard that moves it out of sight runs only here
                        guard = None if self.debug else asyncio.ensure_future(bmod.guard_windows(ep))
                        try:
                            await run_psp(b)
                        finally:
                            if guard is not None:
                                guard.cancel()

                    async def pick(options: list[dict]) -> Optional[str]:
                        return await self.ask_flight(bid, options)

                    warm = self._warm_override or self.warmup_for()
                    self._warm_override = None
                    log.info("page warm-up: %s (the page has had %.0f s of activity)", f"{warm[0]:.0f}-{warm[1]:.0f} s" if warm else "full", self.warm_credit())
                    try:
                        res = await run_booking(sess, req, promo, "payment", psp=psp, choose=pick, warmup=warm)
                    finally:
                        try:
                            await tab.close()
                        except Exception:  # noqa: BLE001
                            pass
                self._warmed = True
                self._add_warm(sum(warm) / 2 if warm else sum(WARM_NEEDED) / 2)
                res.steps = steps
                return res
            finally:
                self._touch(self.after_booking_close_s)
