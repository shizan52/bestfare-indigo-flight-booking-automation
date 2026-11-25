"""If the trusted browser is down (box rebooted, window closed) a booking must neither hang nor
lose the customer's money: it is retried later, then refunded exactly once."""
import asyncio

import pytest

from conftest import booking

pytestmark = pytest.mark.asyncio


async def test_engine_reports_browser_down_as_retryable_not_crash(cfg, monkeypatch):
    import importlib
    import automation
    monkeypatch.setattr(cfg, "CDP_ENDPOINTS", ["http://127.0.0.1:9"])        # nothing listens here
    importlib.reload(automation)
    from indigo_client import BookingRequest, Passenger
    req = BookingRequest("DEL", "BOM", "07-12-2026", [Passenger("A", "B", "M", "01-01-1950")], "offer1", "a@b.co", "9000000001")
    res = await asyncio.wait_for(automation.book_flight(req, "6ETST001"), timeout=30)
    assert res.success is False
    assert res.retryable is True                                              # worker will wait + retry, not refund yet
    assert "unavailable" in res.error


async def test_health_reports_down_without_raising(cfg, monkeypatch):
    import importlib
    import automation
    monkeypatch.setattr(cfg, "CDP_ENDPOINTS", ["http://127.0.0.1:9"])
    importlib.reload(automation)
    h = await automation.health()
    assert h["ok"] is False and "error" in h


async def test_browser_down_then_back_up_completes_the_booking(db, monkeypatch):
    import importlib
    import worker
    importlib.reload(worker)
    worker.RETRY_DELAY_S = 0.05
    from indigo_client import BookingResult
    state = {"up": False, "calls": 0}

    async def run(req, promo):
        state["calls"] += 1
        if state["calls"] >= 3:                                               # the Edge window is reopened after 2 failures
            state["up"] = True
        if not state["up"]:
            return BookingResult(success=False, retryable=True, error="booking browser unavailable: connection refused")
        return BookingResult(success=True, pnr="JFTWEBACK", kind="order_id", fare_amount=1)
    worker._runner = run
    sent = []

    async def notify(c, t, kind="info"):
        sent.append(t)
    worker._notify = notify
    await db.upsert_user(7); await db.add_credits(7, 2, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("DN1")], total_cost=1)
    await worker.start(notify, workers=1)
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (await db.get_booking("DN1"))["status"] == "done":
            break
    assert (await db.get_booking("DN1"))["status"] == "done"
    assert (await db.get_user(7))["credits"] == 1                             # charged once, never double
    await worker.stop()
