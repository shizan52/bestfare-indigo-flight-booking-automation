"""IndiGo not taking a booking: the engine deals with it BY ITSELF, calmly. The traveller never sees a timer ("wait 15 minutes"), the app is never locked, and
a lock-out left behind by an older version is wiped at the first start. (Why: a single refusal used to lock the whole app for 15 minutes - up to two hours when
agents who restart the app all day kept inheriting the 'blocks in a row' counter.)"""
import logging
import time

import pytest

import browser as bmod
import engine as eng
import extension
from indigo_client import BookingResult, IndigoBlocked, IndigoSession, Pace
from test_engine import Events, FakeEdge, finished, make, order  # noqa: F401

TINY = ((False, (0.01, 0.02), (1.0, 2.0)), (True, (0.01, 0.02), (3.0, 4.0)), (True, (0.01, 0.02), (5.0, 6.0)))


@pytest.fixture()
def calm(monkeypatch):
    """The recovery steps without the waiting: the same plan, a few milliseconds each, and a computer that is online."""
    monkeypatch.setattr(eng, "RECOVERY", TINY)
    monkeypatch.setattr(eng, "NET_POLL_S", 0.01)

    async def online(self):
        return True
    monkeypatch.setattr(eng.Engine, "_online", online)


def refused(kind="fetch", name="search"):
    return IndigoBlocked(f"{name}: request blocked at the edge (TypeError: Failed to fetch)", kind=kind, name=name)


def notes(ev, bid):
    return [x["text"] for x in ev if x["type"] == "note" and x["id"] == bid]


# ── the start of a booking ───────────────────────────────────────────────────────────────────────────────────────────
def test_a_refusal_at_the_start_is_tried_again_by_itself_and_the_booking_goes_through(make, store, calm):
    seen, n = [], {"calls": 0}

    async def runner(bid, req, promo):
        n["calls"] += 1
        seen.append(box["e"]._warm_override)
        if n["calls"] == 1:
            raise refused()
        return BookingResult(success=True, pnr="SECOND", kind="record_locator")

    box = {}
    e, ev = make(runner)
    box["e"] = e
    _g, ids = e.submit(order())
    r = finished(ev, ids[0])
    assert r["status"] == "done" and r["pnr"] == "SECOND" and n["calls"] == 2
    assert seen == [None, (1.0, 2.0)]                                                   # the second try warmed up longer than the first
    assert eng.CALM_NOTE_FIRST in notes(ev, ids[0])
    assert not any(x["type"] == "cooldown" for x in ev) and Pace.cooldown_left() == 0 and store.setting("pace") is None
    assert "warmup" in [x["stage"] for x in ev if x["type"] == "stage" and x["id"] == ids[0]]      # the step list goes back to "Getting ready" meanwhile


def test_the_second_step_closes_the_browser_and_starts_a_fresh_one(make, calm):
    edge, box, n = FakeEdge(), {}, {"calls": 0}

    async def runner(bid, req, promo):
        await box["e"]._browser()
        n["calls"] += 1
        if n["calls"] <= 2:
            raise refused()
        return BookingResult(success=True, pnr="THIRD", kind="record_locator")

    e, ev = make(runner, edge=edge, idle=60.0, after=0.0)
    box["e"] = e
    _g, ids = e.submit(order())
    assert finished(ev, ids[0])["status"] == "done" and n["calls"] == 3
    assert edge.launches == 2                                                           # tries 1 and 2 used one page; try 3 came after a close and a fresh start
    texts = notes(ev, ids[0])
    assert eng.CALM_NOTE_FIRST in texts and eng.CALM_NOTE_AGAIN in texts


def test_a_denied_home_page_is_never_asked_again_on_the_same_page(make, calm):
    edge, box, n = FakeEdge(), {}, {"calls": 0}

    async def runner(bid, req, promo):
        await box["e"]._browser()
        n["calls"] += 1
        if n["calls"] == 1:
            raise bmod.BrowserDenied("IndiGo is not responding well right now.")
        return BookingResult(success=True, pnr="FRESH1", kind="record_locator")

    e, ev = make(runner, edge=edge, idle=60.0, after=0.0)
    box["e"] = e
    _g, ids = e.submit(order())
    assert finished(ev, ids[0])["status"] == "done" and n["calls"] == 2
    assert edge.launches == 2                                                           # the very first step already closed the page and started a new one


def test_an_explicit_slow_down_is_given_rests_twice_as_long(make, calm, monkeypatch):
    e, _ev = make(None)
    monkeypatch.setattr(eng.random, "uniform", lambda a, b: a)

    async def took(kind):
        t0 = time.monotonic()
        await e._calm("B1", eng.Refusal(kind), (False, (0.2, 0.2), (1.0, 2.0)), True)
        return time.monotonic() - t0
    normal = e.call(took("fetch")).result(5)
    slow = e.call(took("429")).result(5)
    assert 0.18 <= normal < 0.35 and 0.38 <= slow < 0.65


def test_the_patience_is_bounded_the_end_is_a_plain_message_and_nothing_is_locked(make, store, calm):
    calls = []

    async def runner(bid, req, promo):
        calls.append(bid)
        if len(calls) <= 4:
            raise refused()
        return BookingResult(success=True, pnr="NEXT01", kind="record_locator")

    e, ev = make(runner)
    _g, a = e.submit(order())
    r = finished(ev, a[0])
    assert r["status"] == "failed" and len(calls) == 4                                  # the first try and three calm ones - then it stops
    assert "press Book again" in r["error"] and not any(c.isdigit() for c in r["error"])      # no "wait 15 minutes", no number at all
    assert Pace.cooldown_left() == 0 and store.setting("pace") is None
    assert e._patient()                                                                 # the next one starts more patiently ...
    _g, b = e.submit(order())
    assert finished(ev, b[0])["status"] == "done"                                       # ... but is never turned away


def test_a_denied_home_page_and_a_session_that_would_not_start_are_tried_again_too(make, calm):
    n = {"calls": 0}

    async def runner(bid, req, promo):
        n["calls"] += 1
        if n["calls"] == 1:
            raise bmod.BrowserDenied("IndiGo is not responding well right now.")
        if n["calls"] == 2:
            return BookingResult(success=False, error="could not start a session (HTTP 400)")
        return BookingResult(success=True, pnr="GOOD01", kind="record_locator")

    e, ev = make(runner)
    _g, ids = e.submit(order())
    assert finished(ev, ids[0])["status"] == "done" and n["calls"] == 3


def test_the_real_booking_path_hands_the_longer_warmup_to_the_second_try(make, calm, monkeypatch):
    """Through Engine._book_once itself (only IndiGo and the browser are stand-ins): the first try uses the usual warm-up, the second one the longer one."""
    from test_engine import FakeBrowser
    seen = []

    class Tab:
        async def close(self):
            pass

    async def fake_open_session(b, steps, session_cls=None):
        return Tab(), object()

    async def fake_tidy(b):
        pass

    async def fake_run_booking(sess, req, promo, until, psp=None, choose=None, warmup=None):
        seen.append(warmup)
        if len(seen) == 1:
            raise refused()
        return BookingResult(success=True, pnr="REAL01", kind="record_locator")

    monkeypatch.setattr(eng, "CDPBrowser", FakeBrowser)
    monkeypatch.setattr(eng, "open_session", fake_open_session)
    monkeypatch.setattr(eng.bmod, "tidy", fake_tidy)
    monkeypatch.setattr(eng, "run_booking", fake_run_booking)
    e, ev = make(None, edge=FakeEdge(), idle=60.0, after=0.0)
    _g, ids = e.submit(order())
    assert finished(ev, ids[0])["status"] == "done" and len(seen) == 2
    assert seen[0] == eng.WARM_CAUTIOUS and seen[1] == (1.0, 2.0)                       # (a new installation: the longer range first; then the recovery's own)
    assert e._warm_override is None                                                     # used up: it never leaks into the next booking


def test_a_page_that_did_not_answer_at_the_start_is_a_refusal_with_a_fresh_start(make, calm):
    from indigo_client import IndigoError
    edge, box, n = FakeEdge(), {}, {"calls": 0}

    async def runner(bid, req, promo):
        await box["e"]._browser()
        n["calls"] += 1
        if n["calls"] == 1:
            raise IndigoError("browser error during token: the page did not answer")
        return BookingResult(success=True, pnr="PAGE01", kind="record_locator")

    e, ev = make(runner, edge=edge, idle=60.0, after=0.0)
    box["e"] = e
    _g, ids = e.submit(order())
    assert finished(ev, ids[0])["status"] == "done" and n["calls"] == 2 and edge.launches == 2


# ── the few words the server keeps beside a booking (the admin sees them; the traveller never does) ──────────────────────
class Gate:
    """A gate that remembers what the engine told the server."""
    def __init__(self):
        self.told = []

    def begin(self, rec):
        import gate as gatemod
        return gatemod.Pass("TESTCODE1", "cloud-1", 1)

    def finish(self, cloud_id, outcome, pnr, reason):
        self.told.append((outcome, pnr, reason))
        return {"refunded": outcome == "failed"}


def test_the_server_is_told_how_often_indigo_had_to_be_asked_again(make, calm):
    n, g = {"calls": 0}, Gate()

    async def runner(bid, req, promo):
        n["calls"] += 1
        if n["calls"] == 1:
            raise refused("403", "search")
        if n["calls"] == 2:
            raise refused("fetch", "search")
        return BookingResult(success=True, pnr="LATE01", kind="record_locator")

    e, ev = make(runner, gate=g)
    _g, ids = e.submit(order())
    finished(ev, ids[0])
    for _ in range(100):
        if g.told:
            break
        time.sleep(0.03)
    assert g.told == [("done", "LATE01", "worked on try 3 (403@search, fetch@search)")]


def test_a_booking_that_never_worked_tells_the_server_in_a_few_words_and_the_traveller_in_plain_ones(make, calm):
    g = Gate()

    async def runner(bid, req, promo):
        raise refused("429", "search")

    e, ev = make(runner, gate=g)
    _g, ids = e.submit(order())
    r = finished(ev, ids[0])
    for _ in range(100):
        if g.told:
            break
        time.sleep(0.03)
    outcome, pnr, reason = g.told[0]
    assert outcome == "failed" and pnr is None and reason.startswith("4 tries (429@search): IndiGo is not responding well") and len(reason) <= 80
    assert "429" not in r["error"] and "tries" not in r["error"]                        # none of it reaches the screen


def test_a_booking_that_needed_nothing_adds_no_note(make):
    g = Gate()

    async def runner(bid, req, promo):
        return BookingResult(success=True, pnr="PLAIN1", kind="record_locator")

    e, ev = make(runner, gate=g)
    _g, ids = e.submit(order())
    finished(ev, ids[0])
    for _ in range(100):
        if g.told:
            break
        time.sleep(0.03)
    assert g.told == [("done", "PLAIN1", "")]


# ── later in the booking: never start over (the traveller's choice would be lost) ─────────────────────────────────────────
def test_a_refusal_after_the_flight_was_chosen_is_not_answered_by_starting_over(make, calm):
    log = logging.getLogger("indigo_client")
    calls = []

    async def runner(bid, req, promo):
        calls.append(bid)
        log.info("indigo token      -> 200 (300 ms) ")
        log.info("indigo search     -> 200 (900 ms) ")
        raise refused("fetch", "sell")

    e, ev = make(runner)
    _g, ids = e.submit(order())
    r = finished(ev, ids[0])
    assert r["status"] == "failed" and len(calls) == 1 and "press Book again" in r["error"]


# ── no internet is not IndiGo's refusal ──────────────────────────────────────────────────────────────────────────────────
def test_a_missing_internet_connection_is_waited_for_and_costs_no_recovery_step(make, monkeypatch):
    monkeypatch.setattr(eng, "RECOVERY", TINY)
    monkeypatch.setattr(eng, "NET_POLL_S", 0.01)
    state = {"probes": 0}

    async def online(self):
        state["probes"] += 1
        return state["probes"] >= 4
    monkeypatch.setattr(eng.Engine, "_online", online)
    seen, n, box = [], {"calls": 0}, {}

    async def runner(bid, req, promo):
        n["calls"] += 1
        seen.append(box["e"]._warm_override)
        if n["calls"] == 1:
            raise refused()
        return BookingResult(success=True, pnr="BACK01", kind="record_locator")

    e, ev = make(runner)
    box["e"] = e
    _g, ids = e.submit(order())
    assert finished(ev, ids[0])["status"] == "done" and n["calls"] == 2
    texts = notes(ev, ids[0])
    assert "Waiting for the internet connection…" in texts and "The connection is back — carrying on." in texts
    assert eng.CALM_NOTE_FIRST not in texts and seen == [None, None]                    # none of the patience was spent


def test_without_any_connection_it_ends_with_a_plain_message_about_the_connection(make, monkeypatch):
    monkeypatch.setattr(eng, "RECOVERY", TINY)
    monkeypatch.setattr(eng, "NET_POLL_S", 0.005)
    monkeypatch.setattr(eng, "NET_WAIT_S", 0.03)

    async def offline(self):
        return False
    monkeypatch.setattr(eng.Engine, "_online", offline)

    async def runner(bid, req, promo):
        raise refused()

    e, ev = make(runner)
    _g, ids = e.submit(order())
    r = finished(ev, ids[0])
    assert r["status"] == "failed" and "internet connection" in r["error"] and "wait" not in r["error"].lower()


async def test_behind_a_proxy_a_failed_direct_connection_does_not_mean_offline(monkeypatch, store):
    e = eng.Engine(store, Events(), edge=FakeEdge())

    async def no_route(*a, **k):
        raise OSError("no direct route")
    monkeypatch.setattr(eng.asyncio, "open_connection", no_route)
    monkeypatch.setattr(eng, "_proxy_in_use", lambda: False)
    assert await e._online() is False                                                   # no proxy, no route: really offline
    monkeypatch.setattr(eng, "_proxy_in_use", lambda: True)
    assert await e._online() is True                                                    # a proxy is in use: the browser may well be online - never wait for nothing


def test_a_proxy_is_noticed_from_the_environment(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.office:3128")
    assert eng._proxy_in_use() is True


# ── no lock-out, ever ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_lockout_timer_left_by_an_older_version_is_wiped_at_the_first_start(store):
    store.set_setting("pace", {"until": time.time() + 7000, "at": time.time(), "strikes": 4})
    Pace.cooldown_until = time.monotonic() + 7000
    Pace.strikes = 4
    e = eng.Engine(store, Events(), edge=FakeEdge())
    e.start()
    try:
        assert store.setting("pace") is None and Pace.cooldown_left() == 0 and Pace.strikes == 0 and Pace.lockout is False
    finally:
        e.shutdown(close_browser=False)


def test_the_shared_engines_breaker_cannot_close_the_door_in_this_app(store):
    e = eng.Engine(store, Events(), edge=FakeEdge())
    Pace.trip()
    Pace.trip()
    assert Pace.cooldown_left() == 0 and Pace.strikes == 0                              # (the bot's old behaviour is still there when the lock-out is on)
    Pace.lockout = True
    Pace.trip()
    assert Pace.cooldown_left() > 800 and Pace.strikes == 1
    del e


# ── a new installation, and the patient start after a refusal ────────────────────────────────────────────────────────────
def test_a_new_installation_warms_up_longer_until_indigo_has_answered_a_search(make, store):
    log = logging.getLogger("indigo_client")

    async def runner(bid, req, promo):
        log.info("indigo token      -> 200 (300 ms) ")
        log.info("indigo search     -> 200 (900 ms) ")
        return BookingResult(success=True, pnr="SEEN01", kind="record_locator")

    e, ev = make(runner)
    assert not e._seasoned and e.warmup_for() == eng.WARM_CAUTIOUS                      # never seen by IndiGo: the longer range
    _g, ids = e.submit(order())
    finished(ev, ids[0])
    assert e._seasoned and store.setting(eng.SEASONED_KEY) and e.warmup_for() is None   # known now: the usual (full for a cold page)
    assert eng.Engine(store, Events(), edge=FakeEdge())._seasoned                        # ... and it is remembered


def test_an_installation_with_a_booking_behind_it_is_known_from_the_start(store):
    bid = store.add_booking(grp="g", origin="DEL", dest="BOM", travel_date="2026-12-01", offer="offer1",
                            travellers=[{"first": "A", "last": "B", "gender": "M"}], phone="9876543210", email="a@b.co")
    store.update(bid, status="done", pnr="OLD001")
    assert eng.Engine(store, Events(), edge=FakeEdge())._seasoned


def test_the_patient_start_after_a_refusal_ends_when_indigo_answers_again(make):
    e, _ev = make(None)
    e._seasoned = True
    assert not e._patient()
    e._cautious_until = time.monotonic() + 100
    assert e._patient() and e._warm_target() == eng.WARM_CAUTIOUS[1]
    e._warm_launch, e._warm_at = getattr(e.edge, "launches", 0), time.monotonic()
    e._warm_credit = 0.0
    assert e.warmup_for() == eng.WARM_CAUTIOUS
    e._warm_credit = 20.0
    lo, hi = e.warmup_for()
    assert 9 <= lo <= 11 and 25 <= hi <= 27                                              # what is still missing of the longer range
    e._warm_credit = 50.0
    assert e.warmup_for() == (3.0, 6.0)
    e._mark_seasoned()
    assert not e._patient() and e._warm_target() == eng.WARM_TARGET_S


# ── the calls late in a booking repeat themselves calmly (extension.ExtensionSession.call) ────────────────────────────────
class Human:
    def __init__(self):
        self.waited = []

    async def wander(self, seconds):
        self.waited.append(round(seconds, 1))


def session(monkeypatch, failures):
    """An ExtensionSession whose shared call() fails with the given kinds first and then answers."""
    human, asked, kinds = Human(), [], list(failures)

    async def shared(self, name, url, method, user_key, body):
        asked.append(name)
        if kinds:
            raise IndigoBlocked("refused", kind=kinds.pop(0), name=name)
        return {"status": 200, "json": {}}
    monkeypatch.setattr(IndigoSession, "call", shared)
    monkeypatch.setattr(extension, "LATE_CALM_S", (0.5, 0.5))
    return extension.ExtensionSession(object(), human, []), human, asked


async def test_adding_the_travellers_is_repeated_calmly_whatever_the_refusal_was(monkeypatch):
    s, human, asked = session(monkeypatch, ["fetch", "fetch"])
    assert (await s.call("addpax", "u", "POST", "k", {}))["status"] == 200
    assert asked == ["addpax"] * 3 and human.waited == [0.5, 1.0]                       # a calm moment first, a longer one the second time


async def test_it_does_not_go_on_for_ever(monkeypatch):
    s, human, asked = session(monkeypatch, ["fetch"] * 5)
    with pytest.raises(IndigoBlocked):
        await s.call("addpax", "u", "POST", "k", {})
    assert len(asked) == 1 + extension.LATE_TRIES


async def test_the_fare_hold_is_repeated_only_when_the_request_provably_never_arrived(monkeypatch):
    s, _h, asked = session(monkeypatch, ["403"])
    assert (await s.call("sell", "u", "POST", "k", {}))["status"] == 200 and asked == ["sell", "sell"]
    s, _h, asked = session(monkeypatch, ["fetch"])                                      # it might have been processed: never held twice
    with pytest.raises(IndigoBlocked):
        await s.call("sell", "u", "POST", "k", {})
    assert asked == ["sell"]


async def test_reading_the_booking_back_is_repeated_and_the_start_of_a_booking_never_is_here(monkeypatch):
    s, _h, asked = session(monkeypatch, ["5xx"])
    assert (await s.call("itinerary", "u", "POST", "k", {}))["status"] == 200 and asked == ["itinerary", "itinerary"]
    for name in ("token", "search", "pay-init"):                                        # (the start is answered by the engine starting over, in a fresh state)
        s, _h, asked = session(monkeypatch, ["429"])
        with pytest.raises(IndigoBlocked):
            await s.call(name, "u", "POST", "k", {})
        assert asked == [name]
