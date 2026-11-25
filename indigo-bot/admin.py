"""The admin panel: everything an admin needs as buttons (approve people, give or take credits, watch bookings, pause
the service), plus the typed commands for power users.

Security: every entry point - command, button, shared-contact message, typed answer - checks `db.is_admin` first, because
a button's callback data can be forged by a modified Telegram client. Admins come only from the server's .env.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton as Btn
from telegram import KeyboardButton, KeyboardButtonRequestUsers, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

import automation
import common
import config
import db
import doctor
import ui
import worker

log = logging.getLogger("bot")
PAGE = 8
AWAIT = "await"                          # ctx.user_data[AWAIT] = {"kind": "credit"|"find"|"adduser", ...}
CANCEL_TXT = "❌ Cancel"


# ── small helpers ─────────────────────────────────────────────────────────
def _home() -> Btn:
    return Btn("🏠 Admin panel", callback_data="a:home")


def _short_name(u: dict, width: int = 20) -> str:
    n = (u.get("full_name") or "").strip() or (f"@{u['username']}" if u.get("username") else f"ID {u['telegram_id']}")
    return n if len(n) <= width else n[:width - 1] + "…"


def parse_int(s: str) -> Optional[int]:
    return int(s) if re.fullmatch(r"[+-]?\d{1,9}", s or "") else None


AWAIT_TTL = 180                          # a prompt that is not answered within 3 minutes is forgotten


def _expect(ctx: ContextTypes.DEFAULT_TYPE, **what) -> None:
    ctx.user_data[AWAIT] = {"ts": time.time(), **what}


async def _health() -> dict:
    try:
        return await asyncio.wait_for(automation.health(), 6)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "The booking browser did not answer."}


async def _tell(ctx: ContextTypes.DEFAULT_TYPE, tg: int, text: str, markup=None) -> bool:
    return (await common.send(ctx.bot, tg, text, markup)) is not None


# ── views: each returns (html text, inline keyboard) ──────────────────────
async def v_home() -> tuple[str, ui.Markup]:
    ov, h = await db.overview(), await _health()
    paused = await db.get_setting("bookings_paused") == "1"
    day = ov["day"]
    text = "\n".join([
        f"🛠 <b>Admin panel</b> · {ui.BRAND}", "",
        f"{'🟢' if h.get('ok') else '🔴'} Booking browser: <b>{'online' if h.get('ok') else 'offline'}</b>   ·   "
        f"Queue: <b>{worker.queue_size()}</b>",
        "⏸ Bookings are <b>paused</b>" if paused else "▶️ Bookings are <b>open</b>", "",
        f"👥 <b>{ov['active']}</b> active users   ·   🕓 <b>{ov['waiting']}</b> waiting for approval",
        f"💳 <b>{ov['credits']}</b> credits in circulation", "",
        f"📋 Last 24 hours: ✅ {day.get('done', 0)} done · ❌ {day.get('failed', 0)} failed · "
        f"⏳ {day.get('pending', 0) + day.get('running', 0)} in progress"])
    rows = [[Btn(f"🕓 Approvals ({ov['waiting']})", callback_data="a:ap:0"), Btn("👥 Users", callback_data="a:us:0")],
            [Btn("➕ Add a user", callback_data="a:add"), Btn("📋 Bookings", callback_data="a:bk:all")],
            [Btn("📊 Statistics", callback_data="a:st"), Btn("🩺 Health", callback_data="a:hl")],
            [Btn("📒 Credit history", callback_data="a:lg"), Btn("🗄 Backup", callback_data="a:bkp")],
            [Btn("▶️ Open bookings" if paused else "⏸ Pause bookings", callback_data="a:pz"),
             Btn("🔄 Refresh", callback_data="a:home")]]
    return text, ui.kb(rows)


async def v_approvals(index: int) -> tuple[str, ui.Markup]:
    rows = await db.pending_users(50)
    if not rows:
        return "🕓 <b>Waiting for approval</b>\n\nNobody is waiting right now. 🎉", ui.kb([[_home()]])
    index = max(0, min(index, len(rows) - 1))
    p = rows[index]
    text = (f"🕓 <b>Waiting for approval</b> · {index + 1} of {len(rows)}\n\n👤 {ui.person(p)}\n🆔 <code>{p['telegram_id']}</code>\n"
            f"First message: {ui.stamp(p['first_seen'])}\n\nApprove with starting credits:")
    tg = p["telegram_id"]
    buttons = [[Btn(f"✅ +{n}", callback_data=f"a:ok:{tg}:{n}:v") for n in (0, 3, 5, 10)],
               [Btn("🚫 Dismiss", callback_data=f"a:no:{tg}:v")]]
    if len(rows) > 1:
        buttons.append([Btn("◀ Previous", callback_data=f"a:ap:{(index - 1) % len(rows)}"),
                        Btn("Next ▶", callback_data=f"a:ap:{(index + 1) % len(rows)}")])
    buttons.append([_home()])
    return text, ui.kb(buttons)


async def v_users(page: int) -> tuple[str, ui.Markup]:
    total = await db.count_users()
    pages = max(1, math.ceil(total / PAGE))
    page = max(0, min(page, pages - 1))
    users = await db.users_page(page * PAGE, PAGE)
    text = f"👥 <b>Users</b> · {total}   <i>page {page + 1} of {pages}</i>\nTap a person to manage their credits."
    rows = [[Btn(f"{'🔑' if u['is_admin'] else '👤'}{'' if u['is_active'] else '🚫'} {_short_name(u)} · 💳 {u['credits']}",
                 callback_data=f"a:u:{u['telegram_id']}")] for u in users]
    if pages > 1:
        rows.append([Btn("◀", callback_data=f"a:us:{(page - 1) % pages}"), Btn(f"{page + 1}/{pages}", callback_data="a:nop"),
                     Btn("▶", callback_data=f"a:us:{(page + 1) % pages}")])
    rows.append([Btn("🔎 Find", callback_data="a:find"), Btn("➕ Add a user", callback_data="a:add")])
    rows.append([_home()])
    return text, ui.kb(rows)


async def v_user(tg: int, note: str = "") -> tuple[str, ui.Markup]:
    u = await db.get_user(tg)
    if not u:
        return "❔ That person is not registered.", ui.kb([[Btn("⬅️ Users", callback_data="a:us:0"), _home()]])
    counts = await db.user_booking_counts(tg)
    admin = bool(u["is_admin"]) or tg in config.ADMIN_IDS
    lines = [f"👤 <b>{ui.person(u)}</b>",
             f"🆔 <code>{tg}</code>   {'🔑 Admin   ' if admin else ''}{'🟢 Active' if u['is_active'] else '🚫 Deactivated'}",
             f"📅 Joined {ui.stamp(u['created_at'])}", "", f"💳 Credits: <b>{u['credits']}</b>",
             f"📋 Bookings: ✅ {counts.get('done', 0)} · ❌ {counts.get('failed', 0)} · "
             f"⏳ {counts.get('pending', 0) + counts.get('running', 0)}"]
    if note:
        lines += ["", note]
    rows = [[Btn(f"➕ {n}", callback_data=f"a:c:{tg}:{n}") for n in (1, 5, 10, 25)],
            [Btn("➖ 1", callback_data=f"a:c:{tg}:-1"), Btn("➖ 5", callback_data=f"a:c:{tg}:-5"),
             Btn("✏️ Custom amount", callback_data=f"a:cx:{tg}")]]
    second = [Btn("📒 History", callback_data=f"a:led:{tg}")]
    if not admin:
        second.insert(0, Btn("▶️ Activate" if not u["is_active"] else "⏸ Deactivate", callback_data=f"a:t:{tg}"))
    rows += [second, [Btn("⬅️ Users", callback_data="a:us:0"), _home()]]
    return "\n".join(lines), ui.kb(rows)


async def v_ledger(tg: Optional[int]) -> tuple[str, ui.Markup]:
    if tg is None:
        rows = await db.ledger_recent(15)
        head = "📒 <b>Recent credit activity</b>"
        lines = [f"• {ui.stamp(r['created_at'])} · {ui.esc(_short_name({'full_name': r.get('full_name'), 'username': r.get('username'), 'telegram_id': r['user_tg_id']}, 16))}"
                 f" · <b>{r['delta']:+d}</b> → {r['balance']} · {ui.esc(ui.ledger_label(r['reason']))}" for r in rows]
        back = [_home()]
    else:
        rows = await db.get_ledger(tg, 15)
        u = await db.get_user(tg)
        head = f"📒 <b>Credit history</b> · {ui.person(u) if u else tg}"
        lines = [f"• {ui.stamp(r['created_at'])} · <b>{r['delta']:+d}</b> → {r['balance']} · {ui.esc(ui.ledger_label(r['reason']))}"
                 for r in rows]
        back = [Btn("⬅️ Back to person", callback_data=f"a:u:{tg}"), _home()]
    return head + "\n\n" + ("\n".join(lines) if lines else "Nothing yet."), ui.kb([back])


FILTERS = {"all": (None, "All"), "active": (("pending", "running"), "⏳ Active"), "done": (("done",), "✅ Done"),
           "failed": (("failed",), "❌ Failed")}


async def v_bookings(flt: str) -> tuple[str, ui.Markup]:
    flt = flt if flt in FILTERS else "all"
    statuses, _ = FILTERS[flt]
    rows = await db.recent_bookings(10, statuses)
    users = {u["telegram_id"]: u for u in await db.get_all_users()}
    lines = []
    for b in rows:
        who = _short_name(users.get(b["user_tg_id"], {"telegram_id": b["user_tg_id"]}), 14)
        extra = (f" · <code>{ui.esc(b['pnr'])}</code>" if b["status"] == "done" and b.get("pnr") else
                 f"\n      <i>{ui.esc((b.get('error_msg') or '')[:80])}</i>" if b["status"] == "failed" else "")
        lines.append(f"{ui.STATUS_ICON.get(b['status'], '❔')} {ui.stamp(b['created_at'])} · {ui.esc(b['origin'])}→{ui.esc(b['destination'])} "
                     f"{ui.esc(b['travel_date'][:5])} · {ui.esc(ui.offer_short(b['offer']))} · 👤 {ui.esc(who)}{extra}")
    text = "📋 <b>Bookings</b>  <i>(latest 10, no passenger details)</i>\n\n" + ("\n".join(lines) if lines else "Nothing here yet.")
    row = [Btn(("• " if k == flt else "") + label, callback_data=f"a:bk:{k}") for k, (_s, label) in FILTERS.items()]
    return text, ui.kb([row, [_home()]])


async def v_stats() -> tuple[str, ui.Markup]:
    ov = await db.overview()
    day, tot = ov["day"], ov["total"]
    done, failed = tot.get("done", 0), tot.get("failed", 0)
    rate = f"{round(100 * done / (done + failed))}%" if done + failed else "—"
    text = "\n".join([
        "📊 <b>Statistics</b>", "",
        f"👥 Users: <b>{ov['users']}</b>  ({ov['active']} active · {ov['admins']} admins) · 🕓 {ov['waiting']} waiting",
        f"💳 Credits in circulation: <b>{ov['credits']}</b>", "", "<b>Last 24 hours</b>",
        f"✅ {day.get('done', 0)} done · ❌ {day.get('failed', 0)} failed · ⏳ {day.get('pending', 0) + day.get('running', 0)} in progress", "",
        "<b>All time</b>", f"✅ {done} done · ❌ {failed} failed · success rate <b>{rate}</b>"])
    return text, ui.kb([[Btn("🔄 Refresh", callback_data="a:st"), _home()]])


async def v_health(note: str = "") -> tuple[str, ui.Markup]:
    h = await _health()
    ok = bool(h.get("ok"))
    lines = ["🩺 <b>System health</b>", "", f"{'🟢' if ok else '🔴'} Booking browser: <b>{'online' if ok else 'offline'}</b>"]
    if ok:
        lines += [f"      {ui.esc(str(h.get('browser', '')))} · {h.get('tabs', '?')} tab(s) open", f"      Page: {ui.esc(str(h.get('title', '')))}"]
    else:
        lines += [f"      {ui.esc(str(h.get('error', 'not reachable')))}", "",
                  "<b>What to do:</b> on the server, open Edge with <code>tools\\start_trusted_edge.ps1</code>. "
                  "Bookings stay in the queue and are retried automatically."]
    if h.get("cooldown_s"):
        lines.append(f"⏳ IndiGo cool-down: {h['cooldown_s']} s (protects the connection after a block)")
    lines += ["", f"📥 Queue: <b>{worker.queue_size()}</b> waiting · <b>{worker.active_count()}</b> in progress"]
    if note:
        lines += ["", note]
    return "\n".join(lines), ui.kb([[Btn("🩹 Fix it now", callback_data="a:fix"), Btn("🔄 Check again", callback_data="a:hl")],
                                    [_home()]])


def _pick_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("👤 Pick a person", request_users=KeyboardButtonRequestUsers(
            request_id=1, user_is_bot=False, max_quantity=1, request_name=True, request_username=True))], [CANCEL_TXT]],
        resize_keyboard=True, one_time_keyboard=True)


# ── actions ───────────────────────────────────────────────────────────────
async def _approve(ctx: ContextTypes.DEFAULT_TYPE, admin_id: int, tg: int, credits: int) -> str:
    """Create / re-activate the user, give starting credits and welcome them. Returns a one-line result for the admin."""
    existing = await db.get_user(tg)
    if existing and existing["is_active"]:
        return "ℹ️ Already approved."
    seen = next((p for p in await db.pending_users(500) if p["telegram_id"] == tg), None)
    picked = ctx.user_data.get("picked", {}).get(tg)
    username = (seen or {}).get("username") or (picked[0] if picked else None)
    name = (seen or {}).get("full_name") or (picked[1] if picked else None)
    await db.upsert_user(tg, username, name)
    await db.set_active(tg, True)
    if credits:
        await db.add_credits(tg, credits, reason="welcome credits", admin_tg_id=admin_id)
    await db.dismiss_seen(tg)
    rec = await db.get_user(tg)
    sent = await _tell(ctx, tg, ui.welcome_text((name or "").split(" ")[0], rec["credits"] if rec else credits, False),
                       ui.main_menu(False))
    who = ui.person({"full_name": name, "username": username, "telegram_id": tg})
    return (f"✅ Approved {who} with <b>{ui.credit_word(credits)}</b>." +
            ("" if sent else "\n⚠️ I could not message them yet — ask them to open the bot and press <b>Start</b>."))


async def _change_credits(ctx: ContextTypes.DEFAULT_TYPE, admin_id: int, tg: int, delta: int) -> tuple[bool, str]:
    if delta == 0 or abs(delta) > 100000:
        return False, "Enter a whole number other than 0."
    if not await db.get_user(tg):
        return False, "That person is not registered."
    bal = await db.add_credits(tg, delta, reason="admin adjustment", admin_tg_id=admin_id)
    if bal is None:
        return False, "The balance cannot go below 0."
    await _tell(ctx, tg, f"💳 <b>Credits updated</b>: {delta:+d}  ·  balance <b>{bal}</b>")
    return True, f"✅ {delta:+d} → balance <b>{bal}</b>"


async def _send_backup(ctx: ContextTypes.DEFAULT_TYPE, admin_id: int) -> str:
    """Make a fresh backup of the credit records (no passenger data) and send the file to this admin as an off-server copy."""
    try:
        info = await worker.backup_now()
    except Exception as e:  # noqa: BLE001
        log.exception("manual backup failed")
        return f"❌ The backup could not be created: {str(e)[:100]}"
    caption = (f"🗄 <b>Backup</b> · {info['users']} users · {info['credit_log']} credit entries\n"
               "No passenger data inside. Keep this file somewhere safe (your computer or cloud drive).")
    if await common.send_file(ctx.bot, admin_id, Path(info["path"]), caption):
        return "🗄 Backup sent to this chat."
    return "⚠️ The backup was saved on the server, but I could not send the file here."


# ── entry points ──────────────────────────────────────────────────────────
async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    await common.send(ctx.bot, update.effective_user.id, await _send_backup(ctx, update.effective_user.id))


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    ctx.user_data.pop(AWAIT, None)
    text, markup = await v_home()
    await common.send(ctx.bot, update.effective_user.id, text, markup)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not await common.is_admin(update):                      # forged buttons end here
        await common.answer(q, "This is for admins only.", True)
        return
    toast: list = [None, False]
    try:
        await _dispatch(update, ctx, toast)
    except Exception:                                           # noqa: BLE001
        log.exception("admin callback failed")
        toast[:] = ["Something went wrong — please try again.", True]
    finally:
        await common.answer(q, toast[0], toast[1])


async def _dispatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE, toast: list) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    cmd, args = (parts[1] if len(parts) > 1 else ""), parts[2:]
    admin_id = update.effective_user.id
    mid = q.message.message_id if q.message else None

    async def show(text_markup: tuple) -> None:
        await common.show(ctx, admin_id, mid, *text_markup)

    if cmd == "nop":
        return
    if cmd != "cx":
        ctx.user_data.pop(AWAIT, None)                          # leaving a prompt cancels it
    if cmd == "home":
        await show(await v_home())
    elif cmd == "ap":
        await show(await v_approvals(int(args[0]) if args and args[0].isdigit() else 0))
    elif cmd in ("ok", "no") and args and args[0].isdigit():
        tg = int(args[0])
        in_view = args[-1] == "v"
        if cmd == "ok":
            credits = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
            result = await _approve(ctx, admin_id, tg, min(credits, 100000))
        else:
            await db.dismiss_seen(tg)
            result = "🚫 Dismissed."
        if in_view:
            toast[:] = [re.sub(r"<[^>]+>", "", result.split("\n")[0]), False]
            await show(await v_approvals(0))
        else:                                                   # the alert message itself becomes the receipt
            await show((result, ui.kb([[Btn("🕓 All requests", callback_data="a:ap:0"), _home()]])))
    elif cmd == "us":
        await show(await v_users(int(args[0]) if args and args[0].isdigit() else 0))
    elif cmd == "u" and args and args[0].isdigit():
        await show(await v_user(int(args[0])))
    elif cmd == "c" and len(args) == 2 and args[0].isdigit() and parse_int(args[1]) is not None:
        tg, delta = int(args[0]), int(args[1])
        ok, note = await _change_credits(ctx, admin_id, tg, delta)
        if not ok:
            toast[:] = [note, True]
        await show(await v_user(tg, note if ok else ""))
    elif cmd == "cx" and args and args[0].isdigit():
        tg = int(args[0])
        u = await db.get_user(tg)
        if not u:
            toast[:] = ["That person is not registered.", True]
            return
        _expect(ctx, kind="credit", tg=tg, mid=mid)
        await show((f"✏️ <b>Custom amount</b> for {ui.person(u)}\n\nSend a number — for example <code>12</code> to add 12 "
                    "credits or <code>-3</code> to take 3 away.", ui.kb([[Btn("❌ Cancel", callback_data=f"a:u:{tg}")]])))
    elif cmd == "t" and args and args[0].isdigit():
        tg = int(args[0])
        u = await db.get_user(tg)
        if not u:
            toast[:] = ["That person is not registered.", True]
            return
        if u["is_admin"] or tg in config.ADMIN_IDS:
            toast[:] = ["Admins cannot be deactivated here.", True]
            return
        await db.set_active(tg, not u["is_active"])
        await show(await v_user(tg, "▶️ Activated." if not u["is_active"] else "⏸ Deactivated — credits and history are kept."))
    elif cmd == "led":
        await show(await v_ledger(int(args[0]) if args and args[0].isdigit() else None))
    elif cmd == "lg":
        await show(await v_ledger(None))
    elif cmd == "bk":
        await show(await v_bookings(args[0] if args else "all"))
    elif cmd == "st":
        await show(await v_stats())
    elif cmd == "hl":
        await show(await v_health())
    elif cmd == "fix":                                          # the admin asks the doctor to repair the browser now
        fix = await automation.heal("an admin pressed Fix it now", force=True)
        mark = "🩹" if fix.get("ok") else "⚠️"
        await show(await v_health(f"{mark} <b>Repair:</b> {ui.esc(doctor.describe(fix) or 'nothing needed fixing.')}"))
    elif cmd == "bkp":
        toast[:] = [await _send_backup(ctx, admin_id), False]
    elif cmd == "pz":
        paused = await db.get_setting("bookings_paused") == "1"
        await db.set_setting("bookings_paused", "0" if paused else "1")
        toast[:] = ["Bookings are open again." if paused else "Bookings are paused. People will see a short notice.", False]
        await show(await v_home())
    elif cmd == "find":
        _expect(ctx, kind="find", mid=mid)
        await show(("🔎 <b>Find a person</b>\n\nSend a name, an @username or a Telegram ID.",
                    ui.kb([[Btn("❌ Cancel", callback_data="a:us:0")]])))
    elif cmd == "add":
        _expect(ctx, kind="adduser", mid=mid)
        await show(("➕ <b>Add a user</b>\n\nUse the button below to pick them from your chats — or type their numeric "
                    "Telegram ID.\n<i>They can also just open the bot and press Start; you will get an approval request.</i>",
                    ui.kb([[Btn("❌ Cancel", callback_data="a:home")]])))
        await common.send(ctx.bot, admin_id, "👇 Pick a person, or type an ID:", _pick_keyboard())
    else:
        toast[:] = ["That button is no longer available.", False]


async def _restore_menu(ctx: ContextTypes.DEFAULT_TYPE, admin_id: int, text: str = "👌") -> None:
    await common.send(ctx.bot, admin_id, text, ui.main_menu(True))


async def _offer_person(ctx: ContextTypes.DEFAULT_TYPE, admin_id: int, tg: int, mid: Optional[int]) -> None:
    """After an id arrived: manage them if known, otherwise offer the starting-credit buttons."""
    u = await db.get_user(tg)
    if u:
        text, markup = await v_user(tg)
    else:
        picked = ctx.user_data.get("picked", {}).get(tg)
        who = ui.person({"full_name": picked[1] if picked else None, "username": picked[0] if picked else None, "telegram_id": tg})
        text = f"➕ <b>Add {who}</b>\n🆔 <code>{tg}</code>\n\nStarting credits:"
        markup = ui.kb([[Btn(f"✅ +{n}", callback_data=f"a:ok:{tg}:{n}") for n in (0, 3, 5, 10)], [_home()]])
    await common.show(ctx, admin_id, None, text, markup)


async def on_users_shared(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The admin picked somebody with the 'Pick a person' button."""
    if not await common.is_admin(update):
        return
    shared = update.effective_message.users_shared
    aw = ctx.user_data.pop(AWAIT, None)
    if not shared or not shared.users or not aw or aw.get("kind") != "adduser":
        return
    su = shared.users[0]
    name = " ".join(x for x in (su.first_name, su.last_name) if x) or None
    ctx.user_data.setdefault("picked", {})[su.user_id] = (su.username, name)
    admin_id = update.effective_user.id
    await _restore_menu(ctx, admin_id, "👌 Got it.")
    await _offer_person(ctx, admin_id, su.user_id, aw.get("mid"))


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """An admin's typed answer to one of the prompts above. True when it was consumed."""
    aw = ctx.user_data.get(AWAIT)
    if not aw:
        return False
    if time.time() - aw.get("ts", 0) > AWAIT_TTL or not await common.is_admin(update):
        ctx.user_data.pop(AWAIT, None)
        return False
    admin_id = update.effective_user.id
    msg = update.effective_message
    text = (msg.text or "").strip()
    kind = aw["kind"]
    if text == CANCEL_TXT:
        ctx.user_data.pop(AWAIT, None)
        await _restore_menu(ctx, admin_id, "Cancelled.")
        return True
    if kind == "credit":
        amount = parse_int(text)
        ok, note = await _change_credits(ctx, admin_id, aw["tg"], amount) if amount is not None else (False, "Send a whole number, e.g. 12 or -3.")
        await common.delete(ctx.bot, admin_id, msg.message_id)
        if ok:
            ctx.user_data.pop(AWAIT, None)
            await common.show(ctx, admin_id, aw.get("mid"), *await v_user(aw["tg"], note))
        else:
            await common.send(ctx.bot, admin_id, f"⚠️ {note}")
        return True
    if kind == "find":
        hits = await db.search_users(text)
        ctx.user_data.pop(AWAIT, None)
        await common.delete(ctx.bot, admin_id, msg.message_id)
        if len(hits) == 1:
            await common.show(ctx, admin_id, aw.get("mid"), *await v_user(hits[0]["telegram_id"]))
            return True
        rows = [[Btn(f"{'🔑' if u['is_admin'] else '👤'} {_short_name(u)} · 💳 {u['credits']}", callback_data=f"a:u:{u['telegram_id']}")]
                for u in hits]
        rows.append([Btn("🔎 Search again", callback_data="a:find"), _home()])
        await common.show(ctx, admin_id, aw.get("mid"),
                          f"🔎 <b>{len(hits)} result(s)</b> for “{ui.esc(text[:40])}”" if hits else
                          f"🔎 Nobody matches “{ui.esc(text[:40])}”.", ui.kb(rows))
        return True
    if kind == "adduser":
        tg = parse_int(text)
        if tg is None or tg < 10000:
            await common.send(ctx.bot, admin_id, "⚠️ That does not look like a Telegram ID (a number with at least 5 digits).")
            return True
        ctx.user_data.pop(AWAIT, None)
        await common.delete(ctx.bot, admin_id, msg.message_id)
        await _restore_menu(ctx, admin_id, "👌 Got it.")
        await _offer_person(ctx, admin_id, tg, aw.get("mid"))
        return True
    ctx.user_data.pop(AWAIT, None)
    return False


# ── typed commands (kept for power users; the panel does the same with buttons) ──
async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    tg = parse_int(ctx.args[0]) if ctx.args else None
    credits = parse_int(ctx.args[1]) if len(ctx.args) > 1 else 0
    if tg is None or tg <= 0 or credits is None or credits < 0:
        await common.send(ctx.bot, update.effective_user.id, "Usage: /adduser &lt;telegram_id&gt; [credits]\n"
                                                              "Tip: the <b>🛠 Admin panel</b> does this with buttons.")
        return
    result = await _approve(ctx, update.effective_user.id, tg, credits)
    if result.startswith("ℹ️") and credits:                      # already active: top up instead
        _ok, result = await _change_credits(ctx, update.effective_user.id, tg, credits)
    await common.send(ctx.bot, update.effective_user.id, result)


async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    tg = parse_int(ctx.args[0]) if ctx.args else None
    if tg is None or tg in config.ADMIN_IDS or not await db.set_active(tg, False):
        await common.send(ctx.bot, update.effective_user.id,
                          "Usage: /removeuser &lt;telegram_id&gt;  (unknown user, or an admin, which cannot be removed here)")
        return
    await common.send(ctx.bot, update.effective_user.id, f"🚫 <code>{tg}</code> deactivated (credits and history are kept).")


async def cmd_addcredit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    tg = parse_int(ctx.args[0]) if len(ctx.args) > 0 else None
    amt = parse_int(ctx.args[1]) if len(ctx.args) > 1 else None
    if tg is None or amt is None:
        await common.send(ctx.bot, update.effective_user.id, "Usage: /addcredit &lt;telegram_id&gt; &lt;+/-amount&gt;")
        return
    ok, note = await _change_credits(ctx, update.effective_user.id, tg, amt)
    await common.send(ctx.bot, update.effective_user.id, f"<code>{tg}</code>: {note}" if ok else f"❌ {note}")


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    text, markup = await v_users(0)
    await common.send(ctx.bot, update.effective_user.id, text, markup)


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    text, markup = await v_approvals(0)
    await common.send(ctx.bot, update.effective_user.id, text, markup)


async def cmd_ledger(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    tg = parse_int(ctx.args[0]) if ctx.args else None
    text, markup = await v_ledger(tg)
    await common.send(ctx.bot, update.effective_user.id, text, markup)


async def cmd_bookings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    text, markup = await v_bookings("all")
    await common.send(ctx.bot, update.effective_user.id, text, markup)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    text, markup = await v_stats()
    await common.send(ctx.bot, update.effective_user.id, text, markup)


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await common.admin_only(update, ctx):
        return
    text, markup = await v_health()
    await common.send(ctx.bot, update.effective_user.id, text, markup)
