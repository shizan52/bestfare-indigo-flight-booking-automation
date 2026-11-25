"""The booking engine with a stand-in for IndiGo: one booking at a time, progress in the customer's words, IndiGo being busy,
repair, timeout, stop - and the booking browser that starts only when needed, works out of sight and closes by itself."""
import asyncio
import logging
import threading
import time
from datetime import date, timedelta

import pytest

import engine as eng
import fares
import forms
from cdp import CDPError
from indigo_client import BookingRequest, BookingResult, IndigoBlocked, Pace, Passenger


class FakeEdge:
    def __init__(self):
        self.ensured = []              # one entry per ensure(): the 'quiet' flag it was asked with
        self.launches = 0
        self.closed = 0

    async def ensure(self, note=None, quiet=False):
        self.ensured.append(quiet)
        if len(self.ensured) == 1 or self.closed >= self.launches:
            self.launches += 1                                   # (a closed browser has to be started again)
        return "http://127.0.0.1:1"

    async def close(self):
        self.closed += 1
        return True


class Events(list):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()

    def __call__(self, ev):
        with self.lock:
            self.append(ev)

    def wait(self, pred, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                hits = [e for e in self if pred(e)]
            if hits:
                return hits
            time.sleep(0.02)
        raise AssertionError(f"event not seen; got {[e.get('type') for e in self]}")


def order(offer="offer1") -> forms.Order:
    o = forms.Order(origin="DEL", dest="BOM", day=forms.today_ist() + timedelta(days=7),
                    travellers=[forms.Traveller("RAHUL", "SHARMA", "M")], phone="9876543210", email="a@b.co",
                    offer=offer, eligible=True)
    assert forms.validate(o) == {}
    return o


@pytest.fixture()
def make(store, monkeypatch):
    monkeypatch.setattr(eng, "IDLE_POLL_S", 0.03)
    made = []

    def _make(runner=None, edge=None, debug=False, idle=0.3, after=0.2, gate=None):
        ev = Events()
        e = eng.Engine(store, ev, edge=edge or FakeEdge(), runner=runner, debug=debug, idle_close_s=idle, after_booking_close_s=after, gate=gate)
        e.start()
        made.append(e)
        return e, ev

    yield _make
    for e in made:
        e.shutdown(close_browser=False)


def finished(ev, bid):
    return ev.wait(lambda e: e["type"] == "finished" and e["id"] == bid)[0]["record"]


# ── a booking ────────────────────────────────────────────────────────────────────────────────────────
def test_a_booking_reports_progress_in_the_customers_words(make, store):
    log = logging.getLogger("indigo_client")

    async def runner(bid, req, promo):
        log.info("indigo token      -> 200 (300 ms) ")
        log.info("indigo search     -> 200 (900 ms) ")
        log.info("cheapest DEL353BOM stops=0 product=R Rs5132")
        log.info("indigo sell       -> 200 (800 ms) ")
        log.info("coupon %s applied=True (IndiGo says '%s', discount Rs1902.0)", promo, promo)
        log.info("indigo addpax     -> 200 (700 ms) ")
        await asyncio.sleep(0.05)
        return BookingResult(success=True, pnr="PNRABC", kind="record_locator", fare_amount=5132.0, coupon_applied=True,
                             discount=1902.0, hold_until="2030-01-01T00:30:00Z")

    e, ev = make(runner)
    _grp, ids = e.submit(order("offer2"))
    r = finished(ev, ids[0])
    assert r["status"] == "done" and r["pnr"] == "PNRABC" and r["discount"] == 1902.0 and r["coupon_applied"] is True
    stages = [x["stage"] for x in ev if x["type"] == "stage" and x["id"] == ids[0]]
    assert stages == ["search", "hold", "travellers", "pnr"]
    notes = [x["text"] for x in ev if x["type"] == "note" and x["id"] == ids[0]]
    assert "Best fare found: ₹5,132 (non-stop)" in notes and any("you save ₹1,902" in n for n in notes)
    assert not any(c in n for n in notes for c in ("TESTCODE1", "TESTCODE2", "6ETST001", "PROMO2"))      # the offer code never reaches the screen
    assert not any(x["type"] == "log" for x in ev)                                       # normal mode: nothing technical is emitted
    assert not e.busy


def test_the_engine_checks_indigos_record_against_the_order(make, store):
    good = {"data": {"passengers": [{"name": {"first": "RAHUL", "last": "SHARMA"}, "info": {"gender": 1}}],
                     "journeysDetail": [{"journeydetail": {"origin": "DEL", "destination": "BOM", "departure": None}}],
                     "contacts": [{"homePhone": "919876543210", "emailAddress": "a@b.co"}], "bookingDetails": {"promoCode": "TESTCODE2"}}}
    day = {"n": 0}

    async def runner(bid, req, promo):
        d = {**good, "data": {**good["data"], "journeysDetail": [{"journeydetail": {
            "origin": "DEL", "destination": "BOM", "departure": req.date[6:] + "-" + req.date[3:5] + "-" + req.date[:2] + "T06:00:00"}}]}}
        day["n"] += 1
        if day["n"] == 2:                                                                # the second record has the wrong date
            d["data"]["journeysDetail"][0]["journeydetail"]["departure"] = "2031-01-01T06:00:00"
        return BookingResult(success=True, pnr=f"PNR{day['n']}", kind="record_locator", coupon_applied=True, itinerary=d)

    e, ev = make(runner)
    _g, a = e.submit(order("offer2"))
    ra = finished(ev, a[0])
    assert ra["verified"] == "ok" and "match" in ra["verify_note"]
    assert any(x["type"] == "note" and x["text"] == "Details checked with IndiGo ✓" for x in ev)
    _g, b = e.submit(order("offer2"))
    rb = finished(ev, b[0])
    assert rb["verified"] == "mismatch" and "different travel date" in rb["verify_note"] and "1 Jan 2031" in rb["verify_note"]
    assert any(x["type"] == "note" and "Please check this before you pay" in x["text"] for x in ev)
    raw = store.path.read_bytes()
    assert b"2031-01-01T06:00:00" not in raw and b"journeysDetail" not in raw             # IndiGo's record itself is not kept


def test_a_record_that_says_little_is_neither_confirmed_nor_blamed(make):
    async def runner(bid, req, promo):
        return BookingResult(success=True, pnr="PNRX", kind="record_locator", itinerary={"data": {"bookingDetails": {"recordLocator": "PNRX"}}})

    e, ev = make(runner)
    _g, ids = e.submit(order())
    r = finished(ev, ids[0])
    assert r["verified"] == "unknown" and not any(x["type"] == "note" and "checked" in x["text"].lower() for x in ev)


def test_a_broken_browser_is_repaired_and_the_booking_tried_once_more(make):
    n = {"calls": 0}

    async def runner(bid, req, promo):
        n["calls"] += 1
        if n["calls"] == 1:
            raise CDPError("connection closed")
        return BookingResult(success=True, pnr="OK1234", kind="record_locator")

    e, ev = make(runner)
    _g, ids = e.submit(order())
    r = finished(ev, ids[0])
    assert r["status"] == "done" and r["pnr"] == "OK1234" and n["calls"] == 2
    assert any(x["type"] == "note" and "longer than usual" in x["text"] for x in ev)


def test_a_browser_that_stays_broken_gives_a_plain_message(make):
    async def runner(bid, req, promo):
        raise CDPError("connection closed")

    e, ev = make(runner)
    _g, ids = e.submit(order())
    r = finished(ev, ids[0])
    assert r["status"] == "failed" and r["error"] == "Something went wrong. Please try again."
    assert "browser" not in r["error"].lower() and "edge" not in r["error"].lower()


def test_a_hanging_booking_is_stopped(make, monkeypatch):
    monkeypatch.setattr(eng, "BOOKING_TIMEOUT_S", 0.3)

    async def runner(bid, req, promo):
        await asyncio.sleep(30)

    e, ev = make(runner)
    _g, ids = e.submit(order())
    r = finished(ev, ids[0])
    assert r["status"] == "failed" and "longer than usual" in r["error"]


def test_the_user_can_stop_a_running_booking_and_drop_a_waiting_one(make):
    started = threading.Event()

    async def runner(bid, req, promo):
        started.set()
        await asyncio.sleep(30)

    e, ev = make(runner)
    _g, first = e.submit(order())
    _g, second = e.submit(order("offer2"))                                             # waits behind the first
    assert started.wait(5)
    e.stop(second[0])
    e.stop(first[0])
    r1, r2 = finished(ev, first[0]), finished(ev, second[0])
    assert r1["status"] == r2["status"] == "failed" and "stopped" in r1["error"] and "stopped" in r2["error"]
    time.sleep(0.2)
    assert not e.busy


def test_a_crash_in_the_engine_never_hangs_the_queue(make):
    n = {"calls": 0}

    async def runner(bid, req, promo):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("boom")
        return BookingResult(success=True, pnr="NEXT01")

    e, ev = make(runner)
    _g, a = e.submit(order())
    _g, b = e.submit(order())
    assert finished(ev, a[0])["status"] == "failed"
    assert finished(ev, b[0])["status"] == "done"


def test_one_offer_is_one_booking(make):
    seen = []

    async def runner(bid, req, promo):
        seen.append(promo)
        return BookingResult(success=True, pnr="P1")

    e, ev = make(runner)
    _g, ids = e.submit(order("offer1"))
    assert len(ids) == 1
    finished(ev, ids[0])
    _g, ids = e.submit(order("offer2"))
    finished(ev, ids[0])
    assert seen == ["TESTCODE1", "TESTCODE2"]                                           # each offer's code comes from the gate


# ── the booking browser ──────────────────────────────────────────────────────────────────────────────
def test_the_browser_is_not_started_when_the_engine_starts(make):
    edge = FakeEdge()
    make(edge=edge)
    time.sleep(0.5)
    assert edge.ensured == [] and edge.launches == 0


def test_the_browser_starts_on_demand_quietly_and_closes_by_itself(make):
    edge = FakeEdge()
    e, _ev = make(edge=edge, idle=0.2)
    e.call(e._browser()).result(5)
    assert edge.ensured == [True]                                                       # quiet = out of sight
    time.sleep(0.15)
    assert edge.closed == 0                                                             # still within its idle time
    for _ in range(100):
        if edge.closed:
            break
        time.sleep(0.03)
    assert edge.closed == 1


def test_debug_mode_shows_the_browser_window(make):
    edge = FakeEdge()
    e, _ev = make(edge=edge, debug=True)
    e.call(e._browser()).result(5)
    assert edge.ensured == [False]


def test_the_browser_closes_soon_after_a_booking_is_done(make):
    edge = FakeEdge()

    async def runner(bid, req, promo):
        await e._browser()
        return BookingResult(success=True, pnr="P1", kind="record_locator")

    e, ev = make(runner, edge=edge, idle=60.0, after=0.15)
    _g, ids = e.submit(order())
    finished(ev, ids[0])
    for _ in range(100):
        if edge.closed:
            break
        time.sleep(0.03)
    assert edge.closed == 1 and edge.ensured == [True]                                  # one start for the booking, one close after it


def test_the_browser_is_never_closed_under_a_running_booking(make):
    edge = FakeEdge()
    go = threading.Event()

    async def runner(bid, req, promo):
        await e._browser()
        await asyncio.sleep(0.6)                                                        # much longer than the idle time below
        go.set()
        return BookingResult(success=True, pnr="P1", kind="record_locator")

    e, ev = make(runner, edge=edge, idle=0.1, after=0.1)
    _g, ids = e.submit(order())
    finished(ev, ids[0])
    assert go.is_set() and edge.closed <= 1


# ── the fare calendar's side ─────────────────────────────────────────────────────────────────────────
class FakeTab:
    async def viewport(self):
        return 1000, 700

    async def close(self):
        pass


class FakeBrowser:
    def __init__(self, ep):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def pages(self):
        return [{"id": "h", "url": "https://www.goindigo.in/", "title": "Book flights"}]

    async def open_tab(self, info):
        return FakeTab()


@pytest.fixture()
def fare_env(monkeypatch):
    calls = []

    async def fake_fetch(tab, origin, dest, start, days=60):
        calls.append((origin, dest, start, days))
        return fares.FareResult(fares={start.isoformat(): (6001.0, "L")})

    class Human:
        def __init__(self, *a):
            pass

        async def wander(self, *a):
            pass

    async def no_wait():
        return None

    monkeypatch.setattr(eng, "CDPBrowser", FakeBrowser)
    monkeypatch.setattr(eng, "HumanInput", Human)
    monkeypatch.setattr(eng.fares, "fetch", fake_fetch)
    monkeypatch.setattr(eng.Pace, "wait", staticmethod(no_wait))
    return calls


def test_fares_come_back_as_an_event_and_are_remembered(make, fare_env):
    edge = FakeEdge()
    e, ev = make(edge=edge, idle=60)
    start = date(2026, 12, 1)
    e.request_fares("DEL", "BOM", start, 60)
    got = ev.wait(lambda x: x["type"] == "fares")[0]
    assert got["origin"] == "DEL" and got["dest"] == "BOM" and got["fares"] == {"2026-12-01": [6001.0, "L"]} and got["error"] is None
    assert edge.ensured == [True]
    e.request_fares("DEL", "BOM", start, 60)                                            # the same again: from memory
    time.sleep(0.4)
    assert len(fare_env) == 1 and len([x for x in ev if x["type"] == "fares"]) == 2


def test_fares_say_busy_while_a_booking_runs_and_only_then(make, fare_env):
    e, ev = make(edge=FakeEdge(), idle=60)
    e.current = "B1"
    e.request_fares("DEL", "BOM", date(2026, 12, 1), 60)
    assert ev.wait(lambda x: x["type"] == "fares")[0]["error"] == "busy"
    assert fare_env == []                                                              # nothing was asked of IndiGo while the booking works
    e.current = None
    e.request_fares("DEL", "BLR", date(2026, 12, 1), 60)
    got = ev.wait(lambda x: x["type"] == "fares" and x["dest"] == "BLR")[0]
    assert got["error"] is None and len(fare_env) == 1                                 # (and no lock-out of any kind keeps the calendar away afterwards)


def test_a_refusal_pauses_fare_requests_for_a_while(make, monkeypatch):
    async def refused(tab, origin, dest, start, days=60):
        return fares.FareResult(error="busy")

    class Human:
        def __init__(self, *a):
            pass

        async def wander(self, *a):
            pass

    async def no_wait():
        return None

    monkeypatch.setattr(eng, "CDPBrowser", FakeBrowser)
    monkeypatch.setattr(eng, "HumanInput", Human)
    monkeypatch.setattr(eng.fares, "fetch", refused)
    monkeypatch.setattr(eng.Pace, "wait", staticmethod(no_wait))
    e, ev = make(edge=FakeEdge(), idle=60)
    e.request_fares("DEL", "BOM", date(2026, 12, 1), 60)
    assert ev.wait(lambda x: x["type"] == "fares")[0]["error"] == "busy"
    assert time.monotonic() + 30 < e._fares_off_until <= time.monotonic() + eng.FARES_OFF_S      # the calendar rests a moment - nothing else does


def test_a_browser_that_cannot_start_means_no_fares_not_a_crash(make, monkeypatch):
    class Dead(FakeEdge):
        async def ensure(self, note=None, quiet=False):
            raise eng.bmod.BrowserProblem("Could not get started.")

    e, ev = make(edge=Dead(), idle=60)
    e.request_fares("DEL", "BOM", date(2026, 12, 1), 60)
    got = ev.wait(lambda x: x["type"] == "fares")[0]
    assert got["error"] == "unavailable" and got["fares"] == {}


# ── words ────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expect", [
    ("no flights available for this route/date", "no seats"),
    ("cooling down for 300s after a block", "not responding well"),
    ("IndiGo refused the request (search: HTTP 429 (rate limited))", "not responding well"),
    ("IndiGo refused the request (sell: HTTP 503 (IndiGo server error))", "not responding"),
    ("booking timed out", "longer than usual"),
    ("fare could not be held (HTTP 400)", "no longer available"),
    ("passenger details rejected (HTTP 400)", "traveller details"),
    ("booking browser unavailable: connection closed", "Something went wrong"),
    ("!Could not get started. Please try again.", "Could not get started"),
    ("something strange", "could not be completed"),
])
def test_friendly_reasons(raw, expect):
    assert expect in eng.friendly(raw)


def test_nothing_the_customer_can_read_explains_how_the_work_is_done():
    leaks = ("slow down", "token", "search request", "browser", "edge", "api", "session", "gateway", "cookie", "429", "blocked", "refused")
    raws = ["no flights available for this route/date", "cooling down for 300s after a block", "IndiGo refused the request (x)",
            "IndiGo server error", "booking timed out", "fare could not be held", "passenger details rejected",
            "booking browser unavailable: connection closed", "something strange", "internal error: X", "stopped by you"]
    for raw in raws:
        text = eng.friendly(raw).lower()
        assert not any(w in text for w in leaks), (raw, text)
        assert "wait" not in text and "minutes" not in text, (raw, text)             # never a timer, never "wait a while"
    for text in list(eng.STAGE_TEXT.values()):
        assert not any(w in text.lower() for w in leaks), text


def test_activity_lines_for_debug_mode_are_plain_and_hide_the_offer_codes(monkeypatch):
    monkeypatch.setattr(eng, "SECRET_CODES", {"TESTCODE2"})                       # (the code of a booking that is running)
    cases = [
        ("indigo_client", "indigo token      -> 200 (333 ms) ", 20, "info", "Started a session with IndiGo — OK (333 ms)"),
        ("indigo_client", "indigo search     -> BLOCKED(no-cors) (688 ms) TypeError: Failed to fetch", 20, "error", "slow down"),
        ("indigo_client", "indigo sell       -> 403 (500 ms) ", 20, "warn", "code 403"),
        ("indigo_client", "indigo calendar   -> 200 (400 ms) ", 20, "info", "Loaded the fare calendar"),
        ("engine", "booking X failed: IndiGo refused the request (TESTCODE2)", 30, "info", "the offer code"),
        ("psp_flow", "the gateway redrew itself after HDFC Bank was chosen (try 1) - choosing again", 20, "info", "Payment page:"),
    ]
    for name, msg, lv, level, expect in cases:
        got = eng.activity_line(name, msg, lv)
        assert got and got[0] == level and expect in got[1], (msg, got)
    for skipped in ("cheapest DEL353BOM stops=0 product=R Rs5132", "coupon TESTCODE1 applied=True (IndiGo says 'TESTCODE1')",
                    "PNR GCVUQG (On-Hold) hold until 2026-11-18T04:02:01Z (attempt 1)"):
        assert eng.activity_line("indigo_client", skipped, 20) is None
    assert eng.activity_line("engine", "booking X done: record_locator GCVUQG", 20) is None


def test_debug_mode_feeds_the_technical_story_to_the_activity_page(make):
    log = logging.getLogger("indigo_client")

    async def runner(bid, req, promo):
        log.info("indigo token      -> 200 (300 ms) ")
        log.info("coupon %s applied=True (IndiGo says '%s', discount Rs1902.0)", promo, promo)
        return BookingResult(success=True, pnr="LOG123", kind="record_locator")

    e, ev = make(runner, debug=True)
    _g, ids = e.submit(order("offer2"))
    finished(ev, ids[0])
    lines = [x["text"] for x in ev if x["type"] == "log"]
    assert "Started a session with IndiGo — OK (300 ms)" in lines
    assert not any("TESTCODE" in t or "PROMO2" in t or "6ETST001" in t for t in lines)


def test_shutdown_closes_the_booking_browser(store):
    edge = FakeEdge()

    async def runner(bid, req, promo):
        return BookingResult(success=True, pnr="X")

    e = eng.Engine(store, Events(), edge=edge, runner=runner)
    e.start()
    e.shutdown(close_browser=True)
    assert edge.closed == 1 and not e.thread.is_alive()


def test_the_request_the_engine_builds_is_what_the_traveller_entered(store):
    rec = {"travel_date": "2026-12-09", "origin": "DEL", "dest": "BOM", "offer": "offer2", "email": "a@b.co", "phone": "9876543210",
           "travellers": [{"first": "RAHUL KUMAR", "last": "SHARMA", "gender": "M"}, {"first": "PRIYA", "last": "SHARMA", "gender": "F"}]}
    r = eng.request_from(rec)
    assert isinstance(r, BookingRequest) and r.date == "09-12-2026" and (r.origin, r.destination) == ("DEL", "BOM")
    assert [(p.first_name, p.last_name, p.gender) for p in r.passengers] == [("RAHUL KUMAR", "SHARMA", "M"), ("PRIYA", "SHARMA", "F")]
    assert isinstance(r.passengers[0], Passenger) and r.contact_email == "a@b.co" and r.contact_phone == "9876543210"
