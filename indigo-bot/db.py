"""Async SQLite layer: durable users/credits ledger, ephemeral booking PII.

Design goals (from tasks.md):
  * no credit can ever be lost or double-spent  -> every mutation is ONE atomic SQL
    statement (UPDATE ... WHERE credits >= ?) plus its ledger row in the same txn
  * booking-related PII is deleted 2 h after the PNR was DELIVERED (or 2 h after a
    failure), while users + the credit ledger + an anonymised archive survive
  * jobs survive a crash/restart (pending/running are re-queued on startup)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

import config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id BIGINT  UNIQUE NOT NULL,
    username    TEXT,
    full_name   TEXT,
    credits     INTEGER NOT NULL DEFAULT 0 CHECK (credits >= 0),
    is_admin    INTEGER NOT NULL DEFAULT 0,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bookings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT    UNIQUE NOT NULL,
    user_tg_id      BIGINT  NOT NULL,
    offer           TEXT    NOT NULL,
    origin          TEXT    NOT NULL,
    destination     TEXT    NOT NULL,
    travel_date     TEXT    NOT NULL,
    passengers_json TEXT    NOT NULL,
    pax_count       INTEGER NOT NULL,
    contact_email   TEXT,
    contact_phone   TEXT,
    credit_cost     INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',   -- pending|running|done|failed
    pnr             TEXT,
    pnr_kind        TEXT,
    pay_url         TEXT,
    fare_amount     REAL,
    error_msg       TEXT,
    declared_eligible INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    cleanup_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_bookings_user   ON bookings(user_tg_id, created_at);
CREATE INDEX IF NOT EXISTS ix_bookings_status ON bookings(status);
CREATE INDEX IF NOT EXISTS ix_bookings_clean  ON bookings(cleanup_at);

-- durable, PII-free record kept after the booking row is purged
CREATE TABLE IF NOT EXISTS booking_archive (
    job_id      TEXT PRIMARY KEY,
    user_tg_id  BIGINT NOT NULL,
    offer       TEXT NOT NULL,
    pax_count   INTEGER NOT NULL,
    credit_cost INTEGER NOT NULL,
    status      TEXT NOT NULL,
    fare_amount REAL,
    created_at  TEXT,
    archived_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS credit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_tg_id  BIGINT  NOT NULL,
    delta       INTEGER NOT NULL,
    balance     INTEGER NOT NULL,
    reason      TEXT,
    job_id      TEXT,
    admin_tg_id BIGINT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_credit_user ON credit_log(user_tg_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- everyone who ever messaged the bot (so admins can approve them with /adduser); pruned after 30 days
CREATE TABLE IF NOT EXISTS seen_users (
    telegram_id BIGINT PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now')),
    msg_count   INTEGER NOT NULL DEFAULT 1
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> aiosqlite.Connection:
    return aiosqlite.connect(config.DB_PATH, timeout=30)


async def _prep(db: aiosqlite.Connection) -> None:
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout = 30000")
    await db.execute("PRAGMA foreign_keys = ON")


async def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with _connect() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        await db.executescript(SCHEMA)
        cols = {r[1] for r in await (await db.execute("PRAGMA table_info(bookings)")).fetchall()}
        for col, ddl in (("coupon_applied", "INTEGER"), ("hold_until", "TEXT")):   # migrate older databases
            if col not in cols:
                await db.execute(f"ALTER TABLE bookings ADD COLUMN {col} {ddl}")
        await db.commit()
    log.info("database ready at %s", config.DB_PATH)


async def integrity() -> str:
    """SQLite's own self-check ('ok' when the file is healthy) - run at start-up."""
    async with _connect() as db:
        row = await (await db.execute("PRAGMA quick_check")).fetchone()
        return str(row[0]) if row else "no answer"


# ── backups: money records only, never booking PII ────────────
async def backup_to(path: Path) -> dict:
    """A consistent copy of the database WITHOUT personal data: users, the credit ledger, the anonymised archive and
    the settings are kept; booking rows (names, phones, emails, PNRs) and the people-who-messaged list are removed and
    the file is vacuumed so the deleted data is not recoverable from it. Written atomically (temp file, then rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    async with _connect() as db:
        await db.execute("VACUUM INTO ?", (str(tmp),))
    async with aiosqlite.connect(tmp) as c:
        await c.execute("DELETE FROM bookings")
        await c.execute("DELETE FROM seen_users")
        await c.commit()
        await c.execute("VACUUM")
        counts = {t: (await (await c.execute(f"SELECT COUNT(*) FROM {t}")).fetchone())[0]
                  for t in ("users", "credit_log", "booking_archive")}
        check = (await (await c.execute("PRAGMA integrity_check")).fetchone())[0]
    if check != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"the backup failed its integrity check: {check}")
    os.replace(tmp, path)
    return {"path": str(path), "bytes": path.stat().st_size, **counts}


def prune_backups(directory: Path, keep: int) -> int:
    """Keep the newest `keep` backup files, delete the rest. Returns how many were removed."""
    files = sorted(Path(directory).glob("bestfare-*.db"), key=lambda p: p.name, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)
    return max(0, len(files) - keep)


def backup_name(now: Optional[datetime] = None) -> str:
    return f"bestfare-{(now or datetime.now(timezone.utc)):%Y%m%d-%H%M}.db"


def latest_backup(directory: Path) -> Optional[Path]:
    files = sorted(Path(directory).glob("bestfare-*.db"), key=lambda p: p.name, reverse=True)
    return files[0] if files else None


# ── users ─────────────────────────────────────────────────────
async def upsert_user(telegram_id: int, username: Optional[str] = None, full_name: Optional[str] = None) -> dict:
    async with _connect() as db:
        await _prep(db)
        await db.execute(
            """INSERT INTO users (telegram_id, username, full_name) VALUES (?,?,?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 username  = COALESCE(excluded.username,  users.username),
                 full_name = COALESCE(excluded.full_name, users.full_name)""",
            (telegram_id, username, full_name))
        await db.commit()
        cur = await db.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
        return dict(await cur.fetchone())


async def get_user(telegram_id: int) -> Optional[dict]:
    async with _connect() as db:
        await _prep(db)
        cur = await db.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_all_users() -> list[dict]:
    async with _connect() as db:
        await _prep(db)
        cur = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
        return [dict(r) for r in await cur.fetchall()]


async def set_admin(telegram_id: int, is_admin: bool) -> None:
    async with _connect() as db:
        await db.execute("UPDATE users SET is_admin=? WHERE telegram_id=?", (1 if is_admin else 0, telegram_id))
        await db.commit()


async def set_active(telegram_id: int, active: bool) -> bool:
    async with _connect() as db:
        cur = await db.execute("UPDATE users SET is_active=? WHERE telegram_id=?", (1 if active else 0, telegram_id))
        await db.commit()
        return cur.rowcount > 0


async def is_admin(telegram_id: int) -> bool:
    if telegram_id in config.ADMIN_IDS:
        return True
    u = await get_user(telegram_id)
    return bool(u and u["is_admin"] and u["is_active"])


# ── credits (atomic) ──────────────────────────────────────────
async def _apply_credit(db: aiosqlite.Connection, tg_id: int, delta: int, reason: str,
                        job_id: Optional[str], admin_id: Optional[int]) -> Optional[int]:
    """Apply delta atomically; returns the new balance, or None if it would go below 0."""
    if delta >= 0:
        cur = await db.execute("UPDATE users SET credits = credits + ? WHERE telegram_id=?", (delta, tg_id))
    else:
        cur = await db.execute(
            "UPDATE users SET credits = credits + ? WHERE telegram_id=? AND credits + ? >= 0",
            (delta, tg_id, delta))
    if cur.rowcount == 0:
        return None
    row = await (await db.execute("SELECT credits FROM users WHERE telegram_id=?", (tg_id,))).fetchone()
    bal = int(row["credits"])
    await db.execute(
        "INSERT INTO credit_log (user_tg_id, delta, balance, reason, job_id, admin_tg_id) VALUES (?,?,?,?,?,?)",
        (tg_id, delta, bal, reason, job_id, admin_id))
    return bal


async def add_credits(telegram_id: int, amount: int, reason: str = "", admin_tg_id: Optional[int] = None,
                      job_id: Optional[str] = None) -> Optional[int]:
    """Add or subtract credits. Returns new balance, or None if the user is unknown / would go negative."""
    async with _connect() as db:
        await _prep(db)
        await db.execute("BEGIN IMMEDIATE")
        bal = await _apply_credit(db, telegram_id, amount, reason, job_id, admin_tg_id)
        await db.commit()
        return bal


async def refund_credits(telegram_id: int, amount: int, reason: str = "refund", job_id: Optional[str] = None) -> Optional[int]:
    return await add_credits(telegram_id, amount, reason=reason, job_id=job_id)


async def get_ledger(telegram_id: int, limit: int = 20) -> list[dict]:
    async with _connect() as db:
        await _prep(db)
        cur = await db.execute(
            "SELECT * FROM credit_log WHERE user_tg_id=? ORDER BY id DESC LIMIT ?", (telegram_id, limit))
        return [dict(r) for r in await cur.fetchall()]


# ── bookings ──────────────────────────────────────────────────
async def create_booking_charged(*, user_tg_id: int, bookings: list[dict], total_cost: int) -> Optional[list[str]]:
    """In ONE transaction: deduct `total_cost` credits and insert every booking row.
    Returns the job ids, or None when the balance is insufficient (nothing changes)."""
    job_ids = [b["job_id"] for b in bookings]
    async with _connect() as db:
        await _prep(db)
        await db.execute("BEGIN IMMEDIATE")
        bal = await _apply_credit(db, user_tg_id, -total_cost, f"booking x{len(bookings)}", job_ids[0], None)
        if bal is None:
            await db.rollback()
            return None
        for b in bookings:
            await db.execute(
                """INSERT INTO bookings (job_id,user_tg_id,offer,origin,destination,travel_date,passengers_json,
                       pax_count,contact_email,contact_phone,credit_cost,declared_eligible)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (b["job_id"], user_tg_id, b["offer"], b["origin"], b["destination"], b["travel_date"],
                 json.dumps(b["passengers"]), len(b["passengers"]), b["contact_email"], b["contact_phone"],
                 b["credit_cost"], 1 if b.get("declared_eligible") else 0))
        await db.commit()
        return job_ids


async def get_booking(job_id: str) -> Optional[dict]:
    async with _connect() as db:
        await _prep(db)
        row = await (await db.execute("SELECT * FROM bookings WHERE job_id=?", (job_id,))).fetchone()
        return dict(row) if row else None


async def claim_job(job_id: str) -> bool:
    """Atomically move pending -> running. Returns False if another worker already owns the job,
    so a job that was queued twice (restart recovery + retry) can never be booked twice."""
    async with _connect() as db:
        cur = await db.execute("UPDATE bookings SET status='running' WHERE job_id=? AND status='pending'", (job_id,))
        await db.commit()
        return cur.rowcount == 1


async def mark_running(job_id: str) -> None:
    async with _connect() as db:
        await db.execute("UPDATE bookings SET status='running' WHERE job_id=?", (job_id,))
        await db.commit()


async def requeue(job_id: str) -> None:
    """Put a running job back to pending (transient edge block); credits stay charged."""
    async with _connect() as db:
        await db.execute("UPDATE bookings SET status='pending' WHERE job_id=? AND status='running'", (job_id,))
        await db.commit()


async def mark_done(job_id: str, pnr: str, kind: str, pay_url: Optional[str], fare: Optional[float],
                    coupon_applied: Optional[bool] = None, hold_until: Optional[str] = None) -> None:
    cleanup = (datetime.now(timezone.utc) + timedelta(hours=config.BOOKING_CLEANUP_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await db.execute(
            """UPDATE bookings SET status='done', pnr=?, pnr_kind=?, pay_url=?, fare_amount=?, coupon_applied=?,
                   hold_until=?, completed_at=?, cleanup_at=? WHERE job_id=?""",
            (pnr, kind, pay_url, fare, None if coupon_applied is None else int(coupon_applied), hold_until,
             _now(), cleanup, job_id))
        await db.commit()


async def mark_failed_and_refund(job_id: str, error: str) -> Optional[int]:
    """Atomic: mark failed (once) and refund this job's credits (once). Returns new balance."""
    cleanup = (datetime.now(timezone.utc) + timedelta(hours=config.BOOKING_CLEANUP_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await _prep(db)
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """UPDATE bookings SET status='failed', error_msg=?, completed_at=?, cleanup_at=?
               WHERE job_id=? AND status IN ('pending','running')""",
            (error[:300], _now(), cleanup, job_id))
        if cur.rowcount == 0:                       # already finalised -> never refund twice
            await db.rollback()
            return None
        row = await (await db.execute("SELECT user_tg_id, credit_cost FROM bookings WHERE job_id=?", (job_id,))).fetchone()
        bal = await _apply_credit(db, row["user_tg_id"], row["credit_cost"], "refund: booking failed", job_id, None)
        await db.commit()
        return bal


async def cancel_pending(job_id: str, user_tg_id: int) -> Optional[int]:
    """The customer cancels a booking that has NOT started yet: it is closed and its credits come back, once.
    Returns the new balance, or None when it is too late (running / finished) or it is not theirs."""
    cleanup = (datetime.now(timezone.utc) + timedelta(hours=config.BOOKING_CLEANUP_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    async with _connect() as db:
        await _prep(db)
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """UPDATE bookings SET status='failed', error_msg='cancelled by the customer', completed_at=?, cleanup_at=?
               WHERE job_id=? AND user_tg_id=? AND status='pending'""", (_now(), cleanup, job_id, user_tg_id))
        if cur.rowcount == 0:
            await db.rollback()
            return None
        row = await (await db.execute("SELECT credit_cost FROM bookings WHERE job_id=?", (job_id,))).fetchone()
        bal = await _apply_credit(db, user_tg_id, row["credit_cost"], "refund: cancelled by you", job_id, None)
        await db.commit()
        return bal


async def get_user_bookings(user_tg_id: int, limit: int = 10) -> list[dict]:
    async with _connect() as db:
        await _prep(db)
        cur = await db.execute(
            "SELECT * FROM bookings WHERE user_tg_id=? ORDER BY id DESC LIMIT ?", (user_tg_id, limit))
        return [dict(r) for r in await cur.fetchall()]


async def recent_bookings(limit: int = 20, statuses: Optional[tuple] = None) -> list[dict]:
    async with _connect() as db:
        await _prep(db)
        if statuses:
            marks = ",".join("?" * len(statuses))
            cur = await db.execute(f"SELECT * FROM bookings WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",
                                   (*statuses, limit))
        else:
            cur = await db.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def open_jobs_for_user(user_tg_id: int) -> int:
    async with _connect() as db:
        await _prep(db)
        row = await (await db.execute(
            "SELECT COUNT(*) AS n FROM bookings WHERE user_tg_id=? AND status IN ('pending','running')",
            (user_tg_id,))).fetchone()
        return int(row["n"])


async def recoverable_jobs() -> list[dict]:
    """Jobs interrupted by a crash/restart: re-queue them (running -> pending)."""
    async with _connect() as db:
        await _prep(db)
        await db.execute("UPDATE bookings SET status='pending' WHERE status='running'")
        await db.commit()
        cur = await db.execute("SELECT * FROM bookings WHERE status='pending' ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def cleanup_old_bookings() -> int:
    """Archive (PII-free) then delete bookings whose retention window has passed."""
    async with _connect() as db:
        await _prep(db)
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """INSERT OR IGNORE INTO booking_archive
                   (job_id,user_tg_id,offer,pax_count,credit_cost,status,fare_amount,created_at)
               SELECT job_id,user_tg_id,offer,pax_count,credit_cost,status,fare_amount,created_at
               FROM bookings WHERE cleanup_at IS NOT NULL AND cleanup_at <= ?
                 AND status IN ('done','failed')""", (_now(),))
        cur = await db.execute(
            "DELETE FROM bookings WHERE cleanup_at IS NOT NULL AND cleanup_at <= ? AND status IN ('done','failed')",
            (_now(),))
        await db.execute("DELETE FROM seen_users WHERE last_seen < datetime('now', '-30 days')")
        await db.commit()
        return cur.rowcount


async def stats() -> dict:
    async with _connect() as db:
        await _prep(db)
        u = await (await db.execute("SELECT COUNT(*) n, COALESCE(SUM(credits),0) c FROM users")).fetchone()
        live = await (await db.execute(
            "SELECT status, COUNT(*) n FROM bookings GROUP BY status")).fetchall()
        arch = await (await db.execute(
            "SELECT status, COUNT(*) n FROM booking_archive GROUP BY status")).fetchall()
        return {"users": u["n"], "credits_outstanding": u["c"],
                "live": {r["status"]: r["n"] for r in live}, "archived": {r["status"]: r["n"] for r in arch}}


async def overview() -> dict:
    """Numbers for the admin dashboard (live + anonymised archive, last 24 h and all time)."""
    async with _connect() as db:
        await _prep(db)
        u = await (await db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(is_active),0) active, COALESCE(SUM(is_admin),0) admins, "
            "COALESCE(SUM(credits),0) credits FROM users")).fetchone()
        waiting = await (await db.execute(
            "SELECT COUNT(*) n FROM seen_users s LEFT JOIN users x ON x.telegram_id = s.telegram_id "
            "WHERE x.telegram_id IS NULL OR x.is_active = 0")).fetchone()
        day, total = {}, {}
        for table in ("bookings", "booking_archive"):
            for r in await (await db.execute(
                    f"SELECT status, COUNT(*) n FROM {table} WHERE created_at >= datetime('now','-1 day') GROUP BY status")).fetchall():
                day[r["status"]] = day.get(r["status"], 0) + r["n"]
            for r in await (await db.execute(f"SELECT status, COUNT(*) n FROM {table} GROUP BY status")).fetchall():
                total[r["status"]] = total.get(r["status"], 0) + r["n"]
        return {"users": u["n"], "active": u["active"], "admins": u["admins"], "credits": u["credits"],
                "waiting": waiting["n"], "day": day, "total": total}


async def count_users() -> int:
    async with _connect() as db:
        await _prep(db)
        return int((await (await db.execute("SELECT COUNT(*) n FROM users")).fetchone())["n"])


async def users_page(offset: int, limit: int) -> list[dict]:
    async with _connect() as db:
        await _prep(db)
        cur = await db.execute("SELECT * FROM users ORDER BY is_admin DESC, created_at DESC, id DESC LIMIT ? OFFSET ?",
                               (limit, offset))
        return [dict(r) for r in await cur.fetchall()]


async def search_users(q: str, limit: int = 8) -> list[dict]:
    """Find users by Telegram id (exact) or by part of the @username / name."""
    q = (q or "").strip().lstrip("@")
    if not q:
        return []
    async with _connect() as db:
        await _prep(db)
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cur = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ? OR username LIKE ? ESCAPE '\\' OR full_name LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?", (int(q) if q.isdigit() else -1, like, like, limit))
        return [dict(r) for r in await cur.fetchall()]


async def user_booking_counts(user_tg_id: int) -> dict:
    """{'done': n, 'failed': n, 'pending': n, 'running': n} across live rows and the archive."""
    out: dict[str, int] = {}
    async with _connect() as db:
        await _prep(db)
        for table in ("bookings", "booking_archive"):
            for r in await (await db.execute(
                    f"SELECT status, COUNT(*) n FROM {table} WHERE user_tg_id=? GROUP BY status", (user_tg_id,))).fetchall():
                out[r["status"]] = out.get(r["status"], 0) + r["n"]
    return out


async def ledger_recent(limit: int = 15) -> list[dict]:
    """Latest credit movements of everybody, with the person's name."""
    async with _connect() as db:
        await _prep(db)
        cur = await db.execute(
            "SELECT l.*, u.username, u.full_name FROM credit_log l LEFT JOIN users u ON u.telegram_id = l.user_tg_id "
            "ORDER BY l.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def dismiss_seen(telegram_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM seen_users WHERE telegram_id=?", (telegram_id,))
        await db.commit()


# ── people who contacted the bot ──────────────────────────────
async def note_seen(telegram_id: int, username: Optional[str], full_name: Optional[str]) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO seen_users (telegram_id, username, full_name) VALUES (?,?,?)
               ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name,
                   last_seen=datetime('now'), msg_count=msg_count+1""",
            (telegram_id, username, full_name))
        await db.commit()


async def pending_users(limit: int = 30) -> list[dict]:
    """People who messaged the bot but are not (active) registered users."""
    async with _connect() as db:
        await _prep(db)
        cur = await db.execute(
            """SELECT s.* FROM seen_users s LEFT JOIN users u ON u.telegram_id = s.telegram_id
               WHERE u.telegram_id IS NULL OR u.is_active = 0 ORDER BY s.last_seen DESC LIMIT ?""", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def purge_seen(days: int = 30) -> int:
    async with _connect() as db:
        cur = await db.execute("DELETE FROM seen_users WHERE last_seen < datetime('now', ?)", (f"-{int(days)} days",))
        await db.commit()
        return cur.rowcount


async def set_admins_exactly(ids: list[int]) -> None:
    """Make exactly `ids` the admins: everyone else loses the flag. (Env ADMIN_IDS is handled in config.)"""
    async with _connect() as db:
        await db.execute("UPDATE users SET is_admin=0")
        for tg in ids:                        # keep the name they messaged the bot with, so /users is readable
            await db.execute(
                "INSERT INTO users (telegram_id, username, full_name, is_admin, is_active) "
                "VALUES (?, (SELECT username FROM seen_users WHERE telegram_id=?), "
                "(SELECT full_name FROM seen_users WHERE telegram_id=?), 1, 1) "
                "ON CONFLICT(telegram_id) DO UPDATE SET is_admin=1, is_active=1, "
                "username=COALESCE(users.username, excluded.username), "
                "full_name=COALESCE(users.full_name, excluded.full_name)", (tg, tg, tg))
        await db.commit()


# ── settings / admin bootstrap ────────────────────────────────
async def get_setting(key: str) -> Optional[str]:
    async with _connect() as db:
        await _prep(db)
        row = await (await db.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
        return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    async with _connect() as db:
        await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))
        await db.commit()

