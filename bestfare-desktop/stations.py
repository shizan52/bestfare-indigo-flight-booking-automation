"""The airport list behind the From / To boxes: IndiGo's own station data (stations.json, rebuilt by tools/build_stations.py)
plus the friendly old names people still use (Vizag, Poona, Bombay ...). Pure Python: no network, no Qt, no browser."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import paths
import resources

paths.use_engine()

FILE = "stations.json"          # (read through resources.py)
# the airports most people fly between: shown first when the box is empty and when several airports match
POPULAR = ("DEL", "BOM", "BLR", "CCU", "MAA", "HYD", "AMD", "PNQ", "GOI", "JAI", "LKO", "COK", "PAT", "GAU", "SXR", "IXC")


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


@dataclass(frozen=True)
class Station:
    code: str
    city: str
    airport: str
    state: str
    names: tuple[str, ...]            # every normalised name this airport answers to: the city, other names, old names

    @property
    def label(self) -> str:
        return f"{self.city} ({self.code})"


@lru_cache(maxsize=1)
def all_stations() -> tuple[Station, ...]:
    rows: list[dict] = []
    try:
        rows = json.loads(resources.read_text(FILE)).get("stations", [])
    except (OSError, ValueError):
        pass
    extra: dict[str, set[str]] = {}
    try:                                                  # the bot's table of old names: Vizag, Poona, Calicut ...
        import airports as bot
        for code, names in bot._NAMES.items():           # noqa: SLF001
            extra[code] = set(names)
        if not rows:                                      # no station file: still usable, city names only
            rows = [{"code": c, "city": bot.CITY[c], "airport": "", "state": "", "alt": []} for c in bot.CITY]
    except ImportError:
        pass
    out = []
    for r in rows:
        names = {norm(r["city"]), norm(r.get("short", ""))} | {norm(a) for a in r.get("alt", [])} | extra.get(r["code"], set())
        out.append(Station(r["code"], r["city"], r.get("airport", ""), r.get("state", ""), tuple(sorted(n for n in names if n))))
    return tuple(sorted(out, key=lambda s: s.city))


@lru_cache(maxsize=1)
def _by_code() -> dict[str, Station]:
    return {s.code: s for s in all_stations()}


def get(code: Optional[str]) -> Optional[Station]:
    return _by_code().get((code or "").upper())


def known(code: Optional[str]) -> bool:
    return get(code) is not None


def label(code: str) -> str:
    s = get(code)
    return s.label if s else code


def city(code: str) -> str:
    s = get(code)
    return s.city if s else code


def _popularity(s: Station) -> int:
    return POPULAR.index(s.code) if s.code in POPULAR else len(POPULAR)


def _rank(s: Station, t: str) -> Optional[int]:
    """How well does the typed text fit this airport? Lower is better; None = not at all."""
    if t == s.code.lower():
        return 0
    if t in s.names:
        return 1
    if any(n.startswith(t) for n in s.names):
        return 2
    words = [w for n in s.names for w in n.split()] + norm(s.airport).split()
    if any(w.startswith(t) for w in words):
        return 3
    if len(t) >= 3 and (t in norm(s.airport) or any(t in n for n in s.names)):
        return 4
    return None


def search(text: str, limit: int = 8) -> list[Station]:
    """The airports that fit what the person typed, best first. An empty box lists the popular ones."""
    t = norm(text)
    if not t:
        return sorted(all_stations(), key=lambda s: (_popularity(s), s.city))[:limit]
    scored = []
    for s in all_stations():
        r = _rank(s, t)
        if r is not None:
            scored.append((r, _popularity(s), s.city, s))
    scored.sort(key=lambda x: x[:3])
    return [x[3] for x in scored[:limit]]


def resolve(text: str) -> Optional[str]:
    """'Delhi (DEL)', 'del', 'bangalore' -> the code; None unless the text names exactly one airport."""
    m = re.search(r"\(([A-Za-z]{3})\)\s*$", (text or "").strip())
    if m and known(m.group(1)):
        return m.group(1).upper()
    t = norm(text)
    if not t:
        return None
    exact = [s for s in all_stations() if t == s.code.lower() or t in s.names]
    if len(exact) == 1:
        return exact[0].code
    return None
