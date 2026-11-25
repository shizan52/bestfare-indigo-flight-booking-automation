"""Production safeguards: PII-free backups (+ restore), the flood limiter, the start-up self-check."""
import asyncio
import shutil
from pathlib import Path

import pytest

import ui
from conftest import booking

pytestmark = pytest.mark.asyncio

SECRETS = ("ZEBEDIAH", "QUIMBY", "9123456780", "zebediah.quimby@example.org", "QQ9ZZ1", "Sneaky Stranger", "sneaky_s")


async def seed_with_personal_data(db):
    await db.upsert_user(7, "maya", "Maya Rahman")
    await db.add_credits(7, 5, "welcome credits", admin_tg_id=1001)
    row = booking("PII1", pax=1)
    row["passengers"] = [{"first_name": "ZEBEDIAH", "last_name": "QUIMBY", "gender": "M", "dob": ""}]
    row["contact_phone"], row["contact_email"] = "9123456780", "zebediah.quimby@example.org"
    await db.create_booking_charged(user_tg_id=7, bookings=[row], total_cost=1)
    await db.mark_done("PII1", "QQ9ZZ1", "record_locator", None, 5000.0, True, "2999-01-01T00:00:00Z")
    await db.note_seen(99, "sneaky_s", "Sneaky Stranger")


async def test_a_backup_keeps_the_money_and_contains_no_personal_data(db, tmp_path):
    await seed_with_personal_data(db)
    info = await db.backup_to(tmp_path / "b" / "bestfare-test.db")
    assert info["users"] == 1 and info["credit_log"] == 2 and info["bytes"] > 0          # +5 welcome, -1 booking
    raw = (tmp_path / "b" / "bestfare-test.db").read_bytes()
    for secret in SECRETS:
        assert secret.encode() not in raw, f"{secret} leaked into the backup file"
    import aiosqlite
    async with aiosqlite.connect(tmp_path / "b" / "bestfare-test.db") as c:
        c.row_factory = aiosqlite.Row
        assert (await (await c.execute("SELECT credits FROM users WHERE telegram_id=7")).fetchone())["credits"] == 4
        assert (await (await c.execute("SELECT COUNT(*) n FROM bookings")).fetchone())["n"] == 0
        assert (await (await c.execute("SELECT COUNT(*) n FROM seen_users")).fetchone())["n"] == 0
        assert (await (await c.execute("PRAGMA integrity_check")).fetchone())[0] == "ok"
    assert not list((tmp_path / "b").glob("*.tmp"))                                       # no half-written leftovers
    assert (await db.get_booking("PII1"))["pnr"] == "QQ9ZZ1"                              # the live database is untouched


async def test_a_backup_can_be_restored_and_the_bot_starts_from_it(db, cfg, tmp_path):
    await seed_with_personal_data(db)
    backup = tmp_path / "bestfare-restore.db"
    await db.backup_to(backup)
    cfg.DB_PATH.unlink()                                                                 # the disk "dies"
    for ext in ("-wal", "-shm"):
        Path(str(cfg.DB_PATH) + ext).unlink(missing_ok=True)
    shutil.copy(backup, cfg.DB_PATH)                                                     # restore = copy the file back
    await db.init_db()
    assert (await db.get_user(7))["credits"] == 4                                        # balances survived
    assert [r["delta"] for r in await db.get_ledger(7)] == [-1, 5]                       # and the credit history
    assert await db.get_booking("PII1") is None and await db.integrity() == "ok"
    await db.add_credits(7, 2, "after restore")                                          # and it works normally
    assert (await db.get_user(7))["credits"] == 6


async def test_only_the_newest_backups_are_kept(db, tmp_path):
    for day in range(1, 9):
        (tmp_path / f"bestfare-202611{day:02d}-0300.db").write_bytes(b"x")
    (tmp_path / "unrelated.txt").write_text("keep me")
    assert db.prune_backups(tmp_path, keep=3) == 5
    assert sorted(p.name for p in tmp_path.glob("bestfare-*.db")) == [
        "bestfare-20261106-0300.db", "bestfare-20261107-0300.db", "bestfare-20261108-0300.db"]
    assert (tmp_path / "unrelated.txt").exists() and db.latest_backup(tmp_path).name == "bestfare-20261108-0300.db"


async def test_the_daily_backup_loop_writes_one_per_day_and_reports_repeated_failure(db, cfg, monkeypatch):
    import importlib
    import worker
    importlib.reload(worker)
    alerts = []

    async def alert(text):
        alerts.append(text)
    worker._alert_admins = alert
    await db.upsert_user(7); await db.add_credits(7, 1, "seed")
    task = asyncio.create_task(worker._backup_loop(interval=0.05, first_delay=0))
    await asyncio.sleep(0.4)
    task.cancel()
    files = list(cfg.BACKUP_DIR.glob("bestfare-*.db"))
    assert len(files) == 1                                                                # fresh one exists: not repeated

    async def broken(path):
        raise OSError("disk full")
    monkeypatch.setattr(worker.db, "backup_to", broken)
    for f in files:
        f.unlink()
    task = asyncio.create_task(worker._backup_loop(interval=0.02, first_delay=0))
    await asyncio.sleep(0.4)
    task.cancel()
    assert len(alerts) == 1 and "failed 3 times in a row" in alerts[0] and "disk space" in alerts[0]


async def test_admins_get_a_backup_file_in_telegram_and_nobody_else(tgenv):
    env = tgenv
    await env.db.upsert_user(7, "maya", "Maya"); await env.db.add_credits(7, 3, "seed")
    a = env.person(1001, "Boss")
    await a.say("/backup")
    doc = env.wire.documents[-1]
    assert doc["chat"] == 1001 and doc["filename"].startswith("bestfare-") and doc["filename"].endswith(".db")
    assert "No passenger data inside" in doc["caption"] and len(doc["bytes"]) > 1000
    assert "Backup sent" in a.last()
    await a.say("/admin")
    assert "🗄 Backup" in a.labels()
    await a.press("Backup")
    assert len(env.wire.documents) == 2 and "Backup sent" in env.wire.answers[-1][0]
    n = len(env.wire.documents)
    m = env.person(7, "Maya")
    await m.say("/backup")
    await m.tap("a:bkp", mid=4244)
    assert len(env.wire.documents) == n and "admins only" in m.last()


async def test_a_flood_from_one_person_is_ignored_and_others_are_unaffected(tgenv):
    env = tgenv
    env.bot.RATE_MAX = 5
    await env.db.upsert_user(300, "rahul", "Rahul"); await env.db.upsert_user(301, "other", "Other")
    spammer, calm = env.person(300, "Rahul"), env.person(301, "Other")
    for _ in range(20):
        await spammer.say("/balance")
    assert len([t for t in env.wire.texts(300) if "Your credits" in t]) == 5               # only the first 5 were answered
    await calm.say("/balance")
    assert "Your credits" in calm.last()
    await spammer.tap("m:book", mid=999)                                                   # a flooded button is silently ended
    assert env.wire.answers[-1] == (None, False)
    assert not any("no credits yet" in t for t in env.wire.texts(300))                     # the tap did nothing


async def test_the_flood_limit_resets_after_a_quiet_moment(tgenv, monkeypatch):
    env = tgenv
    env.bot.RATE_MAX = 3
    await env.db.upsert_user(300, "rahul", "Rahul")
    u = env.person(300, "Rahul")
    for _ in range(6):
        await u.say("/balance")
    answered = len([t for t in env.wire.texts(300) if "Your credits" in t])
    assert answered == 3
    real = env.bot.time.monotonic
    monkeypatch.setattr(env.bot.time, "monotonic", lambda: real() + 60)                     # a minute later
    await u.say("/balance")
    assert len([t for t in env.wire.texts(300) if "Your credits" in t]) == 4


async def test_the_indigo_cooldown_survives_a_restart(db, monkeypatch):
    """Live finding: after IndiGo's edge blocked us, a restart forgot the cool-down and probed IndiGo again at once."""
    import time

    import pytest as _pytest

    import bot
    from indigo_client import IndigoBlocked, Pace
    monkeypatch.setattr(Pace, "cooldown_until", 0.0)
    monkeypatch.setattr(Pace, "on_trip", None)
    await bot.restore_cooldown()                                       # start-up: nothing saved yet, saving is switched on
    assert Pace.cooldown_until == 0.0
    Pace.trip(600)                                                     # IndiGo blocks us
    await asyncio.sleep(0.05)
    assert 590 < float(await db.get_setting("pace_until")) - time.time() <= 600
    monkeypatch.setattr(Pace, "cooldown_until", 0.0)                   # --- the service restarts: memory is wiped ---
    left = await bot.restore_cooldown()
    assert 590 < left <= 600 and Pace.cooldown_until > time.monotonic()
    with _pytest.raises(IndigoBlocked):
        await Pace.wait()                                              # still cooling down: no call goes to IndiGo
    await db.set_setting("pace_until", str(time.time() - 5))           # the cool-down has passed
    monkeypatch.setattr(Pace, "cooldown_until", 0.0)
    assert await bot.restore_cooldown() == 0.0 and Pace.cooldown_until == 0.0


async def test_admins_are_told_once_when_indigo_asks_us_to_slow_down(tgenv, monkeypatch):
    from indigo_client import Pace
    env = tgenv
    monkeypatch.setattr(Pace, "cooldown_until", 0.0)
    monkeypatch.setattr(Pace, "on_trip", None)
    await env.bot.restore_cooldown(env.app)
    Pace.trip(900)
    await asyncio.sleep(0.1)
    alerts = [t for t in env.wire.texts(1001) if "IndiGo asked us to slow down" in t]
    assert len(alerts) == 1 and "15 minutes" in alerts[0] and "continue by themselves" in alerts[0]


async def test_repeated_blocks_back_off_longer_each_time_and_a_success_resets_it(db):
    """Live finding: IndiGo kept refusing us after a dozen test holds - probing every 15 minutes only prolongs that."""
    from indigo_client import Pace
    waits = []
    for _ in range(5):
        Pace.trip(900)
        waits.append(round(Pace.cooldown_left()))
    assert waits == [900, 1800, 3600, 7200, 7200]                       # 15 min, 30, 60, then 2 hours at most
    Pace.success()                                                      # IndiGo answers normally again
    Pace.trip(900)
    assert round(Pace.cooldown_left()) == 900


async def test_customers_are_asked_to_come_back_later_while_the_airline_site_is_busy(tgenv):
    from indigo_client import Pace
    from test_wizard import make_user, to_review
    env = tgenv
    u = await make_user(env, credits=3)
    Pace.trip(600)
    await u.say(ui.MENU_BOOK)
    assert "airline's website is busy" in u.last() and "10 minutes" in u.last() and "nothing was charged" in u.last()
    assert not env.wire.card(300)                                       # no form was even opened
    Pace.cooldown_until = 0.0                                           # the block is over: a form can be filled in ...
    await to_review(u)
    Pace.trip(300)                                                      # ... and IndiGo blocks again before Book now
    await u.press("Book now")
    assert "airline's website is busy" in env.wire.answers[-1][0] and env.wire.answers[-1][1] is True
    assert (await env.db.get_user(300))["credits"] == 3 and not await env.db.get_user_bookings(300)
    Pace.cooldown_until = 0.0
    await u.press("Book now")                                           # and it works as soon as the block is over
    assert (await env.db.get_user(300))["credits"] == 2


async def test_a_recent_block_series_is_remembered_across_restarts_but_an_old_one_is_forgotten(db):
    import time

    import bot
    from indigo_client import Pace
    await db.set_setting("pace_until", str(time.time() + 600))
    await db.set_setting("pace_until_strikes", "3")
    await bot.restore_cooldown()
    assert Pace.strikes == 3 and Pace.cooldown_left() > 500
    Pace.strikes, Pace.cooldown_until = 0, 0.0                          # --- another restart, 4 hours after the last block ---
    await db.set_setting("pace_until", str(time.time() - 4 * 3600))
    await db.set_setting("pace_until_strikes", "5")
    await bot.restore_cooldown()
    assert Pace.strikes == 0 and Pace.cooldown_left() == 0


async def test_the_start_up_self_check_reports_a_healthy_database(db):
    assert await db.integrity() == "ok"


async def test_normal_human_speed_never_hits_the_limit(tgenv):
    """The default limit (14 per 10 s) is far above anything a person does - a whole booking in a few seconds is fine."""
    import importlib
    import bot
    env = tgenv
    importlib.reload(bot)                                                                  # the real default limits
    assert bot.RATE_MAX >= 10 and bot.RATE_WINDOW >= 5
    assert ui.MENU_BOOK
