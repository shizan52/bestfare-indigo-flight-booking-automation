"""Decisive LIVE check on this computer, with no booking: start the app's Edge, then run the booking engine's own steps up to the search (token + search, no fare hold, no PNR).

    .venv\\Scripts\\python.exe tools\\probe_search.py OUT_DIR [--force-intro] [--days 10]

--force-intro  forget the introduction first and do it again (the Edge window shows the typing and the search).
Prints one line per step and ends with RESULT: PASS / BLOCKED.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
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
from cdp import CDPBrowser  # noqa: E402
from indigo_client import BookingRequest, IndigoBlocked, IndigoError, Passenger, open_session, run_booking  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("aiohttp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    t0 = time.monotonic()

    def say(s: str) -> None:
        print(f"[{time.monotonic() - t0:6.1f}s] {s}", flush=True)

    edge = bmod.LocalEdge()
    ep = await edge.ensure(note=lambda s: say("browser: " + s))
    say(f"browser ready at {ep}")
    day = (forms.today_ist() + timedelta(days=a.days)).strftime("%d-%m-%Y")
    req = BookingRequest(origin="DEL", destination="BOM", date=day, passengers=[Passenger("RAHUL", "TESTER", "M")],
                         offer="offer1", contact_email="test.booking@example.com", contact_phone="9000000001")
    steps: list = []
    async with CDPBrowser(ep) as b:
        await bmod.tidy(b)
        tab, sess = await open_session(b, steps)
        try:
            res = await run_booking(sess, req, __import__("gate").DevGate().codes["offer1"], "search")
            say(f"engine search: success={res.success} fare={res.fare_amount} error={res.error}")
            ok = res.success
        except (IndigoBlocked, IndigoError) as e:
            say(f"engine search refused: {e}")
            ok = False
        finally:
            await tab.close()
    say("RESULT: " + ("PASS" if ok else "BLOCKED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
