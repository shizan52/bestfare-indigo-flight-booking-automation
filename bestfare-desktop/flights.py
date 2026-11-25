"""The flights a traveller chooses from, told in plain words: "15:30 → 18:00", "2h 30m", "Non-stop", "6E 864".

Pure functions over the entries of `indigo_client.flight_options()` (nothing here knows how the list was found). Used by the
choice panel, the booking notes, the history and the practice mode.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import forms

SORTS = (("price", "Lowest fare"), ("time", "Earliest"))             # (the order of the two buttons; the first is the default)


def _dt(iso: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime((iso or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def clock(iso: Optional[str]) -> str:
    """'2026-11-30T15:30:00' -> '15:30'"""
    d = _dt(iso)
    return d.strftime("%H:%M") if d else "—"


def days_later(o: dict) -> int:
    """0 = lands the same day, 1 = the next day ..."""
    a, b = _dt(o.get("dep")), _dt(o.get("arr"))
    return max(0, (b.date() - a.date()).days) if a and b else 0


def timeline(o: dict) -> str:
    """'15:30 → 18:00' (with '+1' when it lands the next day)"""
    plus = days_later(o)
    return f"{clock(o.get('dep'))} → {clock(o.get('arr'))}" + (f" +{plus}" if plus else "")


def duration(minutes: Optional[int]) -> str:
    if not isinstance(minutes, int) or minutes <= 0:
        return ""
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h and m else (f"{h}h" if h else f"{m}m")


def stops_text(o: dict) -> str:
    n = o.get("stops") or 0
    if n == 0:
        return "Non-stop"
    via = " · ".join(o.get("via") or [])
    return f"{n} stop" + ("s" if n > 1 else "") + (f" · {via}" if via else "")


def flight_number(o: dict) -> str:
    return " + ".join(o.get("flights") or [])


def money(o: dict) -> str:
    return forms.money(o["amount"])


def saving(o: dict) -> float:
    """What the offer saves on this flight (0 when its lowest fare does not carry the offer)."""
    orig = o.get("original")
    return float(orig - o["amount"]) if o.get("offer") and isinstance(orig, (int, float)) and orig > o["amount"] else 0.0


def line(o: dict) -> str:
    """The one-line description kept with the booking: '6E 864 · 15:30 → 18:00'"""
    return " · ".join(x for x in (flight_number(o), timeline(o)) if x)


def lowest_key(options: list[dict]) -> Optional[str]:
    """The key of the lowest-fare flight (the earlier one when two cost the same)."""
    if not options:
        return None
    return min(options, key=lambda o: (o["amount"], o.get("dep") or ""))["key"]


def arranged(options: list[dict], sort: str = "price", nonstop_only: bool = False) -> list[dict]:
    out = [o for o in options if not nonstop_only or (o.get("stops") or 0) == 0]
    if sort == "price":
        return sorted(out, key=lambda o: (o["amount"], o.get("dep") or ""))
    return sorted(out, key=lambda o: (o.get("dep") or "", o["amount"]))


def public(o: dict) -> dict:
    """What travels to the window: everything it shows, nothing that is only for the booking itself."""
    return {k: v for k, v in o.items() if k not in ("fare_key",)}
