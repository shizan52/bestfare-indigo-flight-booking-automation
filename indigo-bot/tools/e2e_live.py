"""Live end-to-end test on the server: REAL worker + REAL database layer + REAL trusted Edge,
only Telegram delivery is replaced by printing. Uses its OWN scratch database so production data
stays untouched. Creates a real On-Hold booking (30 min, no payment) - use sparingly.

    cd D:\\vps\\apps\\indigo-bot\\app
    ..\\.venv\\Scripts\\python.exe tools\\e2e_live.py DEL BOM 02-12-2026 offer1 [pax]
"""
import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))      # the app modules live one level up
os.environ["INDIGO_BASE_DIR"] = r"D:\vps\apps\indigo-bot\e2e"
shutil.rmtree(os.environ["INDIGO_BASE_DIR"], ignore_errors=True)
Path(os.environ["INDIGO_BASE_DIR"]).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("CDP_ENDPOINTS", "http://127.0.0.1:9222")

import automation  # noqa: E402
import db  # noqa: E402
import worker  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main() -> None:
    o, d, date, offer = sys.argv[1:5]
    pax_n = int(sys.argv[5]) if len(sys.argv) > 5 else 1
    await db.init_db()

    print("\n[1] browser health:", json.dumps(await automation.health()))

    uid = 999001
    await db.upsert_user(uid, "e2e", "E2E Test")
    await db.add_credits(uid, 10, "e2e seed", admin_tg_id=1)
    pax = [{"first_name": n, "last_name": "TESTER", "gender": g, "dob": "15-03-1950"}
           for n, g in [("RAHUL", "M"), ("PRIYA", "F"), ("AMIT", "M"), ("NEHA", "F")][:pax_n]]
    row = {"job_id": "E2E00001", "offer": offer, "origin": o, "destination": d, "travel_date": date,
           "passengers": pax, "contact_email": "test.booking@example.com", "contact_phone": "9000000001",
           "credit_cost": pax_n, "declared_eligible": True}
    ids = await db.create_booking_charged(user_tg_id=uid, bookings=[row], total_cost=pax_n)

    async def credits() -> object:
        u = await db.get_user(uid)
        return u["credits"] if u else None

    print(f"[2] charged: credits 10 -> {await credits()} for {pax_n} pax | job {ids}")

    delivered: list = []

    async def notify(_chat, text):
        delivered.append(text)

    worker._notify = notify
    await worker.process("E2E00001")
    b = await db.get_booking("E2E00001") or {}
    print("[3] booking row  :", {k: b.get(k) for k in ("status", "pnr", "pnr_kind", "fare_amount", "coupon_applied",
                                                        "error_msg", "cleanup_at")})
    print("[4] credits after:", await credits())
    print("[5] message the user would receive on Telegram:\n" + "-" * 60)
    print(delivered[0] if delivered else "(none)")
    print("-" * 60)
    print("[6] ledger:", [(r["delta"], r["balance"], r["reason"]) for r in reversed(await db.get_ledger(uid))])


asyncio.run(main())

