"""Best Fare - IndiGo booking Telegram bot (English only).

Everything works with buttons: a persistent menu for people, one booking card that is edited in place (wizard.py),
and a full admin panel (admin.py). Typed commands still work:
  User : /start /book /balance /mybookings /cancel /help
  Admin: /admin /adduser /removeuser /addcredit /users /pending /ledger /bookings /stats /health
The admin list lives ONLY in the server's .env (ADMIN_IDS); no command can create an admin.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Optional

from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, TypeHandler, filters)

import admin
import automation
import common
import config
import db
import doctor
import ui
import wizard
import worker
from indigo_client import Pace

UI_VERSION = "3"                                   # bump to show admins the "what's new" card once


class _RedactTokens(logging.Filter):
    """Never let the bot token (embedded in Telegram API URLs) reach a log file."""
    _pat = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self._pat.sub("<BOT_TOKEN>", record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001
            pass
        return True


_h = logging.StreamHandler()
_h.addFilter(_RedactTokens())
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", handlers=[_h])
for _noisy in ("httpx", "httpcore"):          # these log full request URLs (= the token)
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("bot")

PUBLIC_COMMANDS = [
    ("start", "Open the menu"), ("book", "Book a flight"), ("balance", "Show your credits"),
    ("mybookings", "Your latest bookings"), ("cancel", "Close the booking form"), ("help", "How Best Fare works"),
]
ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    ("admin", "Open the admin panel"), ("adduser", "Add a user: /adduser <id> [credits]"),
    ("addcredit", "Add/remove credits: /addcredit <id> <+/-n>"), ("removeuser", "Deactivate a user"),
    ("users", "List all users"), ("pending", "People waiting for approval"), ("ledger", "Credit history"),
    ("bookings", "Recent bookings"), ("stats", "System statistics"), ("health", "Booking browser health"),
    ("backup", "Send me a backup of the credit records"),
]
# public texts never contain the coupon codes - customers only ever see "Offer 1" / "Offer 2"
SHORT_DESCRIPTION = "Best Fare - book IndiGo flights at the lowest fare, right here in Telegram."
DESCRIPTION = ("Best Fare finds the lowest IndiGo fare and books it for you with your Defence / NCC or Senior Citizen "
               "offer. Choose your cities and date, say who is travelling, and your PNR arrives right here in about two "
               "minutes. Tap Start to begin.")
WHATS_NEW = ("✨ <b>Best Fare is now even simpler for your users</b>\n\n"
             "• One big <b>✈️ Book a flight</b> button\n"
             "• Fewer questions: one name field, no date of birth, quick day buttons (a calendar only if needed)\n"
             "• Plain offer names — customers never see the coupon codes\n"
             "• A clear review screen with <b>Book now</b> and <b>Change</b>\n\n"
             "Your <b>🛠 Admin panel</b> works as before.")


# ── people's screens ──────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    if u.id in config.ADMIN_IDS:                       # the only place an admin flag is ever set
        await db.upsert_user(u.id, u.username, u.full_name)
        await db.set_admin(u.id, True)
    rec = await common.registered(update, ctx)
    if not rec:
        return
    is_admin = await db.is_admin(u.id)
    await common.send(ctx.bot, u.id, ui.welcome_text(u.first_name, rec["credits"], is_admin), ui.main_menu(is_admin))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if u:
        await common.send(ctx.bot, u.id, ui.HELP_TEXT, ui.help_markup())


async def show_credits(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    rec = await common.registered(update, ctx)
    if not rec:
        return
    ledger = await db.get_ledger(rec["telegram_id"], 6)
    await common.send(ctx.bot, rec["telegram_id"], ui.credits_text(rec, ledger), ui.book_markup())


async def show_bookings(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit_mid: Optional[int] = None) -> None:
    rec = await common.registered(update, ctx)
    if not rec:
        return
    rows = await db.get_user_bookings(rec["telegram_id"], limit=5)
    text, markup = ui.bookings_text(rows), ui.bookings_markup(rows)
    if edit_mid:
        await common.show(ctx, rec["telegram_id"], edit_mid, text, markup)
    else:
        await common.send(ctx.bot, rec["telegram_id"], text, markup)


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await show_credits(update, ctx)


async def cmd_mybookings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await show_bookings(update, ctx)


async def cmd_book(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await wizard.start(update, ctx)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop(admin.AWAIT, None)
    if not await wizard.cancel(update, ctx) and update.effective_user:
        await common.send(ctx.bot, update.effective_user.id, "Nothing to cancel — you have no open form.")


async def _status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """'Check my status' for people who are waiting for approval."""
    u = update.effective_user
    rec = await db.get_user(u.id) if u else None
    if rec and rec["is_active"]:
        await cmd_start(update, ctx)
    elif u:
        await db.note_seen(u.id, u.username, u.full_name)
        await common.send(ctx.bot, u.id, "🕓 <b>Still waiting for approval.</b>\nThe admins have been told — you will get a "
                                         "message here the moment you are approved.", ui.pending_menu())


async def _menu_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    ctx.user_data.pop(admin.AWAIT, None)               # tapping the menu abandons any admin prompt
    if text == ui.MENU_BOOK:
        await wizard.start(update, ctx)
    elif text == ui.MENU_CREDITS:
        await show_credits(update, ctx)
    elif text == ui.MENU_BOOKINGS:
        await show_bookings(update, ctx)
    elif text == ui.MENU_HELP:
        await cmd_help(update, ctx)
    elif text == ui.MENU_ADMIN:
        await admin.cmd_admin(update, ctx)
    elif text == ui.MENU_STATUS:
        await _status(update, ctx)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Every typed message: menu buttons first, then an admin prompt, then the booking form."""
    msg, u = update.effective_message, update.effective_user
    if not msg or not u or not msg.text:
        return
    text = msg.text.strip()
    if text in ui.MENU_TEXTS:
        await _menu_text(update, ctx, text)
        return
    if await admin.handle_text(update, ctx) or await wizard.handle_text(update, ctx):
        return
    rec = await db.get_user(u.id)
    if not rec or not rec["is_active"]:
        await common.registered(update, ctx)
        return
    await common.send(ctx.bot, u.id, "🤔 I did not get that. Use the buttons below, or tap <b>❓ Help</b>.",
                      ui.main_menu(await db.is_admin(u.id)))


async def _help_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Buttons of the older multi-page help: show the (now single) help page in place."""
    q = update.callback_query
    await common.answer(q)
    if q.message:
        await common.show(ctx, q.message.chat.id, q.message.message_id, ui.HELP_TEXT, ui.help_markup())


async def _menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    action = (q.data or "")[2:]
    toast: Optional[str] = None
    if action == "book":
        await common.answer(q)
        await wizard.start(update, ctx)
    elif action == "credits":
        await common.answer(q)
        await show_credits(update, ctx)
    elif action == "bookings":
        await common.answer(q)
        await show_bookings(update, ctx)                                   # a new message (never overwrite a PNR card)
    elif action == "refresh" and q.message:
        await common.answer(q)
        await show_bookings(update, ctx, edit_mid=q.message.message_id)    # the list refreshes itself in place
    elif action.startswith("cx:") and q.message and update.effective_user:
        bal = await db.cancel_pending(action[3:], update.effective_user.id)
        toast = ("Too late — this booking has already started." if bal is None else
                 f"Cancelled. Your credits are back (balance {bal}).")
        await common.answer(q, toast, bal is None)
        await show_bookings(update, ctx, edit_mid=q.message.message_id)
    elif action == "help":
        await common.answer(q)
        await cmd_help(update, ctx)
    else:
        await common.answer(q)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = (q.data or "") if q else ""
    if data.startswith("w:"):
        await wizard.on_callback(update, ctx)
    elif data.startswith("a:"):
        await admin.on_callback(update, ctx)
    elif data.startswith("h:"):
        await _help_cb(update, ctx)
    elif data.startswith("m:"):
        await _menu_cb(update, ctx)
    elif q:
        await common.answer(q)


async def on_unknown_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    rec = await common.registered(update, ctx)
    if rec:
        await common.send(ctx.bot, u.id, "🤔 I do not know that command. Use the buttons below, or tap <b>❓ Help</b>.",
                          ui.main_menu(await db.is_admin(u.id)))


async def on_other(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo, sticker, voice note ... - say kindly that only text works."""
    u = update.effective_user
    if u:
        await common.send(ctx.bot, u.id, "📎 I can only read text. Please use the buttons, or type your answer.")


RATE_WINDOW, RATE_MAX = 10.0, 14          # at most 14 taps/messages per person per 10 seconds - far above a human's pace
_recent: dict[int, deque] = {}


async def rate_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler: somebody (or a script) flooding the bot is ignored for a few seconds, so one noisy
    account can never slow the bot down for everybody else or trigger Telegram's flood limits."""
    u = update.effective_user
    if not u:
        return
    now = time.monotonic()
    dq = _recent.setdefault(u.id, deque())
    while dq and now - dq[0] > RATE_WINDOW:
        dq.popleft()
    dq.append(now)
    if len(_recent) > 5000:                                     # forget people who have gone quiet
        for uid in [k for k, v in _recent.items() if not v or now - v[-1] > RATE_WINDOW]:
            _recent.pop(uid, None)
    if len(dq) > RATE_MAX:
        if update.callback_query:
            await common.answer(update.callback_query)          # stop the button's spinner
        raise ApplicationHandlerStop


_last_error_notice: dict[int, float] = {}


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler error", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_user:
        uid, now = update.effective_user.id, time.time()
        if now - _last_error_notice.get(uid, 0) > 60:                       # at most one apology a minute
            _last_error_notice[uid] = now
            await common.send(ctx.bot, uid, "😕 <b>Something went wrong on our side.</b>\nPlease try again — if it keeps "
                                            "happening, tell an admin.")


# ── wiring ────────────────────────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).build()
    private = filters.ChatType.PRIVATE
    commands = [("start", cmd_start), ("help", cmd_help), ("book", cmd_book), ("cancel", cmd_cancel),
                ("balance", cmd_balance), ("mybookings", cmd_mybookings), ("admin", admin.cmd_admin),
                ("adduser", admin.cmd_adduser), ("removeuser", admin.cmd_removeuser), ("addcredit", admin.cmd_addcredit),
                ("users", admin.cmd_users), ("pending", admin.cmd_pending), ("ledger", admin.cmd_ledger),
                ("bookings", admin.cmd_bookings), ("stats", admin.cmd_stats), ("health", admin.cmd_health),
                ("backup", admin.cmd_backup)]
    app.add_handler(TypeHandler(Update, rate_gate), group=-1)
    for name, fn in commands:
        app.add_handler(CommandHandler(name, fn, filters=private))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(private & filters.StatusUpdate.USERS_SHARED, admin.on_users_shared))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(private & filters.COMMAND, on_unknown_command))       # after the real commands
    app.add_handler(MessageHandler(private & ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL, on_other))
    app.add_error_handler(on_error)
    return app


async def _admin_ids() -> list[int]:
    return sorted(set(config.ADMIN_IDS))


async def _alert_admins(app: Application, text: str) -> None:
    for tg in await _admin_ids():
        await common.send(app.bot, tg, f"⚠️ {ui.esc(text)}")


async def _set_profile(bot) -> None:
    """Name, descriptions and command menus - only what changed, so Telegram's rate limits are never hit."""
    try:
        if (await bot.get_my_name()).name != ui.BRAND:
            await bot.set_my_name(ui.BRAND)
        if (await bot.get_my_short_description()).short_description != SHORT_DESCRIPTION:
            await bot.set_my_short_description(SHORT_DESCRIPTION)
        if (await bot.get_my_description()).description != DESCRIPTION:
            await bot.set_my_description(DESCRIPTION)
    except Exception:  # noqa: BLE001 - cosmetic; never block start-up
        log.exception("could not update the bot profile")
    try:
        await bot.set_my_commands([BotCommand(c, d) for c, d in PUBLIC_COMMANDS])
        for tg in await _admin_ids():
            await bot.set_my_commands([BotCommand(c, d) for c, d in ADMIN_COMMANDS], scope=BotCommandScopeChat(tg))
    except Exception:  # noqa: BLE001
        log.exception("could not publish the command menu")


async def activate(app: Application) -> None:
    """Make the bot visible and usable: profile, command menus, admin sync, and a one-time 'what's new' for admins."""
    admins = await _admin_ids()
    await db.set_admins_exactly(admins)               # DB flags always mirror .env ADMIN_IDS exactly
    await _set_profile(app.bot)
    for tg in admins:
        if await db.get_setting(f"ui_notified_{tg}") == UI_VERSION:
            continue
        if await common.send(app.bot, tg, WHATS_NEW, ui.main_menu(True)):
            await db.set_setting(f"ui_notified_{tg}", UI_VERSION)
        else:
            log.warning("could not notify admin %s (has not pressed Start?)", tg)


async def _heartbeat(app: Application, every: float = 300.0) -> None:
    """A line in the log every 5 minutes proves the bot is alive (a silent log = a hung process), and the admins are told
    if bookings are waiting while nothing is working on them."""
    stuck = 0
    while True:
        await asyncio.sleep(every)
        waiting, active = worker.queue_size(), worker.active_count()
        left = max(0, int(Pace.cooldown_until - time.monotonic()))
        log.info("heartbeat: queue=%d active=%d cooldown=%ds", waiting, active, left)
        stuck = stuck + 1 if (waiting > 0 and active == 0) else 0
        if stuck == 2:
            await _alert_admins(app, "Bookings are waiting but nothing has worked on them for about 10 minutes. "
                                     "Open Admin panel -> Health; if it does not clear up, restart the service.")


async def _health_watch(app: Application, every: int = 120) -> None:
    """Every 2 minutes: is the booking browser fine? If not, let the doctor repair it first; only bother the admins
    with what could not be repaired (and say when it is back)."""
    state: Optional[bool] = None
    misses = 0
    while True:
        await asyncio.sleep(every)
        try:
            h = await asyncio.wait_for(automation.health(), 10)
        except Exception:  # noqa: BLE001
            h = {"ok": False}
        if h.get("ok"):
            misses = 0
            if state is False:
                await _alert_admins(app, "🟢 The booking browser is back online. Queued bookings continue.")
            state = True
            continue
        misses += 1
        if misses < 2:                                   # one bad check is not a problem yet
            continue
        fix = await automation.heal("the health check failed twice")
        if fix.get("ok"):
            misses = 0
            if fix.get("actions"):
                await _alert_admins(app, f"🩹 Fixed automatically: {doctor.describe(fix)}")
            if state is False:
                await _alert_admins(app, "🟢 The booking browser is back online. Queued bookings continue.")
            state = True
        elif fix.get("ok") is False and state is not False:
            await _alert_admins(app, f"🔴 The booking browser is offline and could not be repaired automatically. "
                                     f"{doctor.describe(fix)} New bookings wait in the queue and retry by themselves. "
                                     "On the server: log in and run tools\\start_trusted_edge.ps1.")
            state = False


COOLDOWN_KEY = "pace_until"


async def restore_cooldown(app: Optional[Application] = None) -> float:
    """IndiGo's edge told us to slow down? Then a restart (deploy, crash, reboot) must not make us forget it: the
    cool-down is saved when it starts and re-applied here. Returns the seconds still to wait. With `app`, the admins
    are told once when a new cool-down starts."""
    saved = await db.get_setting(COOLDOWN_KEY)
    left = (float(saved) - time.time()) if saved else 0.0
    strikes = int(await db.get_setting(COOLDOWN_KEY + "_strikes") or 0)
    if saved and time.time() - float(saved) < 3 * 3600:          # a recent block series: keep counting; an old one is forgotten
        Pace.strikes = max(Pace.strikes, strikes)
    if left > 0:
        Pace.resume(left)
        log.warning("IndiGo cool-down restored after the restart: %d s left (block #%d in a row)", int(left), Pace.strikes)
    loop = asyncio.get_running_loop()

    def started(seconds: float) -> None:
        loop.create_task(db.set_setting(COOLDOWN_KEY, str(time.time() + seconds)))
        loop.create_task(db.set_setting(COOLDOWN_KEY + "_strikes", str(Pace.strikes)))
        if app is not None:
            again = f" (block #{Pace.strikes} in a row - each one doubles the pause)" if Pace.strikes > 1 else ""
            loop.create_task(_alert_admins(app, f"🟡 IndiGo asked us to slow down{again}. New bookings pause for about "
                                                f"{int(seconds // 60)} minutes and then continue by themselves — nothing "
                                                "is lost and customers are told to wait."))
    Pace.on_trip = started
    return max(0.0, left)


def make_notifier(app: Application):
    """How the worker talks to people: results get 'Book another / My bookings' buttons."""
    async def notify(chat_id: int, text: str, kind: str = "info") -> None:
        markup = ui.after_booking_markup() if kind in ("success", "failure") else None
        await app.bot.send_message(chat_id, text, parse_mode=common.H, disable_web_page_preview=True, reply_markup=markup)
    return notify


async def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set (put it in .env)")
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS is empty - nobody can manage users/credits yet.")
    await db.init_db()
    app = build_app()
    await restore_cooldown(app)
    async with app:
        await app.start()
        await worker.start(make_notifier(app), lambda t: _alert_admins(app, t))
        await activate(app)
        check = await db.integrity()                  # SQLite's own self-check of the credit records
        if check != "ok":
            log.error("DATABASE SELF-CHECK FAILED: %s", check)
            await _alert_admins(app, "The database failed its self-check. Do not add credits until this is looked at - "
                                     "the latest backup is in the server's backups folder (or ask for /backup).")
        watchdog = asyncio.create_task(_health_watch(app))
        pulse = asyncio.create_task(_heartbeat(app))
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("bot running. admins=%s", config.ADMIN_IDS)
        try:
            await asyncio.Event().wait()
        finally:                                   # graceful, correctly ordered shutdown
            watchdog.cancel()
            pulse.cancel()
            await worker.stop()
            try:
                await app.updater.stop()
                await app.stop()
            except Exception:  # noqa: BLE001
                log.exception("error while stopping")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:           # NSSM stops the service with Ctrl-C: a normal stop, not a crash
        log.info("stopped")
