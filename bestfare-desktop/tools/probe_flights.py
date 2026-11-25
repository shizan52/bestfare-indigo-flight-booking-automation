"""LIVE, no booking: run the engine's own steps up to IndiGo's flight search (token + search, no fare hold, no PNR) and keep IndiGo's
answer - the list of flights the traveller will be asked to choose from - in a file, plus a readable summary of what the choice list
will show. For looking at real data, e.g. when IndiGo changes its answer.

    .venv\\Scripts\\python.exe tools\\probe_flights.py OUT_DIR [--origin DEL --dest BOM --days 10 --offer offer1]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import paths  # noqa: E402

sys.path.insert(0, str(HERE / "tools"))
import devcodes  # noqa: E402

devcodes.load()                                   # (the development gate: the offer codes come from the bot's config)
paths.use_engine()
import browser as bmod  # noqa: E402
import forms  # noqa: E402
import gate as gatemod  # noqa: E402
import indigo_client as ic  # noqa: E402
from cdp import CDPBrowser  # noqa: E402
from extension import ExtensionSession  # noqa: E402
from indigo_client import BookingRequest, IndigoBlocked, IndigoError, Passenger, open_session, run_booking  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--origin", default="DEL")
    ap.add_argument("--dest", default="BOM")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--offer", default="offer1", choices=("offer1", "offer2"))
    ap.add_argument("--pax", type=int, default=1, help="how many travellers to search for (to see how IndiGo prices a group)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    def say(s: str) -> None:
        print(f"[{time.monotonic() - t0:6.1f}s] {s}", flush=True)

    kept: dict = {}
    real_cheapest = ic.cheapest

    def spy(search_json, *args, **kw):                                  # (run_booking hands IndiGo's answer to this)
        kept["answer"] = search_json
        return real_cheapest(search_json, *args, **kw)

    ic.cheapest = spy
    edge = bmod.LocalEdge()
    ep = await edge.ensure(note=lambda s: say("browser: " + s), quiet=True)
    day = (forms.today_ist() + timedelta(days=a.days)).strftime("%d-%m-%Y")
    people = [Passenger("RAHUL", "TESTER", "M"), Passenger("PRIYA", "TESTER", "F"), Passenger("ANIL", "TESTER", "M")][:max(1, a.pax)]
    req = BookingRequest(origin=a.origin, destination=a.dest, date=day, passengers=people,
                         offer=a.offer, contact_email="test.booking@example.com", contact_phone="9000000001")
    ok = False
    try:
        async with CDPBrowser(ep) as b:
            await bmod.tidy(b)
            tab, sess = await open_session(b, [], session_cls=ExtensionSession)
            try:
                res = await run_booking(sess, req, gatemod.DevGate().codes[a.offer], "search")
                say(f"engine search: success={res.success} fare={res.fare_amount} error={res.error}")
                ok = res.success
            except (IndigoBlocked, IndigoError) as e:
                say(f"engine search refused: {e}")
            finally:
                await tab.close()
    finally:
        ic.cheapest = real_cheapest
        await edge.close()
    if kept.get("answer"):
        (out / "search_answer.json").write_text(json.dumps(kept["answer"], indent=1, ensure_ascii=False), encoding="utf-8")
        say(f"IndiGo's answer saved: {out / 'search_answer.json'}")
    say("RESULT: " + ("PASS" if ok else "BLOCKED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
