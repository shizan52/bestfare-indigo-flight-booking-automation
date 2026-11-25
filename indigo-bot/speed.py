"""How fast the internet and this computer really are - measured while the work goes on, never guessed.

Every wait that depends on the network (a page that has to load, an answer from IndiGo, the payment gateway) is multiplied by
`Net.scale()`: on a fast connection it is 1.0, so nothing is made to wait longer than needed; on a slow connection or a slow
computer it grows (at most 3x) so the work is waited for patiently instead of being given up on - and nothing is ever cut short
into a half-finished state. Pure Python, no dependencies; shared by the Telegram bot and the desktop app.
"""
from __future__ import annotations

import time

# what each IndiGo call takes when the connection is good (seconds, measured live 2026-11-18)
BASELINE = {"token": 0.5, "search": 4.0, "sell": 2.5, "addpax": 1.5, "itinerary": 1.5}
MAX_SCALE = 3.0
SLOW_AT = 1.8                                # from this scale on, the traveller is told the connection looks slow


class Net:
    ratio: float | None = None               # smoothed (seconds taken / seconds normally taken) over the recent calls
    samples = 0
    last_at = 0.0

    @classmethod
    def record(cls, name: str, seconds: float) -> None:
        """One IndiGo call took `seconds`. Calls we know no baseline for are ignored."""
        base = BASELINE.get(name)
        if not base or seconds <= 0:
            return
        r = seconds / base
        cls.ratio = r if cls.ratio is None else 0.65 * cls.ratio + 0.35 * r
        cls.samples += 1
        cls.last_at = time.monotonic()

    @classmethod
    def scale(cls) -> float:
        """1.0 .. MAX_SCALE: how much longer than usual a wait that depends on the network should be allowed to take."""
        if cls.ratio is None:
            return 1.0
        return max(1.0, min(MAX_SCALE, cls.ratio))

    @classmethod
    def slow(cls) -> bool:
        return cls.samples >= 2 and cls.scale() >= SLOW_AT

    @classmethod
    def reset(cls) -> None:
        cls.ratio, cls.samples, cls.last_at = None, 0, 0.0


def scaled(seconds: float) -> float:
    """`seconds` stretched for a slow connection (never shortened)."""
    return seconds * Net.scale()
