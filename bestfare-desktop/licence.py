"""The licence of this installation: who the user is, how many credits are left, whether access is allowed - as the SERVER says.

Nothing about credits or access is stored in a way the user could edit: the only things kept on disk are the sealed device key,
the sealed licence key and the sealed session token. The credits shown on screen are what the server last said; the server decides
again at every booking.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from cloud import CloudClient, CloudError, Offline
from device import Vault, device_info

NEW, OK, BLOCKED, EXPIRED, OFFLINE = "new", "ok", "blocked", "expired", "offline"
REFRESH_EVERY_S = 300


@dataclass
class Account:
    label: str = ""
    credits: int = 0
    status: str = "active"
    key_tail: str = ""
    expires_at: Optional[float] = None
    broadcast: str = ""
    offers: dict = field(default_factory=dict)
    maintenance: bool = False
    maintenance_msg: str = ""
    min_build: int = 0
    infant_credits: int = 0                  # what an infant costs: nothing (credits are for adults and children; the admin can still change it, 0 is the default)

    def offer_keys(self) -> list[str]:
        """The offers this key may use, in order (the server sends only those, and only as keys: what an offer is stays with the admin)."""
        return sorted(self.offers)

    def offer_on(self, key: str) -> bool:
        return bool((self.offers.get(key) or {}).get("enabled", True))

    def cost(self, travellers: int, infants: int = 0) -> int:
        """Credits for a booking: every adult and child, plus the infants at the admin's price."""
        return travellers + infants * self.infant_credits

    @classmethod
    def from_server(cls, d: dict) -> "Account":
        return cls(label=str(d.get("label", "")), credits=int(d.get("credits", 0)), status=str(d.get("status", "active")), key_tail=str(d.get("key_tail", "")),
                   expires_at=d.get("expires_at"), broadcast=str(d.get("broadcast", "")), offers=dict(d.get("offers") or {}),
                   maintenance=bool(d.get("maintenance")), maintenance_msg=str(d.get("maintenance_msg", "")), min_build=int(d.get("min_build", 0)),
                   infant_credits=int(d.get("infant_credits", 0)))


@dataclass
class Ticket:
    """What the server hands out for ONE booking, after it has taken the credits. It lives in memory only."""
    booking_id: str
    promo: str = field(repr=False)                  # (never printed: a log line or a crash report that shows this object must not show the offer code)
    charged: int
    credits: int


class Licence:
    def __init__(self, client: CloudClient, vault: Vault, app_version: str = ""):
        self.client, self.vault, self.app_version = client, vault, app_version
        self.state = NEW
        self.account = Account()
        self.message = ""
        self.update: dict = {}
        self.installer: dict = {}                # the newer setup program the server offers for the installed app itself (empty = none)
        self.open: list[dict] = []
        self._lock = threading.RLock()
        self._listeners: list[Callable[["Licence"], None]] = []
        self.last_ok = 0.0
        self.token_loaded = False

    def on_change(self, fn: Callable[["Licence"], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in list(self._listeners):
            try:
                fn(self)
            except Exception:  # noqa: BLE001 - a listener must never break the licence
                pass

    # ── state from the server's answers ────────────────────────────────────────────────────────────────────────────
    def _adopt(self, d: dict) -> None:
        with self._lock:
            self.account = Account.from_server(d["account"])
            if "update" in d:
                self.update = d["update"] or {}
            if "installer" in d:
                self.installer = d["installer"] or {}
            if "open" in d:
                self.open = d["open"] or []
            self.state, self.message, self.last_ok = OK, "", time.time()
            self.vault.update(token=self.client.token)
        self._changed()

    def _fail(self, e: CloudError) -> str:
        with self._lock:
            if e.code in ("blocked",):
                self.state, self.message = BLOCKED, e.message
            elif e.code == "expired":
                self.state, self.message = EXPIRED, e.message
            elif e.code in ("device_mismatch", "mac_lock"):
                self.state, self.message = BLOCKED, e.message
            elif isinstance(e, Offline):
                self.state, self.message = OFFLINE, e.message
            elif e.code in ("bad_key", "unauthorized", "rebind_needed"):
                self.state, self.message = NEW, e.message
            else:
                self.state, self.message = OFFLINE, e.message
        self._changed()
        return self.state

    # ── starting up ────────────────────────────────────────────────────────────────────────────────────────────────
    def resume(self) -> str:
        """At every start: is the saved sign-in still good? Returns the state (NEW = the key must be typed in)."""
        d = self.vault.load()
        if not d.get("licence_key"):
            self.state = NEW
            return self.state
        self.client.token = d.get("token", "")
        try:
            self.client.hello()                                                      # (is this the real server? what time is it there?)
            if self.client.token:
                try:
                    self._adopt(self.client.session())
                    return self.state
                except CloudError as e:
                    if e.code not in ("unauthorized",):
                        return self._fail(e)
            # the session ended (the key was moved, the token expired): sign in again with the saved key - the server decides
            self._adopt(self.client.activate(d["licence_key"], device_info(self.app_version), auto=True))
            return self.state
        except CloudError as e:
            if e.code == "bad_key":
                self.vault.update(licence_key=None, token=None)
            return self._fail(e)

    def activate(self, key_text: str) -> None:
        """The user typed their key. Raises CloudError with a message fit to show."""
        key_text = (key_text or "").strip()
        if not key_text:
            raise CloudError("bad_key", "Please type your key.")
        self.client.hello()
        d = self.client.activate(key_text, device_info(self.app_version))
        self.vault.update(licence_key=key_text)
        self._adopt(d)

    def sign_out(self) -> None:
        self.vault.update(licence_key=None, token=None)
        self.client.token = ""
        self.state, self.account, self.message = NEW, Account(), ""
        self._changed()

    def refresh(self) -> str:
        try:
            self._adopt(self.client.session())
        except CloudError as e:
            if e.code == "unauthorized":
                return self.resume()
            return self._fail(e)
        return self.state

    # ── the booking gate ───────────────────────────────────────────────────────────────────────────────────────────
    def begin(self, n: int, offer: str, route: str, date: str, ref: str, infants: int = 0) -> Ticket:
        """Ask the server for permission to book `n` travellers (+ `infants` on their laps). It takes the credits first and only then gives the promo code."""
        try:
            d = self.client.begin(n, offer, route, date, ref, infants)
        except CloudError as e:
            if e.code == "unauthorized":                                              # the session ended meanwhile: sign in again once
                if self.resume() == OK:
                    d = self.client.begin(n, offer, route, date, ref, infants)
                else:
                    raise
            else:
                if e.code in ("blocked", "expired", "no_credits"):
                    self.refresh()
                raise
        with self._lock:
            self.account.credits = int(d.get("credits", self.account.credits))
        self._changed()
        return Ticket(d["booking_id"], d["promo"], int(d.get("charged", n)), int(d.get("credits", 0)))

    def finish(self, booking_id: str, outcome: str, pnr: Optional[str] = None, reason: str = "") -> dict:
        d = self.client.finish(booking_id, outcome, pnr, reason)
        with self._lock:
            self.account.credits = int(d.get("credits", self.account.credits))
        self._changed()
        return d

    @property
    def usable(self) -> bool:
        return self.state == OK and self.account.status == "active"

    def can_afford(self, n: int, infants: int = 0) -> bool:
        return self.usable and self.account.credits >= self.account.cost(n, infants)
