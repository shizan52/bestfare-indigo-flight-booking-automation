"""Rebuild stations.json - the airport list behind the From / To boxes - from IndiGo's own station search.

    .venv\\Scripts\\python.exe tools\\build_stations.py

It asks IndiGo's station search (the one its website uses for the From/To boxes) for every letter, and the two-letter
prefixes of any letter that returns a full page, and keeps the domestic airports (country IN) that are open for booking.
About 30-60 calls, paced like a person. Run it when IndiGo opens or closes an airport; the app itself never needs the
network for this list.
"""
from __future__ import annotations

import asyncio
import json
import random
import string
import sys
import time
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import paths  # noqa: E402

paths.use_engine()
import browser as bmod  # noqa: E402
from cdp import CDPBrowser  # noqa: E402
from indigo_client import _fetch_js, open_session  # noqa: E402

URL = "https://api-prod-skyplus.goindigo.in/bookingwidgetsearchengine/search"
UK = "b888043bdc86e1cda6ad61ffcb251157"
LIMIT = 50
OUT = HERE / "stations.json"


def keep(s: dict) -> bool:
    return bool(s.get("stationCode")) and s.get("countryCode") == "IN" and not s.get("isInternational") and s.get("allowed", True)


def row(s: dict) -> dict:
    alt = [a.strip() for a in str(s.get("alternateCityName") or "").replace("/", ",").split(",") if a.strip()]
    return {"code": s["stationCode"], "city": s.get("fullName") or s.get("shortName") or s["stationCode"],
            "short": s.get("shortName") or s.get("fullName") or s["stationCode"], "airport": s.get("airportName") or "",
            "state": s.get("provinceStateCode") or "", "alt": alt}


async def main() -> int:
    edge = bmod.LocalEdge()
    ep = await edge.ensure()
    found: dict[str, dict] = {}
    calls = 0
    async with CDPBrowser(ep) as b:
        tab, sess = await open_session(b, [])
        try:
            await sess.human.wander(random.uniform(8, 11))

            async def ask(prefix: str) -> list[dict]:
                nonlocal calls
                await asyncio.sleep(2.6 + random.uniform(0, 1.2))
                res = await tab.evaluate_json(_fetch_js(f"{URL}?prefix={quote(prefix)}&limit={LIMIT}", "GET", UK, None), timeout=60)
                calls += 1
                if res.get("fetchError") or res.get("status") != 200:
                    raise RuntimeError(f"station search failed for {prefix!r}: {res.get('fetchError') or res.get('status')}")
                data = (res.get("json") or {}).get("data") or {}
                out = list(data.get("suggestions") or [])
                for s in list(out):
                    out += list(s.get("nearest_airports") or [])
                return out

            todo = list(string.ascii_lowercase)
            while todo:
                prefix = todo.pop(0)
                hits = await ask(prefix)
                for s in hits:
                    if keep(s):
                        found[s["stationCode"]] = row(s)
                top = [s for s in hits if s.get("stationCode")]
                if len(top) >= LIMIT and len(prefix) < 2:                   # a full page: there may be more behind it
                    todo += [prefix + c for c in string.ascii_lowercase]
                print(f"{prefix:<3} {len(hits):>3} results, {len(found)} domestic airports so far", flush=True)
        finally:
            await tab.close()
    await edge.close()
    rows = sorted(found.values(), key=lambda r: r["city"])
    OUT.write_text(json.dumps({"built": time.strftime("%Y-%m-%d"), "source": "IndiGo station search", "stations": rows}, ensure_ascii=False,
                              indent=1), encoding="utf-8")
    print(f"wrote {OUT} with {len(rows)} airports after {calls} calls")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
