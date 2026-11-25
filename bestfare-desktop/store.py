"""Best Fare's own small database on the user's computer (SQLite): the booking history and a few settings.

Privacy rule from the original spec: the personal details of a booking (names, phone, email) are wiped 2 hours after the
booking finished - the PNR only lives 30 minutes anyway. What stays is what the user needs to look back: when, which
route and date, how many travellers, which offer, the PNR and the fare.
Contact details are remembered only when the user ticks "Remember on this computer".
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

WIPE_AFTER_S = 2 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id            TEXT PRIMARY KEY,
    grp           TEXT NOT NULL,
    created_at    REAL NOT NULL,
    origin        TEXT NOT NULL,
    dest          TEXT NOT NULL,
    travel_date   TEXT NOT NULL,          -- YYYY-MM-DD
    offer         TEXT NOT NULL,          -- offer1 | offer2
    pax_count     INTEGER NOT NULL,
    pax_json      TEXT NOT NULL DEFAULT '[]',
    phone         TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,          -- waiting | running | done | failed
    pnr           TEXT,
    kind          TEXT,                   -- record_locator | order_id
    fare          REAL,
    discount      REAL,
    coupon_applied INTEGER,
    hold_until    TEXT,
    pay_url       TEXT,
    error         TEXT,
    finished_at   REAL,
    wiped         INTEGER NOT NULL DEFAULT 0,
    verified      TEXT,                   -- ok | mismatch | unknown: IndiGo's record of the booking checked against the order
    verify_note   TEXT,
    flight        TEXT,                   -- the flight the traveller chose, in words: "6E 864 · 15:30 → 18:00"
    cloud_id      TEXT,                   -- the server's id of this booking (the credits were taken against it)
    cloud_state   TEXT,                   -- reserved (not yet reported to the server) | reported
    credits_used  INTEGER,                -- the credits the server took for this booking (NULL: none / an older record)
    credits_back  INTEGER                 -- the credits the server gave back after it failed (0: none; NULL: the report has not been made yet)
);
CREATE INDEX IF NOT EXISTS ix_bookings_created ON bookings(created_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

FIELDS = ("status", "pnr", "kind", "fare", "discount", "coupon_applied", "hold_until", "pay_url", "error", "finished_at", "verified",
          "verify_note", "flight", "cloud_id", "cloud_state", "credits_used", "credits_back")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            have = {r[1] for r in self._db.execute("PRAGMA table_info(bookings)")}
            for col in ("verified TEXT", "verify_note TEXT", "flight TEXT", "cloud_id TEXT", "cloud_state TEXT", "credits_used INTEGER", "credits_back INTEGER"):          # history files made by an older version
                if col.split()[0] not in have:
                    self._db.execute(f"ALTER TABLE bookings ADD COLUMN {col}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ── bookings ─────────────────────────────────────────────────────────────────────────────────────
    def add_booking(self, *, grp: str, origin: str, dest: str, travel_date: str, offer: str, travellers: list[dict],
                    phone: str, email: str, status: str = "waiting") -> str:
        bid = uuid.uuid4().hex[:10].upper()
        self._q("INSERT INTO bookings (id, grp, created_at, origin, dest, travel_date, offer, pax_count, pax_json, phone, "
                "email, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (bid, grp, time.time(), origin, dest, travel_date, offer, len(travellers),
                 json.dumps(travellers, ensure_ascii=False), phone, email, status))
        return bid

    def update(self, bid: str, **fields: Any) -> None:
        bad = set(fields) - set(FIELDS)
        if bad:
            raise ValueError(f"unknown booking fields: {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = tuple(int(v) if isinstance(v, bool) else v for v in fields.values())
        self._q(f"UPDATE bookings SET {cols} WHERE id=?", (*vals, bid))

    def get(self, bid: str) -> Optional[dict]:
        rows = self._q("SELECT * FROM bookings WHERE id=?", (bid,))
        return _row(rows[0]) if rows else None

    def bookings(self, limit: int = 300) -> list[dict]:
        return [_row(r) for r in self._q("SELECT * FROM bookings ORDER BY created_at DESC LIMIT ?", (limit,))]

    def group(self, grp: str) -> list[dict]:
        return [_row(r) for r in self._q("SELECT * FROM bookings WHERE grp=? ORDER BY created_at, offer", (grp,))]

    def interrupted(self) -> int:
        """Bookings the app was closed (or crashed) in the middle of: they did not finish - say so honestly."""
        rows = self._q("SELECT id FROM bookings WHERE status IN ('waiting','running')")
        for r in rows:
            self.update(r["id"], status="failed", finished_at=time.time(),
                        error="Best Fare was closed before this booking finished.")
        return len(rows)

    def wipe_personal(self, older_than_s: float = WIPE_AFTER_S, now: Optional[float] = None) -> int:
        cutoff = (now or time.time()) - older_than_s
        with self._lock:
            cur = self._db.execute("UPDATE bookings SET pax_json='[]', phone='', email='', wiped=1 "
                                   "WHERE wiped=0 AND finished_at IS NOT NULL AND finished_at < ?", (cutoff,))
            return cur.rowcount

    def unreported(self) -> list[dict]:
        """Bookings the server took credits for whose result it has not been told yet (the app was closed, the connection was down)."""
        return [_row(r) for r in self._q("SELECT * FROM bookings WHERE cloud_id IS NOT NULL AND COALESCE(cloud_state,'') != 'reported'")]

    def clear_history(self) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM bookings WHERE status NOT IN ('waiting','running')").rowcount

    # ── settings ─────────────────────────────────────────────────────────────────────────────────────
    def setting(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM settings WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except ValueError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        if value is None:
            self._q("DELETE FROM settings WHERE key=?", (key,))
        else:
            self._q("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["travellers"] = json.loads(d.pop("pax_json") or "[]")
    except ValueError:
        d["travellers"] = []
    if d.get("coupon_applied") is not None:
        d["coupon_applied"] = bool(d["coupon_applied"])
    return d
