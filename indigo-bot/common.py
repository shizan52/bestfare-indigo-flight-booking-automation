"""Telegram helpers shared by the user and admin screens. Every call goes through `bot.<method>` so tests can fake the
transport, and every failure is swallowed after logging - a blocked user or an edited-away message must never crash a
handler."""
from __future__ import annotations

import html
import logging
import time
from collections import deque
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

import config
import db
import ui

log = logging.getLogger("bot")
H = ParseMode.HTML
esc = html.escape


async def send(bot, chat_id: int, text: str, markup=None):
    try:
        return await bot.send_message(chat_id, text, parse_mode=H, reply_markup=markup, disable_web_page_preview=True)
    except TelegramError as e:
        log.warning("could not send to %s: %s", chat_id, e)
        return None


async def edit(bot, chat_id: int, message_id: int, text: str, markup=None) -> bool:
    """Edit a message in place. True when it now shows `text` (also when nothing had to change)."""
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode=H, reply_markup=markup,
                                    disable_web_page_preview=True)
        return True
    except BadRequest as e:
        return "not modified" in str(e).lower()
    except TelegramError as e:
        log.warning("could not edit %s/%s: %s", chat_id, message_id, e)
        return False


async def send_file(bot, chat_id: int, path, caption: str = "") -> bool:
    """Send a file as a document. True when it arrived."""
    try:
        data = path.read_bytes()
        await bot.send_document(chat_id, document=data, filename=path.name, caption=caption, parse_mode=H,
                                write_timeout=60, read_timeout=60)
        return True
    except (TelegramError, OSError) as e:
        log.warning("could not send the file %s to %s: %s", getattr(path, "name", path), chat_id, e)
        return False


async def delete(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def answer(q, text: Optional[str] = None, alert: bool = False) -> None:
    try:
        await q.answer(text, show_alert=alert)
    except TelegramError:
        pass


async def show(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: Optional[int], text: str, markup=None) -> Optional[int]:
    """Edit the given message, or send a fresh one when there is none / it can no longer be edited. Returns its id."""
    if message_id and await edit(ctx.bot, chat_id, message_id, text, markup):
        return message_id
    msg = await send(ctx.bot, chat_id, text, markup)
    return msg.message_id if msg else None


async def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and await db.is_admin(u.id))


async def admin_only(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """The gate in front of every admin command and button."""
    if await is_admin(update):
        return True
    chat = update.effective_chat
    if chat:
        await send(ctx.bot, chat.id, "🔒 This is for admins only.")
    return False


async def registered(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    """The user's record when they are active; otherwise show the friendly 'waiting for approval' card, tell the
    admins (with approve buttons) and return None."""
    u = update.effective_user
    if not u:
        return None
    rec = await db.get_user(u.id)
    if rec and rec["is_active"]:
        return rec
    await db.note_seen(u.id, u.username, u.full_name)
    if rec:                                         # switched off by an admin on purpose: no new alert, just a clear answer
        await send(ctx.bot, u.id, ui.deactivated_text(), ui.pending_menu())
        return None
    await send(ctx.bot, u.id, ui.pending_text(u.id), ui.pending_menu())
    await alert_new_request(ctx.bot, u)
    return None


_recent_alerts: deque = deque(maxlen=60)


async def alert_new_request(bot, user) -> None:
    """Tell every admin that somebody is waiting - once per person per 6 hours, and never more than 20 a hour."""
    now = time.time()
    last = await db.get_setting(f"req_alert_{user.id}")
    if last and now - float(last) < 6 * 3600:
        return
    while _recent_alerts and now - _recent_alerts[0] > 3600:
        _recent_alerts.popleft()
    if len(_recent_alerts) >= 20:
        return
    _recent_alerts.append(now)
    await db.set_setting(f"req_alert_{user.id}", str(now))
    for tg in sorted(set(config.ADMIN_IDS)):
        await send(bot, tg, ui.request_alert_text(user), ui.request_alert_markup(user.id))
