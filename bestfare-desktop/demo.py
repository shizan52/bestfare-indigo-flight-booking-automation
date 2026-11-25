"""PRACTICE MODE (python run.py --demo): the real window with a pretend booking engine. Nothing is sent to IndiGo, no browser is
opened, nothing is booked. Use it to look at every screen and try every outcome - success, a booking IndiGo's record disagrees
with, no seats, IndiGo busy, something went wrong - in about ten seconds each. The fare calendar shows made-up fares. Its history
is kept in a separate file, so it never mixes with real bookings.
"""
from __future__ import annotations

import logging
import queue
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from PyQt6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel

import engine as eng
import flights
import forms
from store import Store

log = logging.getLogger("demo")

OUTCOMES = [("success", "Success — you get a PNR"), ("mismatch", "Success, but IndiGo's record shows a different date"),
            ("no_seats", "No seats on this route"), ("slow_down", "IndiGo is slow for a moment (the app tries again by itself)"),
            ("gives_up", "IndiGo does not answer (the app tries, then says so)"), ("browser", "Something went wrong")]
STEP_SECONDS = {"browser": 0.8, "warmup": 2.0, "search": 1.6, "hold": 1.4, "travellers": 1.2, "pnr": 2.4}
FARE_SECONDS = 1.2


def make_fares(origin: str, dest: str, start: date, days: int) -> dict[str, list]:
    """Made-up but steady fares: the same route and day always give the same number; some days have no flight."""
    out: dict[str, list] = {}
    base = 5200 + (sum(map(ord, origin + dest)) % 11) * 180
    for i in range(days):
        d = start + timedelta(days=i)
        rnd = random.Random(f"{origin}{dest}{d.isoformat()}")
        if rnd.random() < 0.06:
            continue                                                   # no flight that day
        price = base + rnd.choice((0, 0, 220, 430, 900, 1800, 3500, 6200)) + (700 if d.weekday() in (4, 6) else 0)
        out[d.isoformat()] = [float(price), "L" if price < base + 300 else ("M" if price < base + 1500 else "H")]
    return out


VIA_CITIES = ("Ahmedabad", "Hyderabad", "Nagpur", "Jaipur", "Bhopal", "Lucknow", "Indore", "Raipur")
DEPARTURES = ((5, 10), (6, 45), (8, 20), (10, 5), (12, 15), (14, 40), (16, 30), (18, 55), (20, 25), (22, 40))
CONNECTING = (1, 4, 8)                      # which of the departures above are connections (1 stop)


def make_flights(origin: str, dest: str, day: date, pax: int = 1) -> list[dict]:
    """Made-up flights shaped exactly like the real list (indigo_client.flight_options): the same route and day always give the
    same flights. Most flights have the offer in their lowest fare, a few do not; a few are connections."""
    rnd = random.Random(f"{origin}{dest}{day.isoformat()}")
    base = 4300 + (sum(map(ord, origin + dest)) % 13) * 140
    nonstop = 85 + (sum(map(ord, dest + origin)) % 9) * 14
    out = []
    for i, (h, m) in enumerate(DEPARTURES):
        dep = datetime(day.year, day.month, day.day, h, m)
        connect = i in CONNECTING
        minutes = nonstop + (rnd.choice((150, 190, 240)) if connect else rnd.choice((0, 0, 5, 10)))
        arr = dep + timedelta(minutes=minutes)
        each = base + rnd.choice((0, 150, 320, 600, 900, 1400)) - (420 if connect else 0)
        offer = rnd.random() < 0.72
        original = int(each * rnd.uniform(1.28, 1.42)) if offer else None
        number = lambda: f"6E {rnd.randint(100, 7999)}"                               # noqa: E731
        out.append({"key": f"PRACTICE-{i}", "fare_key": f"PRACTICE-FARE-{i}", "amount": float(each * pax),
                    "original": float(original * pax) if original else None, "offer": offer, "product": "R" if offer else "B",
                    "seg": f"{origin}{i}{dest}", "stops": 1 if connect else 0, "dep": dep.strftime("%Y-%m-%dT%H:%M:%S"),
                    "arr": arr.strftime("%Y-%m-%dT%H:%M:%S"), "minutes": minutes,
                    "flights": [number(), number()] if connect else [number()],
                    "via": [rnd.choice(VIA_CITIES)] if connect else [],
                    "origin": origin, "dest": dest, "origin_city": origin, "dest_city": dest})
    return out


class DemoEngine:
    """Same calls as engine.Engine, the same events - but only pretending."""

    def __init__(self, store: Store, emit, debug: bool = False):
        self.store = store
        self._emit = emit
        self.debug = debug
        self.outcome = "success"
        self.auto_choose = False                                    # tests / screenshots: take the lowest fare without asking
        self._offered: dict[str, dict] = {}
        self._picked: Optional[str] = None
        self._stopped: set[str] = set()
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._waiting: list[str] = []
        self._current: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._alive = True
        self.fare_requests: list[tuple] = []

    # ── the engine's interface ────────────────────────────────────────────────────────────────────────
    def start(self) -> None:
        n = self.store.interrupted()
        if n:
            log.info("%d practice booking(s) were cut off by the last close", n)
        self._thread = threading.Thread(target=self._worker, name="demo-engine", daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        return bool(self._current) or bool(self._waiting)

    def submit(self, order: forms.Order):
        grp = f"demo{int(time.time())}"
        travellers = order.records()
        assert order.day is not None
        ids = [self.store.add_booking(grp=grp, origin=order.origin, dest=order.dest, travel_date=order.day.isoformat(),
                                      offer=o, travellers=travellers, phone=order.phone, email=order.email)
               for o in order.bookings]
        for bid in ids:
            self._waiting.append(bid)
            self._q.put(bid)
        return grp, ids

    def stop(self, bid: str) -> None:
        self._stopped.add(bid)
        if bid in self._waiting:
            self._waiting.remove(bid)
            rec = self.store.get(bid)
            self.store.update(bid, status="failed", error="You stopped this booking.", finished_at=time.time())
            self._emit({"type": "finished", "id": bid, "grp": rec["grp"] if rec else None, "record": self.store.get(bid)})

    def choose_flight(self, bid: str, key: str) -> None:
        if key in self._offered:
            self._picked = key

    def keep_open(self, seconds: float = 90.0) -> None:
        """(practice mode has no browser to keep)"""

    def request_fares(self, origin: str, dest: str, start: date, days: int = 60) -> None:
        self.fare_requests.append((origin, dest, start, days))

        def answer() -> None:
            if not self._alive:
                return
            self._emit({"type": "fares", "origin": origin, "dest": dest, "start": start.isoformat(), "days": days,
                        "fares": make_fares(origin, dest, start, days), "empty": False, "error": None})
        threading.Timer(FARE_SECONDS, answer).start()

    def shutdown(self, close_browser: bool = True) -> None:
        self._alive = False
        self._q.put(None)
        if self._thread:
            self._thread.join(3)

    # ── pretending ───────────────────────────────────────────────────────────────────────────────────
    def _log(self, level: str, text: str) -> None:
        self._emit({"type": "log", "level": level, "text": text, "t": time.time()})

    def _wait(self, seconds: float, bid: str) -> bool:
        """Sleep in small steps so Stop reacts at once. False = stopped."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if bid in self._stopped or not self._alive:
                return False
            time.sleep(0.05)
        return True

    def _worker(self) -> None:
        while self._alive:
            bid = self._q.get()
            if bid is None or bid not in self._waiting:
                continue
            self._waiting.remove(bid)
            rec = self.store.get(bid)
            if not rec or rec["status"] != "waiting":
                continue
            self._current = bid
            try:
                self._run(rec)
            except Exception:  # noqa: BLE001
                log.exception("practice booking crashed")
                self._finish(rec, error="The booking could not be completed. Please try again.")
            finally:
                self._current = None

    def _stage(self, bid: str, stage: str) -> bool:
        self._emit({"type": "stage", "id": bid, "stage": stage})
        return self._wait(STEP_SECONDS[stage], bid)

    def _finish(self, rec: dict, error: Optional[str] = None, **ok) -> None:
        if error is None:
            self.store.update(rec["id"], status="done", finished_at=time.time(), **ok)
        else:
            self.store.update(rec["id"], status="failed", error=error, finished_at=time.time())
        self._emit({"type": "finished", "id": rec["id"], "grp": rec["grp"], "record": self.store.get(rec["id"])})

    def _ask(self, bid: str, rec: dict, options: list[dict]) -> Optional[dict]:
        """Show the practice flights and wait for the pick (like the real engine). None = stopped, or nothing chosen in time."""
        self._offered = {o["key"]: o for o in options}
        self._picked = None
        self._emit({"type": "choose", "id": bid, "grp": rec["grp"], "flights": [flights.public(o) for o in options],
                    "seconds": eng.CHOICE_WAIT_S})
        end = time.monotonic() + eng.CHOICE_WAIT_S
        while time.monotonic() < end and self._picked is None:
            if bid in self._stopped or not self._alive:
                break
            if self.auto_choose:
                self._picked = flights.lowest_key(options)
            time.sleep(0.05)
        picked = self._offered.get(self._picked) if self._picked else None
        self._emit({"type": "chosen", "id": bid, "flight": flights.public(picked) if picked else None})
        return picked

    def _run(self, rec: dict) -> None:
        bid, outcome = rec["id"], self.outcome
        self._emit({"type": "started", "id": bid, "grp": rec["grp"], "offer": rec["offer"]})
        self.store.update(bid, status="running")
        fail = lambda text: self._finish(rec, error=text)           # noqa: E731
        stopped = lambda: self._finish(rec, error=eng.friendly("stopped by you"))   # noqa: E731

        if not self._stage(bid, "browser"):
            return stopped()
        if outcome == "browser":
            self._log("error", "(practice) the booking browser stopped answering")
            return fail(eng.friendly("booking browser unavailable: connection closed"))
        if not self._stage(bid, "warmup"):
            return stopped()
        if outcome in ("slow_down", "gives_up"):
            # like the real thing: IndiGo does not take the first try; the app says so calmly, goes back a step by itself and asks again - no timer, no lock
            self._log("error", "(practice) IndiGo refused the search - trying again by itself")
            self._emit({"type": "note", "id": bid, "text": eng.CALM_NOTE_FIRST})
            if not self._stage(bid, "warmup"):
                return stopped()
            if outcome == "gives_up":
                self._emit({"type": "note", "id": bid, "text": eng.CALM_NOTE_AGAIN})
                if not self._stage(bid, "warmup"):
                    return stopped()
                return fail(eng.friendly("IndiGo refused the request (search: HTTP 429)"))
        if not self._stage(bid, "search"):
            return stopped()
        if outcome == "no_seats":
            return fail(eng.friendly("no flights available for this route/date"))
        day = datetime.strptime(rec["travel_date"], "%Y-%m-%d").date()
        a, c, _i = forms.record_mix(rec["travellers"])
        options = make_flights(rec["origin"], rec["dest"], day, a + c)                  # (infants pay a small fixed fee: not a fare per seat)
        picked = self._ask(bid, rec, options)                      # "Choose your flight": the booking waits for the traveller
        if picked is None:
            return stopped() if bid in self._stopped or not self._alive else fail(eng.friendly("no flight was chosen"))
        fare = picked["amount"]
        save = flights.saving(picked)
        self._emit({"type": "note", "id": bid, "text": f"Your flight: {flights.line(picked)} · {flights.money(picked)}"})
        if not self._stage(bid, "hold"):
            return stopped()
        if save > 0:
            self._emit({"type": "note", "id": bid, "text": f"Offer applied — you save {forms.money(save)}"})
        else:
            self._emit({"type": "note", "id": bid,
                        "text": "IndiGo did not apply the offer to this flight — it will be the regular fare."})
        if not self._stage(bid, "travellers"):
            return stopped()
        if not self._stage(bid, "pnr"):
            return stopped()
        pnr = "DEMO" + "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(2))
        hold = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if outcome == "mismatch":
            verified, note = "mismatch", "Please check this before you pay: IndiGo's booking has a different travel date."
            self._emit({"type": "note", "id": bid, "text": note})
        else:
            verified, note = "ok", "Checked against IndiGo's booking: names, route, date and contact match."
            self._emit({"type": "note", "id": bid, "text": "Details checked with IndiGo ✓"})
        self._finish(rec, pnr=pnr, kind="record_locator", fare=float(fare), discount=float(save), coupon_applied=save > 0,
                     hold_until=hold, verified=verified, verify_note=note, flight=flights.line(picked))


class DemoBar(QFrame):
    """The orange strip on top of the window in practice mode: what should the next booking do?"""

    def __init__(self, engine: DemoEngine):
        super().__init__()
        self.setObjectName("demobar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 9, 24, 9)
        lay.setSpacing(10)
        title = QLabel("🧪  PRACTICE MODE")
        title.setStyleSheet("font-weight: 700;")
        lay.addWidget(title)
        lay.addWidget(QLabel("Nothing is sent to IndiGo. Choose what the next booking should do:"))
        self.combo = QComboBox()
        for key, text in OUTCOMES:
            self.combo.addItem(text, key)
        self.combo.currentIndexChanged.connect(lambda _i: setattr(engine, "outcome", self.combo.currentData()))
        lay.addWidget(self.combo)
        lay.addStretch(1)
