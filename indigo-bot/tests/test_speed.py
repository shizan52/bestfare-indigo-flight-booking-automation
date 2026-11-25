"""Faster without being careless: waits that only add time are gone, waits that depend on the network grow with a slow connection,
and a booking that has already kept the page alive does not warm it up a second time."""
import time

import pytest

import indigo_client as ic
import speed


@pytest.fixture(autouse=True)
def fresh_net():
    speed.Net.reset()
    yield
    speed.Net.reset()


# ── measuring the connection ────────────────────────────────────────────────────────────────────────────────────────
def test_a_good_connection_changes_nothing():
    assert speed.Net.scale() == 1.0 and not speed.Net.slow() and speed.scaled(45) == 45
    for name, secs in (("token", 0.4), ("search", 3.9), ("sell", 2.0), ("addpax", 1.2)):
        speed.Net.record(name, secs)
    assert speed.Net.scale() == 1.0 and not speed.Net.slow() and speed.scaled(45) == 45


def test_a_slow_connection_stretches_the_waits_but_never_beyond_three_times():
    for name, secs in (("token", 2.0), ("search", 14.0), ("sell", 8.0), ("addpax", 5.0)):
        speed.Net.record(name, secs)
    assert 1.8 <= speed.Net.scale() <= 3.0 and speed.Net.slow()
    assert 45 < speed.scaled(45) <= 135
    for _ in range(20):
        speed.Net.record("token", 60.0)                                        # absurdly slow: still capped
    assert speed.Net.scale() == 3.0 and speed.scaled(10) == 30


def test_one_slow_answer_alone_is_not_called_a_slow_connection():
    speed.Net.record("search", 20.0)
    assert not speed.Net.slow()


def test_the_connection_recovers_when_it_speeds_up_again():
    for _ in range(4):
        speed.Net.record("token", 3.0)
    assert speed.Net.slow()
    for _ in range(12):
        speed.Net.record("token", 0.4)
    assert speed.Net.scale() < 1.2 and not speed.Net.slow()


def test_calls_without_a_baseline_and_nonsense_are_ignored():
    speed.Net.record("calendar", 9.0)
    speed.Net.record("token", -1)
    speed.Net.record("token", 0)
    assert speed.Net.samples == 0 and speed.Net.scale() == 1.0


# ── the session records how long its calls take and waits longer on a slow line ────────────────────────────────────
class Tab:
    def __init__(self, seconds, status=200):
        self.seconds, self.status, self.timeouts = seconds, status, []

    async def evaluate_json(self, js, timeout=0):
        import asyncio
        self.timeouts.append(timeout)
        await asyncio.sleep(self.seconds)
        return {"status": self.status, "json": {}}


class Human:
    def __init__(self):
        self.waits = []

    async def wander(self, secs=0):
        self.waits.append(secs)


@pytest.fixture()
def pace(monkeypatch):
    monkeypatch.setattr(ic.Pace, "min_gap", 0.0)
    monkeypatch.setattr(ic.Pace, "jitter", 0.0)
    monkeypatch.setattr(ic.Pace, "last_call", 0.0)


async def test_every_answered_call_is_measured_and_the_limit_grows_with_the_speed(pace):
    tab = Tab(0.05)
    s = ic.IndigoSession(tab, Human(), [])
    await s.call("token", ic.U_TOKEN, "POST", ic.UK_SKY, {})
    assert tab.timeouts == [60.0] and speed.Net.samples == 1
    speed.Net.ratio = 3.0
    await s.call("search", ic.U_SEARCH, "POST", ic.UK_FLIGHT, {})
    assert tab.timeouts[-1] == 150.0                                           # 60 x 3, capped at 150
    speed.Net.ratio = 1.0


async def test_a_refused_call_is_not_counted_as_the_speed_of_the_line(pace):
    s = ic.IndigoSession(Tab(0.01, status=500), Human(), [])
    with pytest.raises(ic.IndigoBlocked):
        await s.call("search", ic.U_SEARCH, "POST", ic.UK_FLIGHT, {})
    assert speed.Net.samples == 0


# ── pauses: only what is still missing ──────────────────────────────────────────────────────────────────────────────
async def test_the_pause_before_a_call_counts_the_time_that_has_already_passed(monkeypatch):
    monkeypatch.setattr(ic.random, "uniform", lambda a, b: b)                 # the longest pause asked for
    h = Human()
    s = ic.IndigoSession(Tab(0), h, [])
    ic.Pace.last_call = time.monotonic() - 10                                  # the last call was 10 s ago (a slow answer, a choice)
    await s.human_pause(2, 4)
    assert h.waits == []                                                       # nothing is waited for: 10 s > 4 s
    ic.Pace.last_call = time.monotonic() - 1.5
    await s.human_pause(2, 4)
    assert len(h.waits) == 1 and 2.3 < h.waits[0] <= 2.5                       # 4 s asked, 1.5 s have passed: 2.5 s are still missing
    ic.Pace.last_call = time.monotonic()
    await s.human_pause(1, 3)
    assert 2.9 < h.waits[-1] <= 3.0                                            # right after a call: the whole pause, as before


# ── the warm-up ─────────────────────────────────────────────────────────────────────────────────────────────────────
from test_flights import REAL, REQ, booking_session  # noqa: E402  (the fake IndiGo of the flight tests)


async def test_the_page_is_warmed_up_for_22_to_38_seconds_unless_the_caller_says_it_already_is(monkeypatch):
    sess, _sent, waits = booking_session(monkeypatch, REAL)
    monkeypatch.setattr(ic.random, "uniform", lambda a, b: a)
    await ic.run_booking(sess, REQ, "6ETST001", "search")
    assert waits[0] == 22.0                                                    # the original, unchanged default
    sess2, _s2, waits2 = booking_session(monkeypatch, REAL)
    monkeypatch.setattr(ic.random, "uniform", lambda a, b: b)
    await ic.run_booking(sess2, REQ, "6ETST001", "search", warmup=(2.0, 4.0))
    assert waits2[0] == 4.0


# ── the record of the booking ───────────────────────────────────────────────────────────────────────────────────────
from test_psp_step import ITIN_EMPTY, ITIN_OK, make_session, req  # noqa: E402


async def test_a_record_that_shows_up_a_moment_late_does_not_cost_a_second_trip_through_the_payment_page(monkeypatch):
    monkeypatch.setattr(ic, "RECORD_GAP_S", 0.0)
    calls = []
    _ic, sess = make_session(monkeypatch, ITIN_EMPTY, calls)
    answers = [ITIN_EMPTY, ITIN_EMPTY, ITIN_OK]
    real_call = sess.call

    async def call(name, url, method, key, body):
        if name == "itinerary":
            calls.append(name)
            return {"status": 200, "json": answers.pop(0) if answers else ITIN_OK}
        return await real_call(name, url, method, key, body)

    monkeypatch.setattr(sess, "call", call)
    ran = {"psp": 0}

    async def psp_():
        ran["psp"] += 1
    res = await ic.run_booking(sess, req(ic), "6ETST001", "payment", psp=psp_)
    assert res.pnr == "MYBBQP" and res.kind == "record_locator" and ran["psp"] == 1       # the payment page was gone through ONCE
    assert calls.count("itinerary") == 3


async def test_a_record_that_never_comes_still_ends_in_the_order_fallback(monkeypatch):
    monkeypatch.setattr(ic, "RECORD_GAP_S", 0.0)
    calls = []
    _ic, sess = make_session(monkeypatch, ITIN_EMPTY, calls)

    async def psp_(): ...
    res = await ic.run_booking(sess, req(ic), "PROMO2", "payment", psp=psp_)
    assert res.success and res.kind == "order_id" and res.psp_failed is True
    assert calls.count("itinerary") == 6 and calls[-1] == "pay-init"           # (3 quick reads after each of the two trips)
