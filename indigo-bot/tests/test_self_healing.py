"""When something breaks on its own, the bot repairs what it can, retries instead of refunding, and tells the admins
only what needs a human - once, in plain words."""
import asyncio
import importlib

import pytest

from conftest import booking

pytestmark = pytest.mark.asyncio


# ── a fake Edge for the doctor ─────────────────────────────────────────────
class FakeEdge:
    def __init__(self, pages):
        self.tabs = [dict(p) for p in pages]
        self.closed, self.opened = [], []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def version(self):
        return {"Browser": "Edg/157"}

    async def pages(self):
        return [dict(p) for p in self.tabs]

    async def new_page(self, url):
        p = {"id": f"NEW{len(self.opened)}", "url": url, "title": "Book Domestic and International Flights"}
        self.tabs.append(p)
        self.opened.append(url)
        return p

    async def close_page(self, tid):
        if len(self.tabs) <= 1:                      # real Edge quits when its last tab is closed (seen in a live drill)
            raise RuntimeError("Edge quit: the last tab was closed")
        self.closed.append(tid)
        self.tabs = [p for p in self.tabs if p["id"] != tid]


HOME = {"id": "H", "url": "https://www.goindigo.in/", "title": "Book Domestic and International Flights"}


@pytest.fixture()
def doc(cfg, monkeypatch):
    import doctor
    import indigo_client
    importlib.reload(doctor)

    async def fast_sleep(_s):
        return None
    monkeypatch.setattr(doctor.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(indigo_client.Pace, "cooldown_until", 0.0)
    calls = {"start": 0, "stop": 0}

    def start_edge():
        calls["start"] += 1
        return True, ""

    def stop_stuck_edge():
        calls["stop"] += 1
        return 0
    monkeypatch.setattr(doctor, "start_edge", start_edge)
    monkeypatch.setattr(doctor, "stop_stuck_edge", stop_stuck_edge)
    doctor.calls = calls
    return doctor


def use(doctor, monkeypatch, edge, up=True):
    monkeypatch.setattr(doctor, "CDPBrowser", lambda _ep: edge)

    async def wait_cdp(_ep, _seconds):
        return up() if callable(up) else up
    monkeypatch.setattr(doctor, "wait_cdp", wait_cdp)


async def test_left_over_tabs_are_closed_and_the_home_tab_is_never_touched(doc, monkeypatch):
    edge = FakeEdge([HOME, {"id": "P", "url": "https://www.goindigo.in/payment.html?x=1", "title": "Payment"},
                     {"id": "B", "url": "https://netbanking.hdfcbank.com/x", "title": "HDFC"},
                     {"id": "Z", "url": "about:blank", "title": ""}])
    use(doc, monkeypatch, edge)
    res = await doc.heal("http://x", force=True)
    assert res["ok"] is True and sorted(edge.closed) == ["B", "P", "Z"] and "H" not in edge.closed
    assert res["actions"] == ["closed 3 left-over tabs"]


async def test_the_last_tab_is_never_closed_because_that_would_quit_edge(doc, monkeypatch):
    """Live drill: the IndiGo tab was gone and only a blank tab was left. Closing it made Edge exit."""
    edge = FakeEdge([{"id": "Z", "url": "about:blank", "title": ""}])
    use(doc, monkeypatch, edge)
    assert await doc.tidy(edge) == [] and edge.closed == []                   # no home tab: nothing may be closed
    res = await doc.heal("http://x", force=True)
    assert res["ok"] is True and "Edge quit" not in res["detail"]
    assert edge.opened == ["https://www.goindigo.in/"] and edge.closed == ["Z"]      # home opened FIRST, then the blank closed
    assert len(edge.tabs) == 1 and "goindigo.in" in edge.tabs[0]["url"]


async def test_edge_vanishing_in_the_middle_of_a_repair_is_reported_not_raised(doc, monkeypatch):
    class Dying(FakeEdge):
        async def new_page(self, url):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")      # what the debugging port answers as Edge exits
    use(doc, monkeypatch, Dying([{"id": "Z", "url": "about:blank", "title": ""}]))
    res = await doc.heal("http://x", force=True)
    assert res["ok"] is False and res["detail"] == "Edge stopped answering while it was being repaired."


async def test_a_missing_home_tab_is_opened_again(doc, monkeypatch):
    edge = FakeEdge([{"id": "Z", "url": "about:blank", "title": ""}])
    use(doc, monkeypatch, edge)
    res = await doc.heal("http://x", force=True)
    assert res["ok"] is True and edge.opened == ["https://www.goindigo.in/"]
    assert "opened the IndiGo home page again" in res["actions"]


async def test_without_the_opt_in_task_edge_is_never_closed_only_reported(doc, monkeypatch, cfg):
    monkeypatch.setattr(cfg, "EDGE_AUTOSTART", True)
    monkeypatch.setattr(doc, "can_start_edge", lambda: False)
    stopped = []
    monkeypatch.setattr(doc, "stop_stuck_edge", lambda: stopped.append(1) or 1)
    use(doc, monkeypatch, FakeEdge([HOME]), up=False)
    res = await doc.heal("http://x", force=True)
    assert res["ok"] is False and stopped == [] and doc.calls["start"] == 0
    assert "automatic restart is not set up" in res["detail"]


async def test_edge_that_stopped_answering_is_restarted_through_the_desktop_task(doc, monkeypatch, cfg):
    monkeypatch.setattr(cfg, "EDGE_AUTOSTART", True)
    monkeypatch.setattr(doc, "can_start_edge", lambda: True)
    edge = FakeEdge([HOME])
    answers = iter([False, False, True])                                  # down, still down, then up after the restart
    use(doc, monkeypatch, edge, up=lambda: next(answers, True))
    monkeypatch.setattr(doc, "stop_stuck_edge", lambda: 1)
    res = await doc.heal("http://x", force=True)
    assert res["ok"] is True and doc.calls["start"] == 1
    assert res["actions"][:2] == ["closed an Edge window that had stopped answering", "started Edge again"]
    assert doc.describe(res).startswith("Closed an Edge window")


async def test_an_edge_that_cannot_be_started_is_reported_plainly(doc, monkeypatch, cfg):
    monkeypatch.setattr(cfg, "EDGE_AUTOSTART", True)
    monkeypatch.setattr(doc, "can_start_edge", lambda: True)
    use(doc, monkeypatch, FakeEdge([HOME]), up=False)
    monkeypatch.setattr(doc, "start_edge", lambda: (False, "The user account is not logged on"))
    res = await doc.heal("http://x", force=True)
    assert res["ok"] is False and "logged in on the server's desktop" in res["detail"]


async def test_with_autostart_off_nothing_is_started(doc, monkeypatch, cfg):
    monkeypatch.setattr(cfg, "EDGE_AUTOSTART", False)
    use(doc, monkeypatch, FakeEdge([HOME]), up=False)
    res = await doc.heal("http://x", force=True)
    assert res["ok"] is False and doc.calls["start"] == 0 and "switched off" in res["detail"]


async def test_a_blocked_home_tab_is_replaced_after_the_cooldown_but_not_during_it(doc, monkeypatch):
    import indigo_client
    blocked = {"id": "H", "url": "https://www.goindigo.in/", "title": "Access Denied"}
    edge = FakeEdge([blocked])
    use(doc, monkeypatch, edge)
    monkeypatch.setattr(indigo_client.Pace, "cooldown_until", __import__("time").monotonic() + 600)
    res = await doc.heal("http://x", force=True)
    assert res["ok"] is False and "cool-down" in res["detail"] and not edge.opened
    monkeypatch.setattr(indigo_client.Pace, "cooldown_until", 0.0)
    res = await doc.heal("http://x", force=True)
    assert res["ok"] is True and edge.closed == ["H"] and "replaced a blocked IndiGo tab with a fresh one" in res["actions"]


async def test_repairs_are_throttled_unless_an_admin_forces_one(doc, monkeypatch):
    use(doc, monkeypatch, FakeEdge([HOME]))
    first = await doc.heal("http://x")
    second = await doc.heal("http://x")
    forced = await doc.heal("http://x", force=True)
    assert first["ok"] is True and second["ok"] is None and forced["ok"] is True


async def test_the_doctor_never_touches_a_browser_that_a_booking_is_using(cfg, monkeypatch):
    import automation
    importlib.reload(automation)
    called = []

    async def heal(*a, **k):
        called.append(a)
        return {"ok": True, "actions": [], "detail": ""}
    monkeypatch.setattr(automation.doctor, "heal", heal)
    lock = automation._lock(cfg.CDP_ENDPOINTS[0])
    async with lock:                                                       # a booking holds the browser
        res = await automation.heal("watchdog")
    assert res["ok"] is True and "booking is running" in res["detail"] and called == []
    res = await automation.heal("watchdog")                                # free again: the doctor runs
    assert called


async def test_a_booking_that_finds_the_browser_down_repairs_it_and_asks_for_a_retry(cfg, monkeypatch):
    import automation
    from cdp import CDPError
    importlib.reload(automation)

    class Down:
        def __init__(self, _ep):
            pass

        async def __aenter__(self):
            raise CDPError("connection refused")

        async def __aexit__(self, *a):
            return None
    monkeypatch.setattr(automation, "CDPBrowser", Down)

    async def heal(ep, reason="", force=False):
        return {"ok": True, "actions": ["started Edge again"], "detail": ""}
    monkeypatch.setattr(automation.doctor, "heal", heal)
    from indigo_client import BookingRequest, Passenger
    req = BookingRequest("DEL", "BOM", "07-12-2026", [Passenger("A", "B", "M")], "offer1", "a@b.co", "9000000001")
    res = await automation.book_flight(req, "6ETST001")
    assert res.success is False and res.retryable is True and "auto-repair: Started Edge again" in res.error


async def test_an_indigo_server_error_is_retried_not_refunded(cfg, monkeypatch):
    import indigo_client as ic
    monkeypatch.setattr(ic.Pace, "min_gap", 0.0)
    monkeypatch.setattr(ic.Pace, "jitter", 0.0)
    monkeypatch.setattr(ic.Pace, "cooldown_until", 0.0)

    class Tab:
        async def evaluate_json(self, *_a, **_k):
            return {"status": 503, "json": None}
    sess = ic.IndigoSession(Tab(), None, [])
    with pytest.raises(ic.IndigoBlocked):
        await sess.call("search", ic.U_SEARCH, "POST", ic.UK_FLIGHT, {})
    assert ic.Pace.cooldown_until == 0.0                                    # a server hiccup does not trip the breaker


# ── worker: retries, delivery, trends ──────────────────────────────────────
@pytest.fixture()
async def w(db):
    import worker
    importlib.reload(worker)
    sent, alerts = [], []

    async def notify(chat, text, kind="info"):
        sent.append((chat, text, kind))

    async def alert(text):
        alerts.append(text)
    worker._notify, worker._alert_admins = notify, alert
    worker.SEND_DELAYS = (0.0, 0.0, 0.0)
    yield worker, sent, alerts
    await worker.stop()


async def test_a_timed_out_booking_is_retried_instead_of_refunded(db, w):
    worker, sent, _alerts = w
    from indigo_client import BookingResult
    worker.JOB_TIMEOUT_S, worker.RETRY_DELAY_S = 0.1, 0.02
    calls = {"n": 0}

    async def run(req, promo):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(5)                                          # hangs the first time
        return BookingResult(success=True, pnr="PNR001", kind="record_locator")
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 2, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("TO1")], total_cost=1)
    await worker.start(worker._notify, worker._alert_admins, workers=1)
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (await db.get_booking("TO1"))["status"] == "done":
            break
    assert (await db.get_booking("TO1"))["status"] == "done" and calls["n"] == 2
    assert (await db.get_user(7))["credits"] == 1 and any("retried automatically" in t for _c, t, _k in sent)


async def test_the_pnr_message_is_retried_and_admins_hear_if_it_never_arrives(db, w):
    worker, sent, alerts = w
    from indigo_client import BookingResult
    tries = {"n": 0}

    async def flaky(chat, text, kind="info"):
        tries["n"] += 1
        if tries["n"] < 3:
            raise ConnectionError("telegram hiccup")
        sent.append((chat, text, kind))
    worker._notify = flaky

    async def run(req, promo):
        return BookingResult(success=True, pnr="PNR777", kind="record_locator")
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 5, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("N1")], total_cost=1)
    await worker.process("N1")
    assert tries["n"] == 3 and any("PNR777" in t for _c, t, _k in sent) and not alerts

    async def dead(chat, text, kind="info"):
        raise ConnectionError("telegram down")
    worker._notify = dead
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("N2")], total_cost=1)
    await worker.process("N2")
    assert (await db.get_booking("N2"))["status"] == "done"                 # the booking itself is safe
    assert len(alerts) == 1 and "PNR777" in alerts[0] and "could not be delivered" in alerts[0]


async def test_admins_are_told_once_when_the_pnr_step_keeps_failing(db, w):
    worker, _sent, alerts = w
    from indigo_client import BookingResult
    outcome = {"psp_failed": True}

    async def run(req, promo):
        return BookingResult(success=True, pnr="JFTWE1", kind="order_id", psp_failed=outcome["psp_failed"])
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 9, "seed")
    for i in range(5):
        await db.create_booking_charged(user_tg_id=7, bookings=[booking(f"S{i}")], total_cost=1)
        await worker.process(f"S{i}")
    assert len(alerts) == 1 and "PNR step failed 3 times in a row" in alerts[0]
    outcome["psp_failed"] = False
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("S9")], total_cost=1)
    await worker.process("S9")
    assert worker._streak["psp"] == 0


async def test_admins_are_told_when_a_coupon_stops_working(db, w):
    worker, _sent, alerts = w
    from indigo_client import BookingResult

    async def run(req, promo):
        return BookingResult(success=True, pnr="X", kind="record_locator", coupon_applied=False)
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 9, "seed")
    for i in range(3):
        await db.create_booking_charged(user_tg_id=7, bookings=[booking(f"C{i}", offer="offer1")], total_cost=1)
        await worker.process(f"C{i}")
    assert len(alerts) == 1 and "Offer 1 (6ETST001) was not applied" in alerts[0]


async def test_three_real_failures_in_a_row_alert_the_admins_but_no_flights_does_not(db, w):
    worker, _sent, alerts = w
    from indigo_client import BookingResult
    reason = {"err": "no flights available for this route/date"}

    async def run(req, promo):
        return BookingResult(success=False, error=reason["err"])
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 9, "seed")
    for i in range(3):
        await db.create_booking_charged(user_tg_id=7, bookings=[booking(f"NF{i}")], total_cost=1)
        await worker.process(f"NF{i}")
    assert alerts == []                                                     # an empty flight is not a system fault
    reason["err"] = "passenger details rejected (HTTP 400)"
    for i in range(3):
        await db.create_booking_charged(user_tg_id=7, bookings=[booking(f"RF{i}")], total_cost=1)
        await worker.process(f"RF{i}")
    assert len(alerts) == 1 and "3 bookings in a row have failed" in alerts[0] and "passenger details" in alerts[0]
    assert (await db.get_user(7))["credits"] == 9                          # every failure was refunded


# ── the watchdog and the admin's Fix-it-now button ──────────────────────────
async def test_the_watchdog_repairs_first_and_only_says_what_it_fixed(tgenv, monkeypatch):
    import automation
    env = tgenv

    async def sick():
        return {"ok": False}

    async def heal(reason="", force=False):
        return {"ok": True, "actions": ["started Edge again"], "detail": ""}
    monkeypatch.setattr(automation, "health", sick)
    monkeypatch.setattr(automation, "heal", heal)
    task = asyncio.create_task(env.bot._health_watch(env.app, every=0.01))
    await asyncio.sleep(0.15)
    task.cancel()
    texts = env.wire.texts(1001)
    assert any("Fixed automatically: Started Edge again." in t for t in texts)
    assert not any("offline" in t for t in texts)


async def test_admins_can_press_fix_it_now(tgenv, monkeypatch):
    import automation
    env = tgenv
    forced = []

    async def health():
        return {"ok": False, "error": "connection refused"}

    async def heal(reason="", force=False):
        forced.append(force)
        return {"ok": True, "actions": ["opened the IndiGo home page again"], "detail": ""}
    monkeypatch.setattr(automation, "health", health)
    monkeypatch.setattr(automation, "heal", heal)
    a = env.person(1001, "Boss")
    await a.say("/health")
    assert "🩹 Fix it now" in a.labels()
    await a.press("Fix it now")
    assert forced == [True] and "Repair:</b> Opened the IndiGo home page again." in a.card_text()
