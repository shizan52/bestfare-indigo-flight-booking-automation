"""Load and stall tests: many customers at once, races between the worker and cancel buttons, a hung engine - nothing may
get stuck, nobody may be charged wrongly, and the credit ledger must always add up."""
import asyncio
import importlib
import time

import pytest

import ui
from conftest import booking

pytestmark = pytest.mark.asyncio

CUSTOMERS = 60


async def book_one(env, uid):
    u = env.person(uid, f"U{uid}")
    await u.say(ui.MENU_BOOK)
    await u.press("Delhi"); await u.press("Mumbai"); await u.press("Tomorrow")
    await u.cmd("n", "1")
    letters = "".join(chr(65 + int(c)) for c in str(uid))                 # names are letters only: 1007 -> BAAH
    await u.say(f"CUST{letters} SHARMA"); await u.cmd("g", "M")
    await u.say("9876543210"); await u.say("me@example.com")
    await u.cmd("o", "offer1")
    await u.press("Book now")
    return u


async def ledger_adds_up(db):
    users = await db.get_all_users()
    for u in users:
        total = sum(r["delta"] for r in await db.get_ledger(u["telegram_id"], 1000))
        assert total == u["credits"], (u["telegram_id"], total, u["credits"])


async def test_sixty_customers_book_at_once_and_everything_finishes(tgenv):
    from indigo_client import BookingResult
    env = tgenv
    for i in range(CUSTOMERS):
        await env.db.upsert_user(1000 + i, f"u{i}", f"User {i}")
        await env.db.add_credits(1000 + i, 3, "seed")

    async def engine(req, promo):
        await asyncio.sleep(0.004)
        return BookingResult(success=True, pnr="P" + req.passengers[0].first_name[-6:].replace(" ", ""),
                             kind="record_locator", fare_amount=5000, coupon_applied=True, discount=100.0)
    env.worker._runner = engine
    env.worker._notify = env.bot.make_notifier(env.app)
    t0 = time.monotonic()
    await asyncio.wait_for(asyncio.gather(*[book_one(env, 1000 + i) for i in range(CUSTOMERS)]), timeout=120)
    asked = time.monotonic() - t0
    assert env.worker.queue_size() == CUSTOMERS                        # every confirmed booking is queued exactly once
    await env.worker.start(env.worker._notify, workers=1)
    await asyncio.wait_for(env.worker._queue.join(), timeout=60)
    drained = time.monotonic() - t0 - asked
    rows = await env.db.recent_bookings(CUSTOMERS + 10)
    assert len(rows) == CUSTOMERS and {r["status"] for r in rows} == {"done"}
    assert env.worker.active_count() == 0 and env.worker.queue_size() == 0
    for i in range(CUSTOMERS):
        uid = 1000 + i
        assert (await env.db.get_user(uid))["credits"] == 2           # charged exactly once
        assert any("Your booking is ready" in t for t in env.wire.texts(uid)), uid       # and the result reached each person
    await ledger_adds_up(env.db)
    print(f"\n{CUSTOMERS} customers: taps handled in {asked:.1f}s, queue drained in {drained:.1f}s")


async def test_cancel_buttons_racing_the_worker_never_double_refund_or_lose_a_booking(tgenv):
    from indigo_client import BookingResult
    env = tgenv
    N = 40
    for i in range(N):
        await env.db.upsert_user(2000 + i, f"u{i}", f"User {i}")
        await env.db.add_credits(2000 + i, 3, "seed")
    runs = []

    async def engine(req, promo):
        runs.append(req.passengers[0].first_name)
        await asyncio.sleep(0.003)
        return BookingResult(success=True, pnr="OK" + str(len(runs)), kind="record_locator")
    env.worker._runner = engine
    env.worker._notify = env.bot.make_notifier(env.app)
    people = await asyncio.gather(*[book_one(env, 2000 + i) for i in range(N)])
    await env.worker.start(env.worker._notify, workers=1)

    async def cancel(i, u):
        jobs = await env.db.get_user_bookings(2000 + i, 1)
        if i % 2 == 0:                                                   # half of the customers change their mind
            await asyncio.sleep(0.002 * (i % 7))
            await u.tap(f"m:cx:{jobs[0]['job_id']}", mid=9000 + i)
    await asyncio.gather(*[cancel(i, u) for i, u in enumerate(people)])
    await asyncio.wait_for(env.worker._queue.join(), timeout=60)
    cancelled = done = 0
    for i in range(N):
        b = (await env.db.get_user_bookings(2000 + i, 1))[0]
        credits = (await env.db.get_user(2000 + i))["credits"]
        assert b["status"] in ("done", "failed"), b                       # nothing left pending / running
        if b["status"] == "done":
            done += 1
            assert credits == 2
        else:
            cancelled += 1
            assert credits == 3 and "cancelled" in b["error_msg"]         # refunded once, never twice
    assert done + cancelled == N and len(runs) == done                   # the engine never ran for a cancelled booking
    assert cancelled > 0 and done >= N // 2                               # the race really happened both ways
    await ledger_adds_up(env.db)


async def test_a_job_that_hangs_forever_is_stopped_refunded_and_the_queue_moves_on(db, cfg):
    import worker
    importlib.reload(worker)
    worker.JOB_HARD_TIMEOUT_S = 0.2
    sent, alerts = [], []

    async def notify(chat, text, kind="info"):
        sent.append((chat, text, kind))

    async def alert(text):
        alerts.append(text)
    worker._notify, worker._alert_admins = notify, alert
    hung = {"id": None}
    real_process = worker.process

    async def process(job_id, attempt=None):
        if hung["id"] is None:
            hung["id"] = job_id
            await db.claim_job(job_id)
            await asyncio.sleep(999)                                       # a hang that ignores every inner timeout
        await real_process(job_id, attempt)
    worker.process = process
    from indigo_client import BookingResult

    async def run(req, promo):
        return BookingResult(success=True, pnr="NEXT01", kind="record_locator")
    worker._runner = run
    await db.upsert_user(7); await db.add_credits(7, 5, "seed")
    await db.create_booking_charged(user_tg_id=7, bookings=[booking("H1"), booking("H2")], total_cost=2)
    await worker.start(notify, alert, workers=1)
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (await db.get_booking("H2"))["status"] == "done":
            break
    await worker.stop()
    assert (await db.get_booking("H1"))["status"] == "failed" and (await db.get_booking("H2"))["status"] == "done"
    assert (await db.get_user(7))["credits"] == 4                           # 5 - 2 booked + 1 refunded for the hung one
    assert any("could not book" in t for _c, t, _k in sent) and any("hung for more than" in a for a in alerts)
    await ledger_adds_up(db)


async def test_the_heartbeat_proves_life_and_reports_a_queue_nobody_works_on(tgenv, caplog):
    import logging
    env = tgenv
    caplog.set_level(logging.INFO, logger="bot")
    await env.worker.enqueue("WAITING1")                                    # waiting, but no worker is running
    task = asyncio.create_task(env.bot._heartbeat(env.app, every=0.02))
    await asyncio.sleep(0.3)
    task.cancel()
    assert any("heartbeat: queue=1 active=0" in r.getMessage() for r in caplog.records)
    alerts = [t for t in env.wire.texts(1001) if "nothing has worked on them" in t]
    assert len(alerts) == 1                                                 # once, not every beat
    await env.worker._queue.get()
    env.worker._queue.task_done()
