"""Queue + worker + cleanup + recovery, using a fake booking engine (no IndiGo traffic)."""
import asyncio
import importlib
import time

import pytest

from conftest import booking

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def w(db):
    import worker
    importlib.reload(worker)
    sent: list[tuple[int, str]] = []

    async def notify(chat, text, kind="info"):
        sent.append((chat, text))

    worker._notify = notify
    yield worker, sent
    await worker.stop()


def ok_runner(delay=0.0, log=None):
    from indigo_client import BookingResult

    async def run(req, promo):
        if log is not None:
            log.append(("start", promo, time.monotonic()))
        await asyncio.sleep(delay)
        if log is not None:
            log.append(("end", promo, time.monotonic()))
        return BookingResult(success=True, pnr="JFTWE" + promo, kind="order_id", fare_amount=5132,
                             pay_url="https://pay.example/x")
    return run


async def test_success_delivers_reference_and_schedules_cleanup(db, w):
    worker, sent = w
    worker._runner = ok_runner()
    await db.upsert_user(7); await db.add_credits(7, 5, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("OK1")], total_cost=1)
    await worker.process("OK1")
    b = await db.get_booking("OK1")
    assert b["status"] == "done" and b["pnr"] == "JFTWE6ETST001" and b["cleanup_at"]
    assert sent and sent[0][0] == 7 and "JFTWE6ETST001" in sent[0][1] and "30" in sent[0][1]


async def test_offer_selects_the_right_coupon(db, w):
    worker, sent = w
    seen = []
    from indigo_client import BookingResult

    async def run(req, promo):
        seen.append((req.offer, promo))
        return BookingResult(success=True, pnr="X" + promo, kind="order_id")
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 9, "seed")
    await db.create_booking_charged(user_tg_id=7, total_cost=2, bookings=[booking("O1", "offer1"), booking("O2", "offer2")])
    await worker.process("O1"); await worker.process("O2")
    assert seen == [("offer1", "6ETST001"), ("offer2", "PROMO2")]


async def test_failure_refunds_and_tells_user(db, w):
    worker, sent = w
    from indigo_client import BookingResult

    async def run(req, promo):
        return BookingResult(success=False, error="no flights available")
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 5, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("F1", pax=2)], total_cost=2)
    assert (await db.get_user(7))["credits"] == 3
    await worker.process("F1")
    assert (await db.get_user(7))["credits"] == 5
    assert (await db.get_booking("F1"))["status"] == "failed"
    assert "refunded" in sent[0][1]


async def test_job_queued_twice_is_booked_only_once(db, w):
    """Regression: restart-recovery + a retry could queue the same job twice -> two IndiGo holds."""
    worker, sent = w
    from indigo_client import BookingResult
    runs = {"n": 0}

    async def run(req, promo):
        runs["n"] += 1
        await asyncio.sleep(0.05)
        return BookingResult(success=True, pnr="JFTWEONCE", kind="order_id", fare_amount=1)
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 3, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("DUP")], total_cost=1)
    await asyncio.gather(worker.process("DUP"), worker.process("DUP"), worker.process("DUP"))
    assert runs["n"] == 1                                          # exactly one booking attempt
    assert len([1 for _c, t in sent if "JFTWEONCE" in t]) == 1     # exactly one message
    assert (await db.get_user(7))["credits"] == 2


async def test_transient_edge_block_is_retried_not_failed(db, w):
    """A 429/deny must not cost the customer their booking: re-queue, keep credits, succeed later."""
    worker, sent = w
    worker.RETRY_DELAY_S = 0.05
    from indigo_client import BookingResult
    calls = {"n": 0}

    async def run(req, promo):
        calls["n"] += 1
        if calls["n"] < 3:
            return BookingResult(success=False, retryable=True, error="IndiGo temporarily refused the request")
        return BookingResult(success=True, pnr="JFTWEOK", kind="order_id", fare_amount=5000)
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 3, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("RT1")], total_cost=1)
    await worker.start(worker._notify, workers=1)       # start() already re-queues the pending job
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (await db.get_booking("RT1"))["status"] == "done":
            break
    b = await db.get_booking("RT1")
    assert b["status"] == "done" and b["pnr"] == "JFTWEOK" and calls["n"] == 3
    assert (await db.get_user(7))["credits"] == 2                    # charged exactly once
    assert sum("retried automatically" in t for _c, t in sent) == 1            # told the user once, not spammed


async def test_persistent_block_refunds_once_after_retries(db, w):
    worker, sent = w
    worker.RETRY_DELAY_S = 0.02
    worker.MAX_BLOCK_RETRIES = 2
    from indigo_client import BookingResult

    async def run(req, promo):
        return BookingResult(success=False, retryable=True, error="IndiGo temporarily refused the request")
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 3, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("RT2")], total_cost=1)
    await worker.start(worker._notify, workers=1)       # start() already re-queues the pending job
    for _ in range(200):
        await asyncio.sleep(0.05)
        if (await db.get_booking("RT2"))["status"] == "failed":
            break
    assert (await db.get_booking("RT2"))["status"] == "failed"
    assert (await db.get_user(7))["credits"] == 3                    # refunded exactly once


async def test_crashing_engine_refunds_and_never_kills_worker(db, w):
    worker, sent = w

    async def run(req, promo):
        raise RuntimeError("kaboom")
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 2, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("K1")], total_cost=1)
    await worker.process("K1")
    assert (await db.get_user(7))["credits"] == 2
    assert (await db.get_booking("K1"))["status"] == "failed"


async def test_a_hung_engine_is_retried_then_refunded_exactly_once(db, w):
    """A timeout may be a passing browser hiccup: it is retried; only if it keeps hanging is the booking refunded."""
    worker, sent = w
    worker.JOB_TIMEOUT_S, worker.RETRY_DELAY_S, worker.MAX_BLOCK_RETRIES = 0.1, 0.02, 2
    runs = {"n": 0}

    async def run(req, promo):
        runs["n"] += 1
        await asyncio.sleep(30)
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 2, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("T1")], total_cost=1)
    await worker.start(worker._notify, workers=1)
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (await db.get_booking("T1"))["status"] == "failed":
            break
    assert (await db.get_booking("T1"))["status"] == "failed" and runs["n"] == 3      # first try + 2 retries
    assert (await db.get_user(7))["credits"] == 2                                    # refunded once


async def test_burst_of_users_is_queued_and_processed_one_by_one(db, w):
    """30 users book at once; one browser => strictly serial, nothing lost, nothing overlaps."""
    worker, sent = w
    log: list = []
    worker._runner = ok_runner(delay=0.01, log=log)
    for u in range(30):
        await db.upsert_user(100 + u); await db.add_credits(100 + u, 1, "seed")
    await asyncio.gather(*[db.create_booking_charged(user_tg_id=100 + u, bookings=[booking(f"B{u}")], total_cost=1)
                           for u in range(30)])
    await worker.start(worker._notify, workers=1)           # 1 trusted browser
    for u in range(30):
        await worker.enqueue(f"B{u}")
    await asyncio.wait_for(worker._queue.join(), timeout=20)
    assert len(sent) == 30
    statuses = [(await db.get_booking(f"B{u}"))["status"] for u in range(30)]
    assert statuses == ["done"] * 30
    # no two bookings ever overlapped in time
    active = peak = 0
    for kind, _p, _t in sorted(log, key=lambda x: x[2]):
        active += 1 if kind == "start" else -1
        peak = max(peak, active)
    assert peak == 1


async def test_cleanup_purges_pii_after_retention_but_keeps_money_trail(db, w):
    worker, sent = w
    worker._runner = ok_runner()
    await db.upsert_user(7); await db.add_credits(7, 5, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("CL")], total_cost=1)
    await worker.process("CL")
    assert await db.cleanup_old_bookings() == 0                    # still inside the 2h window
    import aiosqlite
    async with aiosqlite.connect(db.config.DB_PATH) as conn:        # pretend 2h passed
        await conn.execute("UPDATE bookings SET cleanup_at='2000-01-01 00:00:00' WHERE job_id='CL'")
        await conn.commit()
    assert await db.cleanup_old_bookings() == 1
    assert await db.get_booking("CL") is None                      # names/phone/email/PNR gone
    st = await db.stats()
    assert st["archived"] == {"done": 1}                           # anonymised trail survives
    assert (await db.get_user(7))["credits"] == 4 and len(await db.get_ledger(7)) == 2


async def test_unfinished_jobs_are_never_cleaned(db, w):
    await db.upsert_user(7); await db.add_credits(7, 5, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("P1")], total_cost=1)
    import aiosqlite
    async with aiosqlite.connect(db.config.DB_PATH) as conn:
        await conn.execute("UPDATE bookings SET cleanup_at='2000-01-01 00:00:00' WHERE job_id='P1'")
        await conn.commit()
    assert await db.cleanup_old_bookings() == 0                    # pending jobs are protected


async def test_restart_requeues_interrupted_jobs(db, w):
    worker, sent = w
    worker._runner = ok_runner()
    await db.upsert_user(7); await db.add_credits(7, 5, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("RC1"), booking("RC2")], total_cost=2)
    await db.mark_running("RC1")                                   # was mid-flight when the process died
    await worker.start(worker._notify, workers=1)
    await asyncio.wait_for(worker._queue.join(), timeout=10)
    assert (await db.get_booking("RC1"))["status"] == "done"
    assert (await db.get_booking("RC2"))["status"] == "done"
    assert (await db.get_user(7))["credits"] == 3                  # charged once, not twice

