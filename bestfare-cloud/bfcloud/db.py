"""SQLite storage. One connection, one lock: every change is a short transaction, so two requests can never both spend the last credit.

Everything that talks to the database passes values as parameters - there is no string-built SQL anywhere in the project
(tests/test_security.py tries injection on every text field).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_VERSION = 5

# columns added after the first version: (table, column, definition). An older database gets them when it is opened.
MIGRATIONS = [("admins", "totp_last", "INTEGER NOT NULL DEFAULT 0"), ("bookings", "repeats", "INTEGER NOT NULL DEFAULT 0"), ("bookings", "infants", "INTEGER NOT NULL DEFAULT 0"),
              ("licences", "offers", "TEXT NOT NULL DEFAULT 'offer1,offer2'")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, pw_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin', totp_secret TEXT, totp_on INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, last_login REAL, must_change INTEGER NOT NULL DEFAULT 0,
    totp_last INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    sid TEXT PRIMARY KEY, admin_id INTEGER NOT NULL, csrf TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
    ip TEXT NOT NULL DEFAULT '', ua TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS licences (
    id INTEGER PRIMARY KEY, key_hash TEXT NOT NULL UNIQUE, key_enc TEXT NOT NULL, key_tail TEXT NOT NULL,
    label TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', block_reason TEXT NOT NULL DEFAULT '',
    credits INTEGER NOT NULL DEFAULT 0 CHECK (credits >= 0), created_at REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '',
    expires_at REAL, mac_lock TEXT NOT NULL DEFAULT '', device_pub TEXT, device_id TEXT, device_info TEXT NOT NULL DEFAULT '{}',
    bound_at REAL, last_seen REAL, last_ip TEXT NOT NULL DEFAULT '', epoch INTEGER NOT NULL DEFAULT 1,
    flagged INTEGER NOT NULL DEFAULT 0, flag_reason TEXT NOT NULL DEFAULT '', client_build INTEGER NOT NULL DEFAULT 0,
    offers TEXT NOT NULL DEFAULT 'offer1,offer2'
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY, licence_id INTEGER NOT NULL, ts REAL NOT NULL, delta INTEGER NOT NULL, balance INTEGER NOT NULL,
    kind TEXT NOT NULL, booking_id TEXT, actor TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_ledger_licence ON ledger (licence_id, id);
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY, licence_id INTEGER NOT NULL, ts REAL NOT NULL, travellers INTEGER NOT NULL, offer TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT '', travel_date TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'reserved',
    pnr TEXT, reason TEXT NOT NULL DEFAULT '', finished_at REAL, ip TEXT NOT NULL DEFAULT '', client_ref TEXT,
    refunded INTEGER NOT NULL DEFAULT 0, repeats INTEGER NOT NULL DEFAULT 0, infants INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_bookings_licence ON bookings (licence_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bookings_ref ON bookings (licence_id, client_ref) WHERE client_ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, licence_id INTEGER, actor TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', level TEXT NOT NULL DEFAULT 'info'
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY, build INTEGER NOT NULL UNIQUE, version TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'stable',
    notes TEXT NOT NULL DEFAULT '', mandatory INTEGER NOT NULL DEFAULT 0, rollout INTEGER NOT NULL DEFAULT 100,
    state TEXT NOT NULL DEFAULT 'draft', file_name TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    key_enc TEXT NOT NULL, manifest TEXT NOT NULL, created_at REAL NOT NULL, published_at REAL, uploaded_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS installers (
    id INTEGER PRIMARY KEY, version TEXT NOT NULL UNIQUE, vnum INTEGER NOT NULL UNIQUE, file_name TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    sig TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'draft', mandatory INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, published_at REAL, uploaded_by TEXT NOT NULL DEFAULT ''
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(SCHEMA)
            for table, column, definition in MIGRATIONS:            # (names above are constants, never input)
                have = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One atomic change: everything inside happens, or nothing does."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def one(self, sql: str, args: tuple | list = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchone()

    def all(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    def run(self, sql: str, args: tuple | list = ()) -> int:
        with self._lock:
            return self._db.execute(sql, tuple(args)).rowcount

    def insert(self, sql: str, args: tuple | list = ()) -> int:
        with self._lock:
            return int(self._db.execute(sql, tuple(args)).lastrowid or 0)

    def scalar(self, sql: str, args: tuple | list = (), default: Any = 0) -> Any:
        row = self.one(sql, args)
        return default if row is None or row[0] is None else row[0]
