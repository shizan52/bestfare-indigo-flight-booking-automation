"""A faster booking without being careless: the page is warmed up WHILE the traveller fills the form (so Book now does not wait ~40 s),
the browser stays for the form, the calendar's work gives way to a booking, the pop-up guard runs only where pop-ups happen, and a
slow connection is waited for instead of failing."""
import asyncio
import logging
import time

import pytest

import engine as eng
import speed
from indigo_client import BookingResult
from test_engine import Events, FakeEdge, finished, make, order  # noqa: F401  (the same stand-ins as the engine tests)


@pytest.fixture(autouse=True)
def fresh_net():
    speed.Net.reset()
    yield
    speed.Net.reset()


@pytest.fixture(autouse=True)
def known_installation(monkeypatch):
    """These tests are about the usual warm-up of an installation IndiGo already knows (a brand-new one warms up longer: see test_self_healing)."""
    monkeypatch.setattr(eng.Engine, "_was_ever_used", lambda self: True)


# ── the warm-up credit ──────────────────────────────────────────────────────────────────────────────────────────────
def test_a_cold_page_gets_the_full_warm_up_and_a_warm_one_only_what_is_missing(make):
    e, _ev = make()
    assert e.warm_credit() == 0.0 and e.warmup_for() is None                    # cold: the shared engine's own 22-38 s
    e._add_warm(7.0)
    assert e.warmup_for() is None                                               # a little activity is not enough to skip anything
    e._add_warm(10.0)
    assert e.warm_credit() == 17.0 and e.warmup_for() == (5.0, 21.0)            # 22-17 .. 38-17: the same total, partly done already
    e._add_warm(30.0)
    assert e.warmup_for() == (1.0, 2.5)                                         # plenty: only a moment to look alive (it used to be 2-4 s for nothing)


def test_warm_up_belongs_to_one_page_and_fades(make):
    edge = FakeEdge()
    e, _ev = make(edge=edge)
    edge.launches = 1
    e._add_warm(40.0)
    assert e.warm_credit() == 40.0
    edge.launches = 2                                                           # the browser was closed and started again: a cold page
    assert e.warm_credit() == 0.0 and e.warmup_for() is None
    e._add_warm(40.0)
    e._warm_at -= eng.WARM_FRESH_S + 1                                          # nothing happened for a long time
    assert e.warm_credit() == 0.0


# ── keeping the page alive while the traveller types ────────────────────────────────────────────────────────────────
class WarmEdge(FakeEdge):
    endpoint = "http://127.0.0.1:1"


@pytest.fixture()
def warming(make, monkeypatch):
    monkeypatch.setattr(eng, "WARM_POLL_S", 0.02)
    chunks = []

    def build(**kw):
        edge = WarmEdge()
        edge.launches = 1
        e, ev = make(edge=edge, **kw)

        async def chunk():
            chunks.append(time.monotonic())
            e._add_warm(eng.WARM_CHUNK_S)
        monkeypatch.setattr(e, "_warm_chunk", chunk)
        return e, ev, chunks
    return build


def test_the_page_is_kept_alive_while_the_browser_waits_for_the_form_until_it_is_warm(warming):
    e, _ev, chunks = warming(idle=30)
    e._touch(30)                                                                # the fare calendar has just used the browser
    deadline = time.monotonic() + 5
    while e.warm_credit() < eng.WARM_TARGET_S and time.monotonic() < deadline:
        time.sleep(0.02)
    assert e.warm_credit() >= eng.WARM_TARGET_S and len(chunks) == 9            # 9 x 4 s = 36 s, then it stops by itself
    n = len(chunks)
    time.sleep(0.3)
    assert len(chunks) == n                                                     # warm enough: nothing more is done


def test_nothing_is_kept_alive_when_there_is_no_browser_or_a_booking_runs(warming):
    e, _ev, chunks = warming(idle=30)
    time.sleep(0.3)
    assert chunks == []                                                         # no browser was opened: nothing to keep warm (and none is started)
    e._touch(30)
    e.current = "SOMEBOOKING"
    time.sleep(0.3)
    assert chunks == []                                                         # a booking is running: it does its own work
    e.current = None
    e.keep_warm = False
    time.sleep(0.3)
    assert chunks == []                                                         # switched off (debug / tests)


def test_a_piece_that_fails_is_skipped_without_harm(warming, monkeypatch):
    e, _ev, chunks = warming(idle=30)
    calls = {"n": 0}

    async def broken():
        calls["n"] += 1
        raise RuntimeError("the page went away")
    monkeypatch.setattr(e, "_warm_chunk", broken)
    monkeypatch.setattr(eng, "WARM_POLL_S", 0.01)
    e._touch(30)
    time.sleep(0.5)
    assert calls["n"] >= 1 and e.thread.is_alive()                              # still alive, still polite


def test_a_booking_waits_at_most_one_short_piece_for_the_browser(warming):
    """The warm-up piece holds the browser for 4 s at most - so Book now never queues behind a long job."""
    assert eng.WARM_CHUNK_S <= 4.0


# ── the browser stays for the form ──────────────────────────────────────────────────────────────────────────────────
def test_the_browser_is_kept_open_while_the_form_is_filled_but_never_started_for_it(make):
    e, _ev = make()
    e.keep_open(90)
    time.sleep(0.1)
    assert e._close_at == 0.0                                                    # nothing was open: nothing is started or "kept"
    now = time.monotonic()
    e._opened_at = now
    e._close_at = now + 5
    e.keep_open(90)
    time.sleep(0.1)
    assert 85 < e._close_at - time.monotonic() <= 90                             # the form is being typed: the browser waits
    e._opened_at = now - (eng.KEEP_OPEN_MAX_S - 10)
    e._close_at = now + 5
    e.keep_open(90)
    time.sleep(0.1)
    assert e._close_at - now <= 15                                               # ... but not for ever: the cap is the opening time + 8 min


# ── a real booking: warm-up, guard, calendar ────────────────────────────────────────────────────────────────────────
class FakeBrowserCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeTab:
    async def close(self):
        ...


def stub_the_browser_side(monkeypatch, seen):
    monkeypatch.setattr(eng, "CDPBrowser", lambda ep: FakeBrowserCtx())

    async def tidy(b):
        return []
    monkeypatch.setattr(eng.bmod, "tidy", tidy)

    async def open_session(b, steps, session_cls=None):
        return FakeTab(), object()
    monkeypatch.setattr(eng, "open_session", open_session)

    async def guard(ep):
        seen["guard_started"] = seen.get("guard_started", 0) + 1
        seen["order"].append("guard on")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            seen["order"].append("guard off")
            raise
    monkeypatch.setattr(eng.bmod, "guard_windows", guard)

    async def run_psp(b):
        await asyncio.sleep(0.02)                                               # (the real page takes a while: the guard has started)
        seen["order"].append("payment page")
    monkeypatch.setattr(eng, "run_psp", run_psp)

    async def run_booking(sess, req, promo, until, psp=None, choose=None, warmup=None):
        seen["warmup"] = warmup
        seen["order"].append("before the payment page")
        await psp()
        seen["order"].append("after the payment page")
        return BookingResult(success=True, pnr="PNRFAST", kind="record_locator")
    monkeypatch.setattr(eng, "run_booking", run_booking)


def test_a_warm_page_is_not_warmed_up_again_and_the_guard_runs_only_around_the_payment_page(make, monkeypatch):
    seen = {"order": []}
    stub_the_browser_side(monkeypatch, seen)
    edge = FakeEdge()
    e, ev = make(edge=edge, idle=30)
    e.keep_warm = False
    edge.ensured.append(True)                                                   # (the calendar already started this browser: one launch)
    edge.launches = 1
    e._add_warm(40.0)                                                           # the page was kept alive while the form was filled
    _g, ids = e.submit(order("offer1"))
    r = finished(ev, ids[0])
    assert r["status"] == "done" and r["pnr"] == "PNRFAST"
    assert seen["warmup"] == (1.0, 2.5)                                         # a few seconds, not 22-38
    time.sleep(0.1)                                                             # (a cancelled task finishes a moment later)
    assert seen["order"][:3] == ["before the payment page", "guard on", "payment page"]
    assert "guard off" in seen["order"] and seen["guard_started"] == 1          # on for the payment page only, then switched off


def test_a_cold_page_gets_the_full_warm_up(make, monkeypatch):
    seen = {"order": []}
    stub_the_browser_side(monkeypatch, seen)
    e, ev = make(idle=30)
    e.keep_warm = False
    _g, ids = e.submit(order("offer1"))
    finished(ev, ids[0])
    assert seen["warmup"] is None


def test_the_next_booking_right_after_one_finds_the_page_warm(make, monkeypatch):
    seen = {"order": []}
    stub_the_browser_side(monkeypatch, seen)
    e, ev = make(idle=30)
    e.keep_warm = False
    _g, a = e.submit(order("offer1"))
    finished(ev, a[0])
    assert seen["warmup"] is None
    _g, b = e.submit(order("offer1"))
    finished(ev, b[0])
    assert seen["warmup"] is not None and seen["warmup"][1] <= 21               # the first booking's own activity counts


def test_a_booking_does_not_wait_for_the_calendars_work(make, monkeypatch):
    seen = {"order": []}
    stub_the_browser_side(monkeypatch, seen)
    e, ev = make(idle=30)
    e.keep_warm = False
    state = {"cancelled": False}

    async def slow_fares(*a):
        try:
            async with e._lock:                                                 # (the calendar holds the browser)
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
    monkeypatch.setattr(e, "_fares_job", slow_fares)
    from datetime import date
    e.request_fares("DEL", "BOM", date.today())
    time.sleep(0.2)
    t0 = time.monotonic()
    _g, ids = e.submit(order("offer1"))
    finished(ev, ids[0])
    assert time.monotonic() - t0 < 3 and state["cancelled"]


# ── a slow connection ───────────────────────────────────────────────────────────────────────────────────────────────
def test_a_slow_connection_is_told_once_per_booking(make):
    log = logging.getLogger("indigo_client")

    async def runner(bid, req, promo):
        for _ in range(3):
            speed.Net.record("token", 2.5)                                      # (what the real calls do)
            log.info("indigo token      -> 200 (2500 ms) ")
            log.info("indigo search     -> 200 (9000 ms) ")
        return BookingResult(success=True, pnr="PNRSLOW", kind="record_locator")

    e, ev = make(runner)
    _g, ids = e.submit(order())
    finished(ev, ids[0])
    notes = [x["text"] for x in ev if x["type"] == "note"]
    assert notes.count(eng.SLOW_NOTE) == 1 and "connection" in eng.SLOW_NOTE.lower()
    assert not any(w in eng.SLOW_NOTE.lower() for w in ("browser", "token", "api", "akamai", "edge"))


def test_a_good_connection_is_never_called_slow(make):
    log = logging.getLogger("indigo_client")

    async def runner(bid, req, promo):
        speed.Net.record("token", 0.4)
        log.info("indigo token      -> 200 (400 ms) ")
        return BookingResult(success=True, pnr="PNROK", kind="record_locator")

    e, ev = make(runner)
    _g, ids = e.submit(order())
    finished(ev, ids[0])
    assert eng.SLOW_NOTE not in [x["text"] for x in ev if x["type"] == "note"]


def test_a_slow_line_gets_more_working_time_but_a_hang_is_still_stopped(make, monkeypatch):
    monkeypatch.setattr(eng, "BOOKING_TIMEOUT_S", 0.6)

    async def runner(bid, req, promo):
        await asyncio.sleep(0.9)                                                 # more than 0.6 s of work
        return BookingResult(success=True, pnr="PNRLATE", kind="record_locator")

    e, ev = make(runner)
    _g, a = e.submit(order())
    assert "longer than usual" in finished(ev, a[0])["error"]                    # a normal connection: stopped at the limit
    speed.Net.ratio = 2.5
    _g, b = e.submit(order())
    assert finished(ev, b[0])["pnr"] == "PNRLATE"                                # a slow one: allowed up to twice as long
    speed.Net.ratio = 2.5

    async def hang(bid, req, promo):
        await asyncio.sleep(60)
    e2, ev2 = make(hang)
    _g, c = e2.submit(order())
    assert "longer than usual" in finished(ev2, c[0])["error"]                   # ... but never for ever


# ── the stage clock ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_where_the_time_went_is_written_for_the_debug_story(make, caplog):
    async def runner(bid, req, promo):
        e._stage(bid, "warmup")
        await asyncio.sleep(0.05)
        e._stage(bid, "search")
        await asyncio.sleep(0.05)
        return BookingResult(success=True, pnr="PNRT", kind="record_locator")

    e, ev = make(runner)
    with caplog.at_level(logging.INFO, logger="engine"):
        _g, ids = e.submit(order())
        finished(ev, ids[0])
    line = next(r.getMessage() for r in caplog.records if "timing:" in r.getMessage())
    assert "warmup" in line and "search" in line and "total" in line
