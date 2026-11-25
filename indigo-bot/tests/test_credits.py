"""tasks.md: 1 credit per passenger, fully automatic; admin gives credits; nobody else can."""
import asyncio

import pytest

from conftest import booking

pytestmark = pytest.mark.asyncio


async def test_one_credit_per_passenger(db):
    await db.upsert_user(5)
    await db.add_credits(5, 10, "seed")
    ids = await db.create_booking_charged(user_tg_id=5, bookings=[booking("A", pax=1)], total_cost=1)
    assert ids == ["A"] and (await db.get_user(5))["credits"] == 9
    ids = await db.create_booking_charged(user_tg_id=5, bookings=[booking("B", pax=2)], total_cost=2)
    assert (await db.get_user(5))["credits"] == 7


async def test_both_offers_cost_double(db):
    await db.upsert_user(5)
    await db.add_credits(5, 10, "seed")
    rows = [booking("X1", "offer1", pax=3), booking("X2", "offer2", pax=3)]
    assert await db.create_booking_charged(user_tg_id=5, bookings=rows, total_cost=6)
    assert (await db.get_user(5))["credits"] == 4          # 3 pax x 2 offers = 6 credits


async def test_insufficient_credits_changes_nothing(db):
    await db.upsert_user(5)
    await db.add_credits(5, 1, "seed")
    assert await db.create_booking_charged(user_tg_id=5, bookings=[booking("A", pax=2)], total_cost=2) is None
    assert (await db.get_user(5))["credits"] == 1
    assert await db.get_booking("A") is None               # no orphan booking row


async def test_concurrent_spend_never_overdraws(db):
    """20 simultaneous 1-credit bookings against a balance of 5: exactly 5 succeed."""
    await db.upsert_user(5)
    await db.add_credits(5, 5, "seed")
    results = await asyncio.gather(*[
        db.create_booking_charged(user_tg_id=5, bookings=[booking(f"C{i}")], total_cost=1) for i in range(20)])
    assert sum(r is not None for r in results) == 5
    assert (await db.get_user(5))["credits"] == 0


async def test_refund_happens_exactly_once(db):
    await db.upsert_user(5)
    await db.add_credits(5, 3, "seed")
    await db.create_booking_charged(user_tg_id=5, bookings=[booking("R", pax=2)], total_cost=2)
    assert (await db.get_user(5))["credits"] == 1
    first = await db.mark_failed_and_refund("R", "boom")
    second = await db.mark_failed_and_refund("R", "boom again")      # e.g. retry/duplicate callback
    assert first == 3 and second is None
    assert (await db.get_user(5))["credits"] == 3


async def test_no_refund_after_success(db):
    await db.upsert_user(5)
    await db.add_credits(5, 2, "seed")
    await db.create_booking_charged(user_tg_id=5, bookings=[booking("S")], total_cost=1)
    await db.mark_done("S", "JFTWE123", "order_id", None, 100.0)
    assert await db.mark_failed_and_refund("S", "late failure") is None
    assert (await db.get_user(5))["credits"] == 1


async def test_balance_can_never_go_negative(db):
    await db.upsert_user(5)
    await db.add_credits(5, 2, "seed")
    assert await db.add_credits(5, -3, "admin") is None
    assert (await db.get_user(5))["credits"] == 2


async def test_ledger_records_every_movement(db):
    await db.upsert_user(5)
    await db.add_credits(5, 5, "seed", admin_tg_id=1001)
    await db.create_booking_charged(user_tg_id=5, bookings=[booking("L")], total_cost=1)
    await db.mark_failed_and_refund("L", "x")
    led = await db.get_ledger(5)
    assert [r["delta"] for r in reversed(led)] == [5, -1, 1]
    assert led[0]["balance"] == 5 and led[-1]["admin_tg_id"] == 1001
