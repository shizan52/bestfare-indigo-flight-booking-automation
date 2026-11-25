"""The rules of the Best Fare cloud: licences, devices, credits, bookings, releases. No HTTP in here - only decisions.

The principles that make the system hard to misuse:
  * The SERVER is the only place that knows the credits, the offer codes and the licence state. The app cannot spend a credit it
    does not have, because the credit is taken here, before the app is given anything it needs for the booking.
  * A key works on exactly ONE computer: the first activation binds it to the device's own key pair (and records the MAC address);
    any other computer is refused, logged and flagged. Only an admin can move a key.
  * Every change of credits is one atomic step written to a ledger that is never edited.
  * Refunds after a failed booking are automatic, but limited and watched: a client that "fails" suspiciously often loses them.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import bundle, crypto
from .db import Database

DEFAULTS: dict[str, Any] = {
    "maintenance": False, "maintenance_msg": "Best Fare is being updated. Please try again in a few minutes.",
    "broadcast": "", "min_build": 0, "infant_credits": 0, "refund_daily_limit": 12, "refund_ratio_limit": 0.85, "refund_ratio_window": 20, "begin_per_hour": 30,
    "reservation_min": 45, "session_days": 7, "max_open": 3, "max_travellers": 9, "reconcile_days": 7,
    "offers": {
        "offer1": {"title": "Offer 1", "who": "Promotional fare A", "code": "6ETST001", "enabled": True},
        "offer2": {"title": "Offer 2", "who": "Promotional fare B", "code": "PROMO2", "enabled": True},
    },
}
MAX_CREDIT = 100_000
_ROUTE = re.compile(r"^[A-Z]{3}-[A-Z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PNR = re.compile(r"^[A-Z0-9]{5,12}$")
_MAC = re.compile(r"^[0-9a-f]{12}$")


class CloudError(Exception):
    """An answer the caller can act on: a short code for programs, a plain sentence for people."""

    def __init__(self, code: str, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


def clean(text: Any, limit: int = 200) -> str:
    """Text from outside: control characters out, length limited. (Never trusted, never put into SQL by hand.)"""
    s = "".join(ch for ch in str(text if text is not None else "") if ch == " " or (ch.isprintable() and ch not in "\x7f"))
    return s.strip()[:limit]


def norm_mac(text: Any) -> str:
    t = re.sub(r"[^0-9a-fA-F]", "", str(text or "")).lower()
    return ":".join(t[i:i + 2] for i in range(0, 12, 2)) if _MAC.match(t) else ""


class Cloud:
    def __init__(self, data_dir: Path | str, *, release_keys: Optional[list[str]] = None, now: Callable[[], float] = time.time):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        (self.dir / "releases").mkdir(exist_ok=True)
        self.now = now
        # A server that already has users but has lost its secrets must NOT quietly make new ones: every licence key and every installed
        # app would stop working at once. It refuses to start and says what is missing.
        if (self.dir / "cloud.db").exists():
            missing = [n for n in ("identity.pem", "master.key") if not (self.dir / n).exists()]
            if missing:
                raise RuntimeError(f"{', '.join(missing)} missing from {self.dir} although the database exists. Restore from the backup - "
                                   "a new one would make every key and every installed app stop working.")
        self.db = Database(self.dir / "cloud.db")
        self.identity = crypto.Signer.load_or_create(self.dir / "identity.pem")
        master = crypto.load_or_create_secret(self.dir / "master.key")
        self._pepper, self._seal, self._token = (crypto.subkey(master, p) for p in ("pepper", "seal", "token"))
        keys_file = self.dir / "release_keys.txt"                     # the developer's PUBLIC release keys this server accepts (one per line)
        self.release_keys: list[str] = list(release_keys) if release_keys is not None else (
            [ln.strip() for ln in keys_file.read_text().splitlines() if ln.strip() and not ln.startswith("#")] if keys_file.exists() else [])
        self._lock = threading.RLock()

    def close(self) -> None:
        self.db.close()

    # ── settings ───────────────────────────────────────────────────────────────────────────────────────────────────
    def setting(self, key: str) -> Any:
        """A setting's value. Always a COPY: whoever changes what they get back changes nothing for anyone else."""
        row = self.db.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return copy.deepcopy(DEFAULTS[key])
        try:
            return json.loads(row["value"])
        except ValueError:
            return copy.deepcopy(DEFAULTS[key])

    def set_setting(self, key: str, value: Any, actor: str = "") -> None:
        if key not in DEFAULTS:
            raise CloudError("bad_setting", "Unknown setting.")
        self._check_setting(key, value)
        self.db.run("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))
        self.event("setting", actor=actor, detail=f"{key} changed")

    def set_settings(self, changes: dict, actor: str = "") -> None:
        """Several settings at once: every value is checked first, and either all of them are saved or none."""
        if not isinstance(changes, dict) or not changes:
            raise CloudError("bad_setting", "Nothing to change.")
        for k, v in changes.items():
            if k not in DEFAULTS:
                raise CloudError("bad_setting", "Unknown setting.")
            self._check_setting(k, v)
        with self.db.tx() as c:
            for k, v in changes.items():
                c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v, ensure_ascii=False)))
        for k in changes:
            self.event("setting", actor=actor, detail=f"{k} changed")

    def _check_setting(self, key: str, value: Any) -> None:
        d = DEFAULTS[key]
        if key == "infant_credits":                                                             # an infant costs nothing or the same as everyone
            if isinstance(value, bool) or value not in (0, 1):
                raise CloudError("bad_setting", f"The value for '{key}' is not acceptable.")
            return
        if isinstance(d, bool):
            ok = isinstance(value, bool)
        elif isinstance(d, int):
            ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000
        elif isinstance(d, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1
        elif isinstance(d, str):
            ok = isinstance(value, str) and len(value) <= 600
        else:                                                                                   # offers
            ok = isinstance(value, dict) and 1 <= len(value) <= 6 and all(
                isinstance(k, str) and re.match(r"^offer[1-9]$", k) and isinstance(v, dict)
                and isinstance(v.get("code"), str) and re.match(r"^[A-Za-z0-9]{3,20}$", v["code"])
                and isinstance(v.get("title"), str) and 0 < len(v["title"]) <= 30 and isinstance(v.get("who"), str) and len(v["who"]) <= 60
                for k, v in value.items())
            if ok:
                for v in value.values():
                    v["enabled"] = bool(v.get("enabled", True))
        if not ok:
            raise CloudError("bad_setting", f"The value for '{key}' is not acceptable.")

    def all_settings(self) -> dict[str, Any]:
        return {k: self.setting(k) for k in DEFAULTS}

    # ── events (the audit trail) ───────────────────────────────────────────────────────────────────────────────────
    def event(self, kind: str, *, licence_id: Optional[int] = None, actor: str = "", ip: str = "", detail: str = "", level: str = "info") -> None:
        self.db.insert("INSERT INTO events (ts, kind, licence_id, actor, ip, detail, level) VALUES (?,?,?,?,?,?,?)",
                       (self.now(), clean(kind, 40), licence_id, clean(actor, 60), clean(ip, 64), clean(detail, 400), level))
        if self.db.scalar("SELECT COUNT(*) FROM events") > 60_000:
            self.db.run("DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id LIMIT 10000)")

    # ── licences: admin side ───────────────────────────────────────────────────────────────────────────────────────
    def create_licence(self, label: str, credits: int = 0, note: str = "", expires_days: Optional[int] = None, mac_lock: str = "",
                       actor: str = "", offers: Any = None) -> dict:
        label = clean(label, 60)
        allowed = self._clean_offers(offers)
        if not label:
            raise CloudError("bad_label", "Give this user a name.")
        credits = self._amount(credits, allow_zero=True)
        mac = norm_mac(mac_lock) if mac_lock else ""
        if mac_lock and not mac:
            raise CloudError("bad_mac", "That MAC address is not valid (like 3C:52:82:AA:BB:CC).")
        for _ in range(8):
            key = crypto.new_licence_key()
            h = crypto.key_lookup_hash(self._pepper, key)
            if not self.db.one("SELECT 1 FROM licences WHERE key_hash=?", (h,)):
                break
        expires = self.now() + int(expires_days) * 86400 if expires_days else None
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO licences (key_hash, key_enc, key_tail, label, note, credits, created_at, created_by, expires_at, mac_lock, offers) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (h, crypto.seal(self._seal, key.encode(), b"licence-key"), key[-4:], label, clean(note, 500), 0,
                             self.now(), clean(actor, 60), expires, mac, allowed))
            lid = int(cur.lastrowid or 0)
            if credits:
                self._ledger(c, lid, credits, "grant", actor=actor, note="first credits")
        self.event("licence_created", licence_id=lid, actor=actor, detail=f"{label} with {credits} credits, offers {allowed}")
        return {"id": lid, "key": key, "label": label, "credits": credits, "offers": allowed.split(",")}

    def offer_keys(self) -> list[str]:
        """Every offer that exists (the admin's own list in Settings), in order: offer1, offer2 ..."""
        return sorted(self.setting("offers"))

    def _clean_offers(self, offers: Any) -> str:
        """The offers a key may use, as stored: 'offer1,offer2'. At least one, and only offers that exist."""
        if offers is None:
            return ",".join(self.offer_keys())
        if not isinstance(offers, (list, tuple)) or not all(isinstance(x, str) for x in offers):
            raise CloudError("bad_offers", "Choose which offers this user may use.")
        known = self.offer_keys()
        picked = [k for k in known if k in set(offers)]
        if not picked or len(set(offers)) != len(picked):
            raise CloudError("bad_offers", "Choose at least one offer, from the list of offers.")
        return ",".join(picked)

    @staticmethod
    def offers_of(row) -> list[str]:
        return [k for k in str(row["offers"] or "").split(",") if k]

    def _amount(self, n: Any, allow_zero: bool = False) -> int:
        if isinstance(n, bool) or not isinstance(n, int):
            raise CloudError("bad_amount", "The number of credits must be a whole number.")
        if n < (0 if allow_zero else 1) or n > MAX_CREDIT:
            raise CloudError("bad_amount", f"The number of credits must be between {0 if allow_zero else 1} and {MAX_CREDIT}.")
        return n

    def _ledger(self, c, lid: int, delta: int, kind: str, *, booking_id: Optional[str] = None, actor: str = "", note: str = "") -> int:
        """Change the credits AND write the ledger line in the same transaction. The database refuses a negative balance."""
        cur = c.execute("UPDATE licences SET credits = credits + ? WHERE id=? AND credits + ? >= 0", (delta, lid, delta))
        if cur.rowcount != 1:
            raise CloudError("no_credits", "Not enough credits.")
        bal = c.execute("SELECT credits FROM licences WHERE id=?", (lid,)).fetchone()[0]
        c.execute("INSERT INTO ledger (licence_id, ts, delta, balance, kind, booking_id, actor, note) VALUES (?,?,?,?,?,?,?,?)",
                  (lid, self.now(), delta, bal, kind, booking_id, clean(actor, 60), clean(note, 200)))
        return int(bal)

    def adjust_credits(self, lid: int, delta: int, note: str = "", actor: str = "") -> int:
        """An admin adds (+) or takes away (-) credits. Returns the new balance."""
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0 or abs(delta) > MAX_CREDIT:
            raise CloudError("bad_amount", f"Enter a whole number of credits other than zero (at most {MAX_CREDIT}).")
        self._licence(lid)
        try:
            with self.db.tx() as c:
                bal = self._ledger(c, lid, delta, "grant" if delta > 0 else "remove", actor=actor, note=note)
                if bal > MAX_CREDIT * 10:
                    raise CloudError("bad_amount", "That would be too many credits.")
        except CloudError as e:
            if e.code == "no_credits":
                raise CloudError("no_credits", "The user has fewer credits than you are taking away.") from e
            raise
        self.event("credits", licence_id=lid, actor=actor, detail=f"{delta:+d} credits. {clean(note, 100)}".strip())
        return bal

    def set_status(self, lid: int, status: str, reason: str = "", actor: str = "") -> None:
        if status not in ("active", "blocked"):
            raise CloudError("bad_status", "Unknown status.")
        self._licence(lid)
        reason = clean(reason, 200)
        # blocking also ends every session at once (the epoch is part of the session token)
        self.db.run("UPDATE licences SET status=?, block_reason=?, epoch = epoch + ? WHERE id=?",
                    (status, reason if status == "blocked" else "", 1 if status == "blocked" else 0, lid))
        self.event("blocked" if status == "blocked" else "unblocked", licence_id=lid, actor=actor, detail=reason, level="warn" if status == "blocked" else "info")

    def reset_device(self, lid: int, actor: str = "") -> None:
        """Let the key be activated on a NEW computer (the old one stops working at once)."""
        self._licence(lid)
        self.db.run("UPDATE licences SET device_pub=NULL, device_id=NULL, device_info='{}', bound_at=NULL, epoch = epoch + 1, flagged=0, flag_reason='' WHERE id=?", (lid,))
        self.event("device_reset", licence_id=lid, actor=actor, detail="the key can now be used on a new computer")

    def update_licence(self, lid: int, *, label: Optional[str] = None, note: Optional[str] = None, expires_days: Optional[int] = None,
                       clear_expiry: bool = False, mac_lock: Optional[str] = None, actor: str = "", offers: Any = None) -> None:
        old = self._licence(lid)
        # 1) check everything first (a bad value changes nothing), 2) change only what is really different, 3) write down exactly what changed
        allowed = self._clean_offers(offers) if offers is not None else None
        new_label = None
        if label is not None:
            new_label = clean(label, 60)
            if not new_label:
                raise CloudError("bad_label", "Give this user a name.")
        new_expiry: Any = "keep"
        if clear_expiry:
            new_expiry = None
        elif expires_days is not None:
            if isinstance(expires_days, bool) or not isinstance(expires_days, int) or not 0 < expires_days <= 3650:
                raise CloudError("bad_expiry", "Enter the number of days (1 to 3650).")
            new_expiry = self.now() + expires_days * 86400
        new_mac = None
        if mac_lock is not None:
            new_mac = norm_mac(mac_lock) if mac_lock.strip() else ""
            if mac_lock.strip() and not new_mac:
                raise CloudError("bad_mac", "That MAC address is not valid (like 3C:52:82:AA:BB:CC).")
        said: list[str] = []
        if allowed is not None and allowed != (old["offers"] or ""):
            self.db.run("UPDATE licences SET offers=? WHERE id=?", (allowed, lid))
            self.event("offers_changed", licence_id=lid, actor=actor, detail=f"may use {allowed}")
        if new_label is not None and new_label != old["label"]:
            self.db.run("UPDATE licences SET label=? WHERE id=?", (new_label, lid))
            said.append(f"name {old['label']} -> {new_label}")
        if note is not None and clean(note, 500) != (old["note"] or ""):
            self.db.run("UPDATE licences SET note=? WHERE id=?", (clean(note, 500), lid))
            said.append("note")
        if new_expiry is None and old["expires_at"] is not None:
            self.db.run("UPDATE licences SET expires_at=NULL WHERE id=?", (lid,))
            said.append("no expiry any more")
        elif new_expiry != "keep" and new_expiry is not None:
            self.db.run("UPDATE licences SET expires_at=? WHERE id=?", (new_expiry, lid))
            said.append(f"valid for {expires_days} more days")
        if new_mac is not None and new_mac != (old["mac_lock"] or ""):
            self.db.run("UPDATE licences SET mac_lock=?, epoch = epoch + 1 WHERE id=?", (new_mac, lid))      # (a new lock ends the sessions: the computer is checked again)
            said.append(f"MAC lock set to {new_mac}" if new_mac else "MAC lock removed")
        if said:
            self.event("licence_edited", licence_id=lid, actor=actor, detail="; ".join(said))

    def regenerate_key(self, lid: int, actor: str = "") -> str:
        """A key was leaked: issue a new one. The old key stops working and the computer must activate again."""
        self._licence(lid)
        key = crypto.new_licence_key()
        self.db.run("UPDATE licences SET key_hash=?, key_enc=?, key_tail=?, device_pub=NULL, device_id=NULL, device_info='{}', bound_at=NULL, epoch = epoch + 1 WHERE id=?",
                    (crypto.key_lookup_hash(self._pepper, key), crypto.seal(self._seal, key.encode(), b"licence-key"), key[-4:], lid))
        self.event("key_regenerated", licence_id=lid, actor=actor, level="warn")
        return key

    def reveal_key(self, lid: int, actor: str = "") -> str:
        row = self._licence(lid)
        self.event("key_viewed", licence_id=lid, actor=actor)
        return crypto.unseal(self._seal, row["key_enc"], b"licence-key").decode()

    def delete_licence(self, lid: int, actor: str = "") -> None:
        row = self._licence(lid)
        with self.db.tx() as c:
            for t in ("ledger", "bookings"):
                c.execute(f"DELETE FROM {t} WHERE licence_id=?", (lid,))            # (the table name is a constant above, never input)
            c.execute("DELETE FROM licences WHERE id=?", (lid,))
        self.event("licence_deleted", actor=actor, detail=f"{row['label']} (…{row['key_tail']})", level="warn")

    def _licence(self, lid: Any):
        if isinstance(lid, bool) or not isinstance(lid, int) or not 0 < lid < 2 ** 62:
            raise CloudError("not_found", "No such user.", 404)
        row = self.db.one("SELECT * FROM licences WHERE id=?", (lid,))
        if row is None:
            raise CloudError("not_found", "No such user.", 404)
        return row

    # ── licences: reading (admin) ──────────────────────────────────────────────────────────────────────────────────
    def _status_change(self, r) -> dict:
        """Who stopped this user, and when (the last 'blocked' event): so the panel can say 'Blocked by boss, 3 minutes ago' instead of only 'Blocked'."""
        if r["status"] != "blocked":
            return {"status_by": "", "status_at": None}
        e = self.db.one("SELECT actor, ts FROM events WHERE licence_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1", (r["id"],))
        return {"status_by": (e["actor"] if e else "") or "", "status_at": e["ts"] if e else None}

    def _view(self, r) -> dict:
        info = json.loads(r["device_info"] or "{}")
        now = self.now()
        return {**self._status_change(r), "id": r["id"], "label": r["label"], "note": r["note"], "key_tail": r["key_tail"], "status": r["status"],
                "block_reason": r["block_reason"], "credits": r["credits"], "created_at": r["created_at"], "created_by": r["created_by"],
                "expires_at": r["expires_at"], "expired": bool(r["expires_at"] and r["expires_at"] < now), "mac_lock": r["mac_lock"],
                "bound": bool(r["device_id"]), "device": info, "bound_at": r["bound_at"], "last_seen": r["last_seen"],
                "online": bool(r["last_seen"] and now - r["last_seen"] < 600), "last_ip": r["last_ip"], "flagged": bool(r["flagged"]),
                "flag_reason": r["flag_reason"], "client_build": r["client_build"], "offers": self.offers_of(r)}

    def list_licences(self, search: str = "", flt: str = "all", limit: int = 200, offset: int = 0) -> dict:
        where, args = ["1=1"], []
        s = clean(search, 60)
        if s:
            like = "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(label LIKE ? ESCAPE '\\' OR key_tail LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\' OR last_ip LIKE ? ESCAPE '\\' "
                         "OR device_info LIKE ? ESCAPE '\\')")
            args += [like] * 5
        now = self.now()
        flt_sql = {"active": "status='active'", "blocked": "status='blocked'", "nocredits": "credits=0", "unbound": "device_id IS NULL",
                   "flagged": "flagged=1", "online": "last_seen > %f" % (now - 600), "expired": "expires_at IS NOT NULL AND expires_at < %f" % now}
        if flt in flt_sql:
            where.append(flt_sql[flt])                                  # (a fixed fragment chosen from a dict - never the caller's text)
        elif flt in self.offer_keys():                                  # "who may use offer 2": the name was checked against the real offers
            where.append("(',' || offers || ',') LIKE ?")
            args.append(f"%,{flt},%")
        limit, offset = max(1, min(int(limit), 500)), max(0, min(int(offset), 10 ** 9))
        rows = self.db.all(f"SELECT * FROM licences WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ? OFFSET ?", args + [limit, offset])
        total = self.db.scalar(f"SELECT COUNT(*) FROM licences WHERE {' AND '.join(where)}", args)
        out = []
        for r in rows:
            v = self._view(r)
            v["bookings"] = self.db.scalar("SELECT COUNT(*) FROM bookings WHERE licence_id=? AND status='done'", (r["id"],))
            out.append(v)
        return {"items": out, "total": total}

    def licence_detail(self, lid: int) -> dict:
        r = self._licence(lid)
        v = self._view(r)
        v["ledger"] = [dict(x) for x in self.db.all("SELECT ts, delta, balance, kind, booking_id, actor, note FROM ledger WHERE licence_id=? ORDER BY id DESC LIMIT 100", (lid,))]
        sums = {x["kind"]: x["total"] for x in self.db.all("SELECT kind, SUM(delta) AS total FROM ledger WHERE licence_id=? GROUP BY kind", (lid,))}
        v["usage"] = {"given": int(sums.get("grant", 0)), "removed": -int(sums.get("remove", 0)), "used": -int(sums.get("booking", 0)), "returned": int(sums.get("refund", 0)),
                      "kept": -int(sums.get("booking", 0)) - int(sums.get("refund", 0))}          # (kept = credits really spent: used minus the ones that came back)
        v["bookings_list"] = [dict(x) for x in self.db.all(
            "SELECT id, ts, travellers, offer, route, travel_date, status, pnr, reason, refunded, infants FROM bookings WHERE licence_id=? ORDER BY ts DESC LIMIT 100", (lid,))]
        v["events"] = [dict(x) for x in self.db.all(
            "SELECT ts, kind, actor, ip, detail, level FROM events WHERE licence_id=? ORDER BY id DESC LIMIT 60", (lid,))]
        return v

    def events(self, level: str = "", kind: str = "", limit: int = 200) -> list[dict]:
        where, args = ["1=1"], []
        if level in ("info", "warn", "alert"):
            where.append("e.level=?"); args.append(level)
        if kind:
            where.append("e.kind=?"); args.append(clean(kind, 40))
        rows = self.db.all("SELECT e.id, e.ts, e.kind, e.licence_id, e.actor, e.ip, e.detail, e.level, l.label FROM events e "
                           f"LEFT JOIN licences l ON l.id=e.licence_id WHERE {' AND '.join(where)} ORDER BY e.id DESC LIMIT ?",
                           args + [max(1, min(int(limit), 500))])
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        now = self.now()
        day = now - 86400
        db = self.db
        return {
            "users": db.scalar("SELECT COUNT(*) FROM licences"), "active": db.scalar("SELECT COUNT(*) FROM licences WHERE status='active'"),
            "blocked": db.scalar("SELECT COUNT(*) FROM licences WHERE status='blocked'"),
            "online": db.scalar("SELECT COUNT(*) FROM licences WHERE last_seen > ?", (now - 600,)),
            "no_credits": db.scalar("SELECT COUNT(*) FROM licences WHERE credits=0 AND status='active'"),
            "unbound": db.scalar("SELECT COUNT(*) FROM licences WHERE device_id IS NULL"),
            "flagged": db.scalar("SELECT COUNT(*) FROM licences WHERE flagged=1"),
            "credits_total": db.scalar("SELECT SUM(credits) FROM licences"),
            "bookings_total": db.scalar("SELECT COUNT(*) FROM bookings WHERE status='done'"),
            "bookings_today": db.scalar("SELECT COUNT(*) FROM bookings WHERE status='done' AND ts > ?", (day,)),
            "failed_today": db.scalar("SELECT COUNT(*) FROM bookings WHERE status='failed' AND ts > ?", (day,)),
            "open": db.scalar("SELECT COUNT(*) FROM bookings WHERE status IN ('reserved','unconfirmed')"),
            "credits_used_week": -db.scalar("SELECT SUM(delta) FROM ledger WHERE kind IN ('booking','refund') AND ts > ?", (now - 7 * 86400,)),
            "alerts_today": db.scalar("SELECT COUNT(*) FROM events WHERE level IN ('warn','alert') AND ts > ?", (day,)),
            "daily": [{"day": time.strftime("%d %b", time.gmtime(now - i * 86400)),
                       "done": db.scalar("SELECT COUNT(*) FROM bookings WHERE status='done' AND ts > ? AND ts <= ?", (now - (i + 1) * 86400, now - i * 86400))}
                      for i in range(6, -1, -1)],
        }

    # ── the app: activation and session ────────────────────────────────────────────────────────────────────────────
    def _sanitize_device(self, info: Any) -> dict:
        if not isinstance(info, dict):
            raise CloudError("bad_request", "Device details are missing.")
        macs = []
        for m in (info.get("macs") or [])[:8]:
            n = norm_mac(m)
            if n and n not in macs and n not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
                macs.append(n)
        guid = clean(info.get("guid"), 64)
        return {"macs": macs, "name": clean(info.get("name"), 60), "os": clean(info.get("os"), 80),
                "guid": guid if re.fullmatch(r"[0-9a-f]{16,64}", guid or "") else "", "app": clean(info.get("app"), 30)}

    def activate(self, key_text: str, device_pub: str, device_info: Any, ip: str, build: int, auto: bool = False) -> dict:
        """`auto` = the app signs in again BY ITSELF with the key it has saved (a token ran out); a person typing the key is `auto=False`."""
        key = crypto.normalize_key(key_text)
        if not key or not crypto.valid_public_key(device_pub):
            self.event("bad_key", ip=ip, detail="malformed key", level="warn")
            raise CloudError("bad_key", "That key is not valid. Please check it. If your admin made you a new key, type the new one.")
        row = self.db.one("SELECT * FROM licences WHERE key_hash=?", (crypto.key_lookup_hash(self._pepper, key),))
        if row is None:
            self.event("bad_key", ip=ip, detail="unknown key …" + key[-4:], level="warn")
            raise CloudError("bad_key", "That key is not valid. Please check it. If your admin made you a new key, type the new one.")
        lid = row["id"]
        info = self._sanitize_device(device_info)
        self._check_usable(row, ip)
        if row["mac_lock"] and row["mac_lock"] not in info["macs"]:
            self.event("mac_refused", licence_id=lid, ip=ip, detail="MACs " + ", ".join(info["macs"]) + f" (key is locked to {row['mac_lock']})", level="alert")
            raise CloudError("mac_lock", "This key is locked to a different computer. Please contact your admin.", 403)
        dev_id = hashlib.sha256(device_pub.encode()).hexdigest()[:24]
        if auto and not row["device_pub"]:
            # The admin moved this key (or it never had a computer): a computer that still remembers it must NOT silently take it back -
            # whoever calls first would win, and the old computer would keep a key that was just taken from it.
            raise CloudError("rebind_needed", "This key was moved to another computer. Please type your key again to use it on this computer.", 403)
        if row["device_pub"] and row["device_pub"] != device_pub:
            old = json.loads(row["device_info"] or "{}")
            self.db.run("UPDATE licences SET flagged=1, flag_reason=? WHERE id=?", ("a second computer tried to use this key", lid))
            self.event("device_refused", licence_id=lid, ip=ip, level="alert",
                       detail=f"another computer tried this key: {info['name']} {', '.join(info['macs'])} (bound: {old.get('name', '?')} {', '.join(old.get('macs', []))})")
            raise CloudError("device_mismatch", "This key is already in use on another computer. Each key works on one computer only - "
                                                "ask your admin to move it.", 403)
        now = self.now()
        if not row["device_pub"]:
            self.db.run("UPDATE licences SET device_pub=?, device_id=?, device_info=?, bound_at=? WHERE id=?",
                        (device_pub, dev_id, json.dumps(info), now, lid))
            self.event("device_bound", licence_id=lid, ip=ip, detail=f"{info['name']} {', '.join(info['macs'])}")
        else:
            self.db.run("UPDATE licences SET device_info=? WHERE id=?", (json.dumps(info), lid))
        self._seen(lid, ip, build)
        return self._session_for(self.db.one("SELECT * FROM licences WHERE id=?", (lid,)))

    def check_usable(self, row) -> None:
        """Blocked or expired licences get nothing - not the program, not a key to open it."""
        self._check_usable(self.db.one("SELECT * FROM licences WHERE id=?", (row["id"],)) or row)

    def _check_usable(self, row, ip: str = "") -> None:
        if row["status"] != "active":
            raise CloudError("blocked", row["block_reason"] or "Your access has been stopped. Please contact your admin.", 403)
        if row["expires_at"] and row["expires_at"] < self.now():
            raise CloudError("expired", "Your licence has expired. Please contact your admin to renew it.", 403)

    def _seen(self, lid: int, ip: str, build: int) -> None:
        now, ip = self.now(), clean(ip, 64)
        prev = self.db.one("SELECT last_seen, last_ip FROM licences WHERE id=?", (lid,))
        if (prev and prev["last_ip"] and ip and prev["last_ip"] != ip and prev["last_seen"] and now - prev["last_seen"] < 600
                and (":" in ip) == (":" in prev["last_ip"])):             # (an IPv4 <-> IPv6 switch of one computer is not a second computer)
            self.event("ip_change", licence_id=lid, ip=ip, detail=f"from {prev['last_ip']}")
            turns = self.db.scalar("SELECT COUNT(*) FROM events WHERE kind='ip_change' AND licence_id=? AND ts > ?", (lid, now - 1800))
            if turns >= 4 and not self.db.scalar("SELECT COUNT(*) FROM events WHERE kind='sharing_suspected' AND licence_id=? AND ts > ?", (lid, now - 3600)):
                self.db.run("UPDATE licences SET flagged=1, flag_reason=? WHERE id=?", ("used from different internet addresses at the same time - the key may be shared", lid))
                self.event("sharing_suspected", licence_id=lid, ip=ip, detail=f"{turns} address changes in 30 minutes", level="alert")
        self.db.run("UPDATE licences SET last_seen=?, last_ip=?, client_build=? WHERE id=?",
                    (self.now(), clean(ip, 64), int(build) if isinstance(build, int) and not isinstance(build, bool) and 0 <= build < 10**7 else 0, lid))

    def _session_for(self, row) -> dict:
        ttl = int(self.setting("session_days")) * 86400
        token = crypto.token_make(self._token, {"l": row["id"], "d": row["device_id"], "e": row["epoch"], "exp": self.now() + ttl})
        return {"token": token, "account": self.account(row)}

    def authorize(self, token: str) -> tuple[Any, dict]:
        """The licence a genuine, current session token belongs to. (The device signature is checked by the caller with its key.)"""
        payload = crypto.token_check(self._token, token, self.now())
        if not payload:
            raise CloudError("unauthorized", "Please sign in again.", 401)
        row = self.db.one("SELECT * FROM licences WHERE id=?", (payload.get("l"),))
        if row is None or row["epoch"] != payload.get("e") or row["device_id"] != payload.get("d") or not row["device_pub"]:
            raise CloudError("unauthorized", "Please sign in again.", 401)
        return row, payload

    def account(self, row) -> dict:
        row = self.db.one("SELECT * FROM licences WHERE id=?", (row["id"],))
        mine = set(self.offers_of(row))                          # the user sees only the offers they may use, and only as "Offer 1" / "Offer 2" - never what an offer is
        offers = {k: {"enabled": bool(v["enabled"])} for k, v in sorted(self.setting("offers").items()) if k in mine}
        return {"label": row["label"], "credits": row["credits"], "status": row["status"], "key_tail": row["key_tail"],
                "expires_at": row["expires_at"], "broadcast": self.setting("broadcast"), "offers": offers,
                "maintenance": bool(self.setting("maintenance")), "maintenance_msg": self.setting("maintenance_msg"), "infant_credits": self.setting("infant_credits"),
                "min_build": self.setting("min_build"), "server_time": self.now()}

    def session(self, row, ip: str, build: int) -> dict:
        self._check_usable(row, ip)
        self._seen(row["id"], ip, build)
        return self._session_for(row)

    # ── bookings: the credit is taken HERE, before the app is given what it needs ──────────────────────────────────
    def begin_booking(self, row, n: Any, offer: Any, route: Any, date: Any, client_ref: Any, ip: str, build: int, infants: Any = 0) -> dict:
        self._check_usable(row, ip)
        if self.setting("maintenance"):
            raise CloudError("maintenance", self.setting("maintenance_msg"), 503)
        if isinstance(build, int) and build < self.setting("min_build"):
            raise CloudError("update_required", "A new version of Best Fare is needed. It will update itself - please restart the app.", 426)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= int(self.setting("max_travellers")):
            raise CloudError("bad_request", "The number of travellers is not valid.")
        # infants ride on an adult's lap: at most one per traveller, and the admin decides what they cost (default: a credit each, like every traveller)
        if isinstance(infants, bool) or not isinstance(infants, int) or not 0 <= infants <= n:
            raise CloudError("bad_request", "The number of infants is not valid.")
        charge = n + infants * int(self.setting("infant_credits"))
        offers = self.setting("offers")
        if not isinstance(offer, str) or offer not in offers:
            raise CloudError("bad_request", "That offer is not available.")
        if offer not in self.offers_of(row):
            self.event("offer_refused", licence_id=row["id"], ip=ip, detail=f"asked for {offer}, may use {row['offers']}", level="warn")
            raise CloudError("offer_not_allowed", f"Offer {offer[5:]} is not available for your key.", 403)
        if not offers[offer]["enabled"]:
            raise CloudError("offer_off", f"Offer {offer[5:]} is not available right now.", 409)
        route = clean(route, 7).upper()
        date = clean(date, 10)
        if not _ROUTE.match(route) or not _DATE.match(date):
            raise CloudError("bad_request", "The route or the date is not valid.")
        ref = clean(client_ref, 40) or None
        lid = row["id"]
        with self.db.tx() as c:
            if ref:
                dup = c.execute("SELECT * FROM bookings WHERE licence_id=? AND client_ref=?", (lid, ref)).fetchone()
                if dup is not None:                                                    # (the same request again: the same answer, no second charge)
                    if dup["status"] != "reserved" or dup["repeats"] >= 2:             # ... but only while that booking is still open, and only a couple of times
                        raise CloudError("ref_used", "That booking was already started. Please start a new booking.", 409)
                    c.execute("UPDATE bookings SET repeats = repeats + 1 WHERE id=?", (dup["id"],))
                    cur = c.execute("SELECT credits FROM licences WHERE id=?", (lid,)).fetchone()[0]
                    return {"booking_id": dup["id"], "promo": offers[dup["offer"]]["code"] if dup["offer"] in offers else "", "credits": cur,
                            "charged": dup["travellers"], "repeat": True}
            open_n = c.execute("SELECT COUNT(*) FROM bookings WHERE licence_id=? AND status='reserved' AND ts > ?",
                               (lid, self.now() - int(self.setting("reservation_min")) * 60)).fetchone()[0]
            if open_n >= int(self.setting("max_open")):
                raise CloudError("busy", "You already have bookings in progress. Please wait for them to finish.", 429)
            booking_id = crypto.random_token(12)
            try:
                bal = self._ledger(c, lid, -charge, "booking", booking_id=booking_id,
                                   note=f"{n} traveller(s){f' + {infants} infant(s)' if infants else ''} {route} {date}")
            except CloudError:
                have = c.execute("SELECT credits FROM licences WHERE id=?", (lid,)).fetchone()[0]
                raise CloudError("no_credits", f"This booking needs {charge} credit{'s' if charge != 1 else ''} but you have {have}. "
                                               "Please ask your admin for more credits.", 402, needed=charge, have=have)
            c.execute("INSERT INTO bookings (id, licence_id, ts, travellers, offer, route, travel_date, ip, client_ref, infants) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (booking_id, lid, self.now(), charge, offer, route, date, clean(ip, 64), ref, infants))
        self._seen(lid, ip, build)
        return {"booking_id": booking_id, "promo": offers[offer]["code"], "credits": bal, "charged": charge}

    def finish_booking(self, row, booking_id: Any, outcome: Any, pnr: Any, reason: Any, ip: str) -> dict:
        if not isinstance(booking_id, str) or outcome not in ("done", "failed"):
            raise CloudError("bad_request", "The booking report is not valid.")
        lid = row["id"]
        pnr_c = clean(pnr, 12).upper()
        reason_c = clean(reason, 80)
        with self.db.tx() as c:
            b = c.execute("SELECT * FROM bookings WHERE id=? AND licence_id=?", (booking_id, lid)).fetchone()
            if b is None:
                raise CloudError("not_found", "No such booking.", 404)
            credits = c.execute("SELECT credits FROM licences WHERE id=?", (lid,)).fetchone()[0]
            if b["status"] in ("done", "failed"):                                       # already reported: repeating changes nothing
                return {"status": b["status"], "refunded": bool(b["refunded"]), "credits": credits, "repeat": True}
            now = self.now()
            if outcome == "done":
                if pnr_c and not _PNR.match(pnr_c):
                    raise CloudError("bad_request", "The booking reference is not valid.")
                c.execute("UPDATE bookings SET status='done', pnr=?, finished_at=?, reason=? WHERE id=?",      # (a booking that worked keeps the app's few words, e.g. 'worked on try 2 (fetch@search)': the admin sees how often IndiGo had to be asked again)
                          (pnr_c or None, now, reason_c, booking_id))
                return {"status": "done", "refunded": False, "credits": credits}
            refund, why = self._may_refund(c, lid, b, now)
            if refund:
                credits = self._ledger(c, lid, b["travellers"], "refund", booking_id=booking_id, note=reason_c or "booking failed")
            c.execute("UPDATE bookings SET status='failed', reason=?, finished_at=?, refunded=? WHERE id=?",
                      (reason_c, now, 1 if refund else 0, booking_id))
        if not refund:
            self.db.run("UPDATE licences SET flagged=1, flag_reason=? WHERE id=?", (why, lid))
            self.event("refund_denied", licence_id=lid, ip=ip, detail=f"{why} (booking {booking_id[:6]})", level="alert")
        return {"status": "failed", "refunded": refund, "credits": credits,
                **({} if refund else {"note": "Credits for this booking were not returned automatically. Your admin has been told."})}

    def _may_refund(self, c, lid: int, b, now: float) -> tuple[bool, str]:
        """Automatic refunds are for honest failures: limited per day, and withheld from a client that 'fails' almost every time."""
        if now - b["ts"] > int(self.setting("reconcile_days")) * 86400:
            return False, "the report came too late for an automatic refund"
        n24 = c.execute("SELECT COUNT(*) FROM bookings WHERE licence_id=? AND refunded=1 AND finished_at > ?", (lid, now - 86400)).fetchone()[0]
        if n24 >= int(self.setting("refund_daily_limit")):
            return False, f"more than {self.setting('refund_daily_limit')} automatic refunds in 24 hours"
        window = int(self.setting("refund_ratio_window"))
        recent = c.execute("SELECT status FROM bookings WHERE licence_id=? AND status IN ('done','failed') ORDER BY finished_at DESC LIMIT ?",
                           (lid, window)).fetchall()
        if len(recent) >= max(10, (window * 3) // 4) and sum(1 for r in recent if r["status"] == "failed") / len(recent) >= float(self.setting("refund_ratio_limit")):
            return False, "almost every recent booking was reported as failed"
        return True, ""

    def expire_reservations(self) -> int:
        """Reservations the app never reported on (it crashed, the computer was switched off): they stay charged, but are marked 'unconfirmed'
        for the admin to see; the app reports them when it next starts (finish_booking accepts that for several days)."""
        cut = self.now() - int(self.setting("reservation_min")) * 60
        n = self.db.run("UPDATE bookings SET status='unconfirmed' WHERE status='reserved' AND ts < ?", (cut,))
        if n:
            self.event("unconfirmed", detail=f"{n} booking(s) were never reported on", level="warn")
        return n

    def open_bookings(self, row) -> list[dict]:
        rows = self.db.all("SELECT id, ts, travellers, offer, route, travel_date, status FROM bookings WHERE licence_id=? AND status IN ('reserved','unconfirmed') "
                           "ORDER BY ts DESC LIMIT 20", (row["id"],))
        return [dict(r) for r in rows]

    def admin_refund_booking(self, booking_id: str, actor: str = "") -> dict:
        """An admin gives the credits of one booking back (once)."""
        with self.db.tx() as c:
            b = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
            if b is None:
                raise CloudError("not_found", "No such booking.", 404)
            if b["refunded"]:
                raise CloudError("already", "These credits were already returned.", 409)
            bal = self._ledger(c, b["licence_id"], b["travellers"], "refund", booking_id=booking_id, actor=actor, note="returned by an admin")
            c.execute("UPDATE bookings SET refunded=1, status=CASE WHEN status IN ('reserved','unconfirmed') THEN 'failed' ELSE status END WHERE id=?", (booking_id,))
        self.event("admin_refund", licence_id=b["licence_id"], actor=actor, detail=f"{b['travellers']} credit(s) of booking {booking_id[:6]}")
        return {"credits": bal}

    # ── releases ───────────────────────────────────────────────────────────────────────────────────────────────────
    def add_release(self, data: bytes, content_key_b64: str, notes: str = "", actor: str = "") -> dict:
        try:
            manifest, _payload = bundle.read_bundle(data, self.release_keys)
        except bundle.BundleError as e:
            self.event("release_refused", actor=actor, detail=str(e), level="alert")
            raise CloudError("bad_bundle", str(e), 400)
        try:
            ckey = crypto.b64d(content_key_b64)
        except ValueError:
            raise CloudError("bad_bundle", "The release key is not valid.")
        if len(ckey) != 32:
            raise CloudError("bad_bundle", "The release key is not valid.")
        try:                                                                                    # the key must really open this bundle
            bundle.decrypt_files(manifest, _payload, ckey)
        except bundle.BundleError as e:
            raise CloudError("bad_bundle", "The release key does not open this release: " + str(e))
        build = manifest["build"]
        if self.db.one("SELECT 1 FROM releases WHERE build=?", (build,)):
            raise CloudError("exists", f"Build {build} already exists. A release can never be changed - publish a new build number.", 409)
        top = self.db.scalar("SELECT MAX(build) FROM releases", default=0)
        if build <= top:
            raise CloudError("old_build", f"The newest build is {top}; a new release must be higher.", 409)
        name = f"{build}.bfr"
        (self.dir / "releases" / name).write_bytes(data)
        rid = self.db.insert("INSERT INTO releases (build, version, channel, notes, state, file_name, sha256, size, key_enc, manifest, created_at, uploaded_by) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (build, manifest["version"], manifest["channel"], clean(notes or manifest.get("notes"), 2000), "draft", name,
                              hashlib.sha256(data).hexdigest(), len(data), crypto.seal(self._seal, ckey, b"release-key/%d" % build),
                              json.dumps(manifest), self.now(), clean(actor, 60)))
        self.event("release_uploaded", actor=actor, detail=f"build {build} ({manifest['version']})")
        return {"id": rid, "build": build, "version": manifest["version"]}

    def list_releases(self) -> list[dict]:
        rows = self.db.all("SELECT id, build, version, channel, notes, mandatory, rollout, state, size, created_at, published_at, uploaded_by FROM releases ORDER BY build DESC")
        return [dict(r) | {"installed": self.db.scalar("SELECT COUNT(*) FROM licences WHERE client_build=?", (r["build"],))} for r in rows]

    def edit_release(self, build: int, *, state: Optional[str] = None, mandatory: Optional[bool] = None, rollout: Optional[int] = None,
                     notes: Optional[str] = None, actor: str = "") -> None:
        r = self.db.one("SELECT * FROM releases WHERE build=?", (build,))
        if r is None:
            raise CloudError("not_found", "No such release.", 404)
        if state is not None:
            if state not in ("draft", "live", "withdrawn"):
                raise CloudError("bad_request", "Unknown release state.")
            self.db.run("UPDATE releases SET state=?, published_at=CASE WHEN ?='live' AND published_at IS NULL THEN ? ELSE published_at END WHERE build=?",
                        (state, state, self.now(), build))
        if mandatory is not None:
            self.db.run("UPDATE releases SET mandatory=? WHERE build=?", (1 if mandatory else 0, build))
        if rollout is not None:
            if isinstance(rollout, bool) or not isinstance(rollout, int) or not 1 <= rollout <= 100:
                raise CloudError("bad_request", "The roll-out must be between 1 and 100 percent.")
            self.db.run("UPDATE releases SET rollout=? WHERE build=?", (rollout, build))
        if notes is not None:
            self.db.run("UPDATE releases SET notes=? WHERE build=?", (clean(notes, 2000), build))
        self.event("release_changed", actor=actor, detail=f"build {build}: " + ", ".join(
            f"{k}={v}" for k, v in (("state", state), ("mandatory", mandatory), ("rollout", rollout)) if v is not None))

    def delete_release(self, build: int, actor: str = "") -> None:
        r = self.db.one("SELECT * FROM releases WHERE build=?", (build,))
        if r is None:
            raise CloudError("not_found", "No such release.", 404)
        if r["state"] == "live":
            raise CloudError("live", "A live release cannot be deleted. Withdraw it first.", 409)
        self.db.run("DELETE FROM releases WHERE build=?", (build,))
        try:
            (self.dir / "releases" / r["file_name"]).unlink()
        except OSError:
            pass
        self.event("release_deleted", actor=actor, detail=f"build {build}", level="warn")

    @staticmethod
    def _bucket(device_id: str, build: int) -> int:
        return int(hashlib.sha256(f"{device_id}/{build}".encode()).hexdigest()[:8], 16) % 100

    def update_for(self, row, client_build: int, channel: str = "stable") -> dict:
        """What this device should install now (its roll-out group decides) - or nothing."""
        rows = self.db.all("SELECT * FROM releases WHERE state='live' AND channel=? ORDER BY build DESC", (clean(channel, 20) or "stable",))
        for r in rows:
            if self._bucket(row["device_id"] or "", r["build"]) < r["rollout"]:
                if r["build"] > client_build:
                    return {"available": True, "build": r["build"], "version": r["version"], "notes": r["notes"],
                            "mandatory": bool(r["mandatory"]) or client_build < self.setting("min_build"), "size": r["size"], "sha256": r["sha256"]}
                break
        return {"available": False, "build": client_build, "mandatory": client_build < self.setting("min_build")}

    def release_file(self, row, build: int) -> tuple[Path, str]:
        """The bundle of a release this device is allowed to install."""
        r = self.db.one("SELECT * FROM releases WHERE build=? AND state='live'", (build,))
        if r is None or self._bucket(row["device_id"] or "", build) >= r["rollout"]:
            raise CloudError("not_found", "That release is not available.", 404)
        return self.dir / "releases" / r["file_name"], r["sha256"]

    def release_key(self, row, build: int, epk: str) -> dict:
        """The key that opens ONE release, wrapped so that only the asking app can read it."""
        r = self.db.one("SELECT * FROM releases WHERE build=? AND state='live'", (build,))
        if r is None or self._bucket(row["device_id"] or "", build) >= r["rollout"]:
            raise CloudError("not_found", "That release is not available.", 404)
        if not isinstance(epk, str) or len(epk) > 60:
            raise CloudError("bad_request", "The request is not valid.")
        try:
            key = crypto.unseal(self._seal, r["key_enc"], b"release-key/%d" % build)
            wrapped = crypto.wrap_for(epk, key, f"release/{build}/{row['device_id']}".encode())
        except ValueError:
            raise CloudError("bad_request", "The request is not valid.")
        self.event("release_key", licence_id=row["id"], detail=f"build {build}")
        return wrapped

    def withdraw_current_if_broken(self) -> None:           # (reserved for a future health-based auto-rollback)
        return None

    # ── the installer: the setup program people download (the link) and the apps update themselves with ─────────────────────
    def add_installer(self, tmp_path: Path, version: str, sig_b64: str, notes: str = "", actor: str = "") -> dict:
        """Register an uploaded installer file (already on disk, hashed by the caller or here). It must carry the developer's signature."""
        try:
            vnum = bundle.version_number(version)
        except ValueError:
            raise CloudError("bad_version", "A version looks like 1.2.3.")
        size = tmp_path.stat().st_size
        if not 1_000_000 <= size <= bundle.MAX_INSTALLER:
            raise CloudError("bad_installer", "That file does not look like the installer (size).")
        h = hashlib.sha256()
        with open(tmp_path, "rb") as f:
            head = f.read(2)
            f.seek(0)
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        if head != b"MZ":
            raise CloudError("bad_installer", "That is not a Windows program.")
        digest = h.hexdigest()
        if not bundle.verify_installer(version, digest, sig_b64, self.release_keys):
            self.event("installer_refused", actor=actor, detail=f"version {version}: signature not valid", level="alert")
            raise CloudError("bad_signature", "The signature is not the developer's: this installer was not accepted.", 400)
        top = self.db.scalar("SELECT MAX(vnum) FROM installers", default=0)
        if vnum <= top:
            raise CloudError("old_version", f"The newest installer is {self.db.scalar('SELECT version FROM installers ORDER BY vnum DESC LIMIT 1')}; a new one must be higher.", 409)
        name = f"BestFare-Setup-{version}.exe"
        (self.dir / "installers").mkdir(exist_ok=True)
        os.replace(tmp_path, self.dir / "installers" / name)
        iid = self.db.insert("INSERT INTO installers (version, vnum, file_name, sha256, size, sig, notes, state, created_at, uploaded_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (version, vnum, name, digest, size, sig_b64, clean(notes, 2000), "draft", self.now(), clean(actor, 60)))
        self.event("installer_uploaded", actor=actor, detail=f"version {version} ({size // 1024 // 1024} MB)")
        return {"id": iid, "version": version, "sha256": digest, "size": size}

    def list_installers(self) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT id, version, vnum, size, sha256, notes, state, mandatory, created_at, published_at, uploaded_by FROM installers ORDER BY vnum DESC")]

    def edit_installer(self, iid: int, *, state: Optional[str] = None, mandatory: Optional[bool] = None, notes: Optional[str] = None, actor: str = "") -> None:
        r = self.db.one("SELECT * FROM installers WHERE id=?", (iid,))
        if r is None:
            raise CloudError("not_found", "No such installer.", 404)
        if state is not None:
            if state not in ("draft", "live", "withdrawn"):
                raise CloudError("bad_request", "Unknown installer state.")
            self.db.run("UPDATE installers SET state=?, published_at=CASE WHEN ?='live' AND published_at IS NULL THEN ? ELSE published_at END WHERE id=?", (state, state, self.now(), iid))
        if mandatory is not None:
            self.db.run("UPDATE installers SET mandatory=? WHERE id=?", (1 if mandatory else 0, iid))
        if notes is not None:
            self.db.run("UPDATE installers SET notes=? WHERE id=?", (clean(notes, 2000), iid))
        self.event("installer_changed", actor=actor, detail=f"{r['version']}: " + ", ".join(f"{k}={v}" for k, v in (("state", state), ("mandatory", mandatory)) if v is not None))

    def delete_installer(self, iid: int, actor: str = "") -> None:
        r = self.db.one("SELECT * FROM installers WHERE id=?", (iid,))
        if r is None:
            raise CloudError("not_found", "No such installer.", 404)
        if r["state"] == "live":
            raise CloudError("live", "A live installer cannot be deleted. Withdraw it first.", 409)
        self.db.run("DELETE FROM installers WHERE id=?", (iid,))
        try:
            (self.dir / "installers" / r["file_name"]).unlink()
        except OSError:
            pass
        self.event("installer_deleted", actor=actor, detail=r["version"], level="warn")

    def live_installer(self):
        return self.db.one("SELECT * FROM installers WHERE state='live' ORDER BY vnum DESC LIMIT 1")

    def installer_path(self, row) -> Path:
        return self.dir / "installers" / row["file_name"]

    def installer_for(self, client_shell: int) -> dict:
        """What the app should do about ITS OWN program (the installed shell): nothing, or fetch the live installer and run it."""
        r = self.live_installer()
        if r is None or not isinstance(client_shell, int) or isinstance(client_shell, bool) or r["vnum"] <= client_shell:
            return {"available": False}
        return {"available": True, "version": r["version"], "vnum": r["vnum"], "size": r["size"], "sha256": r["sha256"], "sig": r["sig"], "notes": r["notes"],
                "mandatory": bool(r["mandatory"]), "path": "/download/BestFare-Setup.exe"}

    # ── admins ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def create_admin(self, username: str, password: str, role: str = "admin", actor: str = "", must_change: bool = False) -> int:
        username = clean(username, 40)
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{3,40}", username):
            raise CloudError("bad_username", "A username has 3 to 40 letters, digits, dots, dashes or underscores.")
        if role not in ("owner", "admin"):
            raise CloudError("bad_request", "Unknown role.")
        problem = crypto.password_problem(password)
        if problem:
            raise CloudError("weak_password", problem)
        if self.db.one("SELECT 1 FROM admins WHERE username=?", (username,)):
            raise CloudError("exists", "That username is taken.", 409)
        aid = self.db.insert("INSERT INTO admins (username, pw_hash, role, created_at, must_change) VALUES (?,?,?,?,?)",
                             (username, crypto.password_hash(password), role, self.now(), 1 if must_change else 0))
        self.event("admin_created", actor=actor, detail=f"{username} ({role})")
        return aid

    def totp_secret(self, row) -> str:
        v = row["totp_secret"] if row is not None else None
        if not v:
            return ""
        try:
            return crypto.unseal(self._seal, v, b"totp").decode()
        except Exception:  # noqa: BLE001
            return ""

    def totp_start(self, aid: int) -> str:
        """A new secret for the authenticator app (sealed in the database). Two-step login stays off until a code from it is confirmed."""
        secret = crypto.totp_new_secret()
        self.db.run("UPDATE admins SET totp_secret=?, totp_on=0, totp_last=0 WHERE id=?", (crypto.seal(self._seal, secret.encode(), b"totp"), aid))
        return secret

    def totp_verify(self, aid: int, code: Any, consume: bool = True) -> bool:
        """Is this code right - and (when `consume`) not used before? A code seen by somebody else is worthless a moment later."""
        row = self.db.one("SELECT totp_secret, totp_last FROM admins WHERE id=?", (aid,))
        secret = self.totp_secret(row)
        if not secret:
            return False
        step = crypto.totp_match(secret, code if isinstance(code, str) else str(code or ""), self.now())
        if step is None or step <= int(row["totp_last"] or 0):
            return False
        if not consume:
            return True
        return self.db.run("UPDATE admins SET totp_last=? WHERE id=? AND totp_last<?", (step, aid, step)) == 1     # (two logins with one code: only one passes)

    def list_admins(self) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT id, username, role, totp_on, disabled, created_at, last_login FROM admins ORDER BY id")]

    def set_admin(self, aid: int, *, disabled: Optional[bool] = None, password: Optional[str] = None, role: Optional[str] = None, actor: str = "") -> None:
        r = self.db.one("SELECT * FROM admins WHERE id=?", (aid,))
        if r is None:
            raise CloudError("not_found", "No such admin.", 404)
        owners = self.db.scalar("SELECT COUNT(*) FROM admins WHERE role='owner' AND disabled=0")
        if (disabled and r["role"] == "owner" and owners <= 1) or (role == "admin" and r["role"] == "owner" and owners <= 1):
            raise CloudError("last_owner", "There must always be at least one active owner.", 409)
        if password is not None:
            problem = crypto.password_problem(password)
            if problem:
                raise CloudError("weak_password", problem)
            self.db.run("UPDATE admins SET pw_hash=?, must_change=0 WHERE id=?", (crypto.password_hash(password), aid))
            self.db.run("DELETE FROM admin_sessions WHERE admin_id=?", (aid,))
        if disabled is not None:
            self.db.run("UPDATE admins SET disabled=? WHERE id=?", (1 if disabled else 0, aid))
            if disabled:
                self.db.run("DELETE FROM admin_sessions WHERE admin_id=?", (aid,))
        if role is not None:
            if role not in ("owner", "admin"):
                raise CloudError("bad_request", "Unknown role.")
            self.db.run("UPDATE admins SET role=? WHERE id=?", (role, aid))
        self.event("admin_changed", actor=actor, detail=f"{r['username']}")
