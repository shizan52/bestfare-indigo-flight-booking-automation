"""The booking gate: nothing is booked unless the server allows it.

Before a booking starts the engine asks the gate for a PASS. With a real licence the server takes the credits first (one per
traveller) and only then hands over the offer's promo code - the code never exists anywhere on this computer before that, and it
disappears with the booking. When the booking ends the gate reports the result; a failed booking gets its credits back (within the
server's limits). If the report cannot be made right away it is kept and made at the next start.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from cloud import CloudError, Offline

log = logging.getLogger("gate")


class GateRefused(Exception):
    """The booking may not start. `message` is written for the traveller and shown as it is."""

    def __init__(self, message: str, code: str = "refused"):
        super().__init__(message)
        self.message, self.code = message, code


@dataclass
class Pass:
    promo: str = field(repr=False)                  # (never printed: see licence.Ticket)
    cloud_id: str = ""
    charged: int = 0


def _mix(rec: dict) -> tuple[int, int, int]:
    """(adults, children, infants) of a saved booking (older ones: all adults)."""
    kinds = [(t.get("kind") or "adult") for t in rec.get("travellers") or []]
    if not kinds:
        return int(rec.get("pax_count") or 1), 0, 0
    return sum(k not in ("child", "infant") for k in kinds), kinds.count("child"), kinds.count("infant")


def friendly(e: CloudError) -> str:
    """What a traveller reads when the server says no. Plain words; nothing about how it works."""
    c = e.code
    if c == "no_credits":
        have = e.extra.get("have")
        need = e.extra.get("needed")
        if isinstance(have, int) and isinstance(need, int):
            return (f"You need {need} credit{'s' if need != 1 else ''} for this booking but have {have}. "
                    "Please contact your admin to get more credits.")
        return "You do not have enough credits. Please contact your admin to get more."
    if c in ("blocked", "expired", "maintenance", "update_required", "offer_off", "device_mismatch", "mac_lock", "ref_used", "rebind_needed"):
        return e.message
    if c == "busy":
        return "You already have bookings in progress. Please wait until they finish."
    if isinstance(e, Offline) or c == "offline":
        return "Cannot reach the Best Fare server. Please check the internet connection and try again."
    if c == "rate_limited":
        return "Too many requests. Please wait a moment and try again."
    if c == "untrusted":
        return "Best Fare could not verify its server. Please check the internet connection or update the app."
    if c in ("unauthorized",):
        return "Please sign in again."
    return "Best Fare could not start this booking. Please try again in a moment."


class LicenceGate:
    """The real gate: the server decides."""

    def __init__(self, licence):
        self.licence = licence

    def begin(self, rec: dict) -> Pass:
        adults, children, infants = _mix(rec)
        n = adults + children                                           # the people with a seat; the infants are counted next to them
        try:
            t = self.licence.begin(n, rec["offer"], f"{rec['origin']}-{rec['dest']}", rec["travel_date"], rec["id"], infants)
        except CloudError as e:
            raise GateRefused(friendly(e), e.code) from e
        return Pass(t.promo, t.booking_id, t.charged)

    def finish(self, cloud_id: str, outcome: str, pnr: Optional[str], reason: str) -> dict:
        return self.licence.finish(cloud_id, outcome, pnr, reason)


class DevGate:
    """DEVELOPMENT ONLY (the workspace's tools and tests): promo codes from the environment, no server, no credits. A distribution
    built for users does not contain this file, and without BESTFARE_DEV_OFFERS it refuses everything anyway."""

    def __init__(self):
        raw = os.environ.get("BESTFARE_DEV_OFFERS", "")
        try:
            self.codes = {k: str(v) for k, v in json.loads(raw).items()} if raw else {}
        except (ValueError, AttributeError):
            self.codes = {}

    def begin(self, rec: dict) -> Pass:
        code = self.codes.get(rec["offer"])
        if not code:
            raise GateRefused("This copy of Best Fare is not signed in. Please sign in with your key.", "no_licence")
        return Pass(code)

    def finish(self, cloud_id: str, outcome: str, pnr: Optional[str], reason: str) -> dict:
        return {}
