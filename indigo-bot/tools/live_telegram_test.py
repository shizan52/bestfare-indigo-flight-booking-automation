"""Live booking test THROUGH TELEGRAM.

Everything is real except the taps: the real bot code and handlers, the real Telegram Bot API (every card, every edit and
the final PNR message really arrive in the tester's Telegram chat), the real database, the real queue/worker and the real
IndiGo engine in the trusted Edge. A bot cannot press its own buttons, so this script plays the person: it hands the bot
the same updates Telegram sends when the tester taps and types, as the tester's own Telegram account.

Run ON THE SERVER with the bot service stopped for the ~3 minutes (so only one process drives Edge):
    cd D:\\vps\\apps\\indigo-bot\\app
    ..\\.venv\\Scripts\\python.exe tools\\live_telegram_test.py --tester 6128169627 --date 10-12-2026
It first gives the tester 1 credit through the admin panel (the tester must be an admin), then books ONE passenger.
That creates ONE real 30-minute On-Hold booking at IndiGo (no payment).
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))      # the app modules live one level up
os.environ.setdefault("INDIGO_BASE_DIR", r"D:\vps\apps\indigo-bot")


def args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tester", type=int, required=True, help="Telegram id of the person (an admin) doing the test")
    ap.add_argument("--date", required=True, help="travel date DD-MM-YYYY")
    ap.add_argument("--origin", default="DEL")
    ap.add_argument("--dest", default="BOM")
    ap.add_argument("--offer", default="offer1", choices=("offer1", "offer2", "both"),
                    help="'both' makes TWO bookings (one per offer) in one session")
    ap.add_argument("--name", default="RAHUL TESTER")
    ap.add_argument("--gender", default="M", choices=("M", "F"))
    ap.add_argument("--phone", default="9000000001")
    ap.add_argument("--email", default="test.booking@example.com")
    return ap.parse_args()


async def main() -> int:
    a = args()
    import bot
    import db
    import ui
    import wizard
    import worker
    from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User
    from telegram.ext import ExtBot

    await db.init_db()
    bot.RATE_MAX = 10 ** 9                                            # this script "taps" much faster than a person
    app = bot.build_app()
    out: list[tuple] = []                                             # (kind, chat, message id, text) really sent
    real_send, real_edit = ExtBot.send_message, ExtBot.edit_message_text

    async def send_message(self, chat_id, text, *x, **kw):
        m = await real_send(self, chat_id, text, *x, **kw)
        out.append(("sent", chat_id, m.message_id, text))
        return m

    async def edit_message_text(self, text, *x, **kw):
        r = await real_edit(self, text, *x, **kw)
        out.append(("edited", kw.get("chat_id"), kw.get("message_id"), text))
        return r

    real_doc = ExtBot.send_document

    async def send_document(self, chat_id, *x, **kw):
        m = await real_doc(self, chat_id, *x, **kw)
        out.append(("file", chat_id, m.message_id, f"{kw.get('filename')} - {kw.get('caption', '')}"))
        return m

    async def nothing(self, *x, **kw):                                # the taps are ours: nothing to answer or delete
        return True
    ExtBot.send_message, ExtBot.edit_message_text, ExtBot.send_document = send_message, edit_message_text, send_document
    ExtBot.answer_callback_query, ExtBot.delete_message = nothing, nothing
    await app.initialize()

    rec = await db.get_user(a.tester)
    if not rec or not await db.is_admin(a.tester):
        print("The tester must be an active admin of the bot.")
        return 2
    user = User(a.tester, (rec.get("full_name") or "Tester").split(" ")[0], False, username=rec.get("username"))
    ids = itertools.count(int(time.time()))

    def now() -> datetime:
        return datetime.now(timezone.utc)

    async def say(text: str) -> None:
        ents = [MessageEntity("bot_command", 0, len(text.split()[0]))] if text.startswith("/") else None
        m = Message(next(ids), now(), Chat(a.tester, "private"), from_user=user, text=text, entities=ents)
        m.set_bot(app.bot)
        await app.process_update(Update(next(ids), message=m))

    async def tap(data: str, mid: int) -> None:
        q = CallbackQuery(f"live{next(ids)}", user, "live", data=data, message=Message(mid, now(), Chat(a.tester, "private")))
        q.set_bot(app.bot)
        await app.process_update(Update(next(ids), callback_query=q))

    def last_mid() -> int:
        return next(m for _k, c, m, _t in reversed(out) if c == a.tester)

    def show(title: str) -> None:
        print(f"\n=== {title} ===")

    def step_now() -> str:
        wz = app.user_data[a.tester].get(wizard.KEY)
        return wizard.cur(wz)[0] if wz else "(no booking open)"

    t_start = time.monotonic()
    try:
        need = 2 if a.offer == "both" else 1
        show(f"1. Admin: open the panel and give the tester {need} credit(s) for this test")
        before = rec["credits"]
        await say("/admin")
        panel = last_mid()
        await tap(f"a:u:{a.tester}", panel)
        await tap(f"a:c:{a.tester}:{need}", panel)
        print(f"credits {before} -> {(await db.get_user(a.tester))['credits']}")

        show("2. Customer: book a flight in Telegram")
        await say(ui.MENU_BOOK)
        wz = app.user_data[a.tester][wizard.KEY]
        sid, card = wz["sid"], wz["mid"]
        travel = datetime.strptime(a.date, "%d-%m-%Y").date().isoformat()
        script = [("tap", f"w:{sid}:f:{a.origin}", "to"), ("tap", f"w:{sid}:t:{a.dest}", "date"),
                  ("tap", f"w:{sid}:d:{travel}", None)]
        for kind, data, expect in script:
            await tap(data, card)
            if expect and step_now() != expect:
                print(f"stopped: expected the '{expect}' question, the card is at '{step_now()}'")
                return 1
        if step_now() == "pax":
            await tap(f"w:{sid}:n:1", card)
        for kind, data, expect in [("say", a.name, "g"), ("tap", f"w:{sid}:g:{a.gender}", "phone"),
                                   ("say", a.phone, "email"), ("say", a.email, "offer"),
                                   ("tap", f"w:{sid}:o:{a.offer}", "review")]:
            await (say(data) if kind == "say" else tap(data, card))
            if step_now() != expect:
                print(f"stopped: expected the '{expect}' question, the card is at '{step_now()}'")
                return 1
            print(f"  ok -> {expect}")
        await tap(f"w:{sid}:ok", card)
        mine = list(reversed([m for m in await db.get_user_bookings(a.tester, limit=need) if m["status"] == "pending"]))
        if len(mine) != need:
            print(f"stopped: Book now should have created {need} booking(s), found {len(mine)}")
            return 1
        print(f"{need} booking request(s) created {[m['job_id'] for m in mine]}; "
              f"credits now {(await db.get_user(a.tester))['credits']}")

        show("3. The booking engine runs (real IndiGo in the trusted Edge)")
        worker._notify = bot.make_notifier(app)
        worker._alert_admins = lambda text: bot._alert_admins(app, text)
        results = []
        for m in mine:                                                # one after the other, like the real worker
            t0 = time.monotonic()
            await worker.process(m["job_id"])
            b = await db.get_booking(m["job_id"])
            results.append(b)
            print(f"{b['offer']}: status={b['status']} pnr={b['pnr']} kind={b['pnr_kind']} fare={b['fare_amount']} "
                  f"coupon_applied={b['coupon_applied']} hold_until={b['hold_until']} ({time.monotonic() - t0:.0f} s)")

        show("4. Customer: open My bookings")
        await say(ui.MENU_BOOKINGS)

        show("5. Admin: /backup (a real file upload to Telegram)")
        await say("/backup")
        files = [t for k, c, _m, t in out if c == a.tester and k == "file"]
        print("file received in Telegram:", files[-1].splitlines()[0][:80] if files else "NOTHING SENT")

        show("What arrived in the tester's Telegram")
        for kind, chat, mid, text in out:
            if chat == a.tester:
                first = text.replace("<b>", "").replace("</b>", "").splitlines()[0][:70]
                print(f"  {kind:6} #{mid}: {first}")
        ok = True
        for b in results:
            delivered = any(c == a.tester and k == "sent" and b["pnr"] and b["pnr"] in t for k, c, _m, t in out)
            ok = ok and b["status"] == "done" and b["pnr_kind"] == "record_locator" and delivered
            print(f"  {b['offer']}: PNR {b['pnr']} {'delivered to Telegram' if delivered else 'NOT delivered'}")
        ok = ok and bool(files)
        print(f"\nRESULT: {'PASS' if ok else 'CHECK'} - {len(results)} booking(s) - total {time.monotonic() - t_start:.0f} s - "
              f"credits now {(await db.get_user(a.tester))['credits']}")
        return 0 if ok else 1
    finally:
        await app.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
