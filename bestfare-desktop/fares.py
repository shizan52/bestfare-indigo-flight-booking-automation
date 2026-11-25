"""The lowest fare of every day, for the date calendar - the same call IndiGo's own date picker makes.

POST api-prod-booking-skyplus6e.goindigo.in/v1/getfarecalendar  {startDate, endDate, origin, destination, currencyCode, promoCode,
lowestIn} -> {data: {lowFares: [{date, price, category L|M|H}]}}. Days without flights are simply missing; a route without any
flight answers errors.code "Data is not Available". Measured live: the offer code does NOT change these fares (offers apply when
the booking is searched), so the calendar shows the regular lowest fare of each day. Up to 60 days per call, like the website.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import paths

paths.use_engine()
from indigo_client import _fetch_js  # noqa: E402

URL = "https://api-prod-booking-skyplus6e.goindigo.in/v1/getfarecalendar"
USER_KEY = "15faf8ddf1e8354e90e54fa098e8b1a8"
WINDOW_DAYS = 60

CATEGORY_WORDS = {"L": "low", "M": "medium", "H": "high"}


@dataclass
class FareResult:
    fares: dict[str, tuple[float, str]] = field(default_factory=dict)   # 'YYYY-MM-DD' -> (price, category)
    empty: bool = False                                                  # IndiGo has no flights on this route
    error: Optional[str] = None                                          # why no fares could be had (plain words)


def request_body(origin: str, dest: str, start: date, days: int = WINDOW_DAYS) -> dict:
    end = start + timedelta(days=max(1, min(days, WINDOW_DAYS)) - 1)
    return {"startDate": start.isoformat(), "endDate": end.isoformat(), "origin": origin, "destination": dest,
            "currencyCode": "INR", "promoCode": "", "lowestIn": "M"}


def parse(answer: dict) -> FareResult:
    data = (answer or {}).get("data") or {}
    rows = data.get("lowFares") or []
    errs = (answer or {}).get("errors")
    fares: dict[str, tuple[float, str]] = {}
    for r in rows:
        try:
            price = float(r["price"])
            if price <= 0:                      # no seat to sell that day (found live: today, late in the evening, comes back as 0)
                continue
            fares[str(r["date"])[:10]] = (price, str(r.get("category") or "M").upper())
        except (KeyError, TypeError, ValueError):
            continue
    if not fares and errs:
        return FareResult(empty=True)
    return FareResult(fares=fares, empty=not fares)


async def fetch(tab, origin: str, dest: str, start: date, days: int = WINDOW_DAYS) -> FareResult:
    """One call from the booking page (its session cookies are what IndiGo expects). Never raises."""
    try:
        res = await tab.evaluate_json(_fetch_js(URL, "POST", USER_KEY, request_body(origin, dest, start, days)), timeout=60)
    except Exception:  # noqa: BLE001
        return FareResult(error="unavailable")
    if res.get("fetchError") or res.get("status") in (401, 403, 429):
        return FareResult(error="busy")
    if res.get("status") != 200:
        return FareResult(error="unavailable")
    return parse(res.get("json") or {})


def cheapest(fares: dict[str, tuple[float, str]], first: Optional[date] = None, last: Optional[date] = None) -> Optional[tuple[str, float]]:
    best = None
    for day, (price, _cat) in fares.items():
        d = date.fromisoformat(day)
        if (first and d < first) or (last and d > last):
            continue
        if best is None or (price, day) < (best[1], best[0]):
            best = (day, price)
    return best
