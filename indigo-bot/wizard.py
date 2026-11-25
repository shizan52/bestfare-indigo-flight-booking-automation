"""The booking wizard: ONE 'card' message that is edited in place, with a button wherever a button can replace typing.

Kept as short and plain as possible - one question per screen, no jargon, no codes:
  Trip (from, to, date) -> Passengers (how many, full name, Male/Female) -> Contact (mobile, email) -> Offer -> Confirm

* Typed answers always work as well (city names, dates like "25 Dec"), and the person's message is deleted afterwards, so
  the chat stays tidy and no names or phone numbers linger in it.
* ⬅️ Back and 👍 Keep let anybody fix an earlier answer; on the last screen ✏️ Change jumps straight to what is wrong.
* Every button carries the session id, so a button on an old card can never change the current booking.
* All state lives in memory (ctx.user_data): nothing personal is stored until the person confirms.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import Update
from telegram.ext import ContextTypes

import airports
import common
import config
import db
import ui
import worker
from indigo_client import Pace

log = logging.getLogger("bot")

KEY = "wz"
TTL = 30 * 60                       # an unfinished booking is dropped after 30 minutes
HORIZON = 330                       # IndiGo sells about this many days ahead
NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z.'-]{0,28}")
EMAIL_RE = re.compile(r"[a-z0-9._%+-]{1,64}@[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63}){1,3}")
EXAMPLE_FIRST = ("PRIYA", "AMIT", "NEHA", "ROHAN", "ANITA", "VIKAS")
STAGE = {"from": 1, "to": 1, "date": 1, "pax": 2, "name": 2, "g": 2, "phone": 3, "email": 3, "offer": 4, "review": 5}
GROUP = {"from": "route", "to": "route", "date": "date", "pax": "pax", "name": "pax", "g": "pax", "phone": "contact",
         "email": "contact", "offer": "offer", "review": "review"}


def _today() -> date:
    """'Today' in India - flights are sold on IST dates."""
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


# ── session ───────────────────────────────────────────────────────────────
def new_session(cap: int = 1) -> dict:
    """cap = the most passengers this person can afford (credits, at most MAX_PAX); with 1 credit the 'how many?'
    question is skipped."""
    t = _today()
    cap = max(1, min(config.MAX_PAX, cap))
    return {"sid": uuid.uuid4().hex[:5], "mid": None, "ts": time.time(), "i": 0, "cap": cap, "origin": None, "dest": None,
            "date": None, "n": 1 if cap == 1 else None, "pax": [], "email": None, "phone": None, "offer": None,
            "cal": (t.year, t.month), "cal_open": False, "alt": None, "edit": None, "menu": False}


def steps(wz: dict) -> list[tuple[str, int]]:
    s: list[tuple[str, int]] = [("from", 0), ("to", 0), ("date", 0)]
    if wz["cap"] > 1:
        s.append(("pax", 0))
    for i in range(wz.get("n") or 0):
        s += [("name", i), ("g", i)]
    return s + [("phone", 0), ("email", 0), ("offer", 0), ("review", 0)]


def cur(wz: dict) -> tuple[str, int]:
    s = steps(wz)
    wz["i"] = max(0, min(wz["i"], len(s) - 1))
    return s[wz["i"]]


def _p(wz: dict, idx: int) -> dict:
    while len(wz["pax"]) <= idx:
        wz["pax"].append({})
    return wz["pax"][idx]


def _full(p: dict) -> str:
    return f"{p.get('fn', '')} {p.get('ln', '')}".strip()


def _gender_word(p: dict) -> str:
    return {"M": "Male", "F": "Female"}.get(p.get("g", ""), "")


def value_of(wz: dict, step: str, idx: int) -> Optional[str]:
    """What the person already answered for this step (shown on the 👍 Keep button)."""
    if step == "from":
        return airports.short(wz["origin"]) if wz["origin"] else None
    if step == "to":
        return airports.short(wz["dest"]) if wz["dest"] else None
    if step == "date":
        return ui.d_short(ui.from_store(wz["date"])) if wz["date"] else None
    if step == "pax":
        return str(wz["n"]) if wz["n"] else None
    if step == "name":
        p = _p(wz, idx)
        return _full(p) if p.get("fn") and p.get("ln") else None
    if step == "g":
        return _gender_word(_p(wz, idx)) or None
    if step in ("email", "phone"):
        return wz[step]
    if step == "offer":
        return ui.offer_tag(wz["offer"]) if wz["offer"] else None
    return None


# ── what each screen looks like ───────────────────────────────────────────
def _context(wz: dict) -> list[str]:
    """What has been entered so far, shown above the question."""
    lines = []
    if wz["origin"] or wz["dest"]:
        parts = []
        if wz["origin"]:
            parts.append(f"🛫 {ui.esc(airports.short(wz['origin']))}")
        if wz["dest"]:
            parts.append(f"🛬 {ui.esc(airports.short(wz['dest']))}")
        lines.append("  →  ".join(parts))
    if wz["date"]:
        lines.append(f"📅 {ui.d_long(ui.from_store(wz['date']))}")
    for p in wz["pax"][:wz["n"] or 0]:
        if p.get("fn") and p.get("ln") and p.get("g"):          # only finished passengers (the question names the current one)
            lines.append(f"👤 {ui.esc(_full(p))} · {_gender_word(p)}")
    if wz["phone"]:
        lines.append(f"📞 {ui.esc(wz['phone'])}")
    if wz["email"]:
        lines.append(f"✉️ {ui.esc(wz['email'])}")
    return lines


def _who(wz: dict, idx: int) -> str:
    return "Passenger" if (wz["n"] or 1) == 1 else f"Passenger {idx + 1} of {wz['n']}"


def _step_body(wz: dict, step: str, idx: int, credits: int) -> tuple[str, list[list[Btn]]]:
    sid = wz["sid"]
    alt = wz.get("alt")
    if step in ("from", "to") and alt and alt[0] == ("f" if step == "from" else "t"):
        return ("<b>Which one do you mean?</b>", ui.choice_buttons(sid, alt[0], alt[1]))
    if step == "from":
        return ("<b>Where are you flying from?</b>\nTap your city — or just type its name.",
                ui.airport_buttons(sid, "f", wz["dest"]))
    if step == "to":
        return ("<b>Where are you flying to?</b>\nTap your city — or just type its name.",
                ui.airport_buttons(sid, "t", wz["origin"]))
    if step == "date":
        if wz.get("cal_open"):
            y, m = wz["cal"]
            return ("<b>Pick a day</b>\nOr type a date like <code>25 Dec</code>.",
                    ui.calendar_buttons(sid, y, m, _today(), HORIZON))
        return ("<b>When do you want to fly?</b>\nTap a day — or type a date like <code>25 Dec</code>.",
                ui.quick_date_buttons(sid, _today(), HORIZON))
    if step == "pax":
        top = max(1, min(config.MAX_PAX, credits))
        return (f"<b>How many people are travelling?</b>\n1 credit per person — you have <b>{credits}</b>.",
                ui.number_buttons(sid, "n", top))
    if step == "name":
        prev_last = _p(wz, idx - 1).get("ln") if idx > 0 else None
        example = f"{EXAMPLE_FIRST[idx % len(EXAMPLE_FIRST)]} {prev_last}" if prev_last else "RAHUL SHARMA"
        return (f"<b>{_who(wz, idx)} — full name</b>\nType it exactly as on their ID, like <code>{ui.esc(example)}</code>.", [])
    if step == "g":
        return (f"<b>{ui.esc(_full(_p(wz, idx)))} — Male or Female?</b>",
                [[Btn("👨 Male", callback_data=ui.cb(sid, "g", "M")), Btn("👩 Female", callback_data=ui.cb(sid, "g", "F"))]])
    if step == "phone":
        return ("<b>Your mobile number</b>\n10 digits, like <code>9876543210</code>.", [])
    if step == "email":
        return ("<b>Your email address</b>\nIndiGo sends the booking details here. For example <code>name@gmail.com</code>.", [])
    if step == "offer":
        n = wz["n"] or 1
        need = f" — needs {ui.credit_word(2 * n)}" if credits < 2 * n else ""
        body = ("<b>Which offer do you want?</b>\n"
                "🎖 <b>Offer 1</b> — Defence / NCC\n"
                "👴 <b>Offer 2</b> — Senior Citizen (60 or over)\n"
                "🔀 <b>Both</b> — two tickets, one with each offer")
        return (body, [[Btn("🎖 Offer 1 · Defence / NCC", callback_data=ui.cb(sid, "o", "offer1"))],
                       [Btn("👴 Offer 2 · Senior Citizen", callback_data=ui.cb(sid, "o", "offer2"))],
                       [Btn(f"🔀 Both offers{need}", callback_data=ui.cb(sid, "o", "both"))]])
    return ("", [])


def review_text(wz: dict, credits: int) -> str:
    offer = wz["offer"]
    n = wz["n"]
    cost = n * (2 if offer == "both" else 1)
    lines = [ui.step_header(5).replace("Book a flight", "Check your booking"), "",
             f"🛫 {ui.esc(airports.short(wz['origin']))}  →  🛬 {ui.esc(airports.short(wz['dest']))}",
             f"📅 {ui.d_long(ui.from_store(wz['date']))}", ""]
    lines += [f"👤 {ui.esc(_full(p))} · {_gender_word(p)}" for p in wz["pax"][:n]]
    lines += ["", f"📞 {ui.esc(wz['phone'])}   ✉️ {ui.esc(wz['email'])}", f"🎁 {ui.esc(ui.offer_name(offer))}",
              f"💳 This uses <b>{ui.credit_word(cost)}</b> — you will have {ui.credit_word(max(0, credits - cost))} left"]
    if offer in ("offer2", "both"):
        lines += ["", "ℹ️ The Senior Citizen fare is for passengers aged 60 or over."]
    if offer in ("offer1", "both"):
        lines += ["", "ℹ️ The Promotional fare A may need a valid ID at the airport."]
    lines += ["", "<i>By tapping Book now, you confirm that every passenger is eligible for this offer.</i>"]
    return "\n".join(lines)


def _change_menu(sid: str) -> list[list[Btn]]:
    return [[Btn("🛫 Cities", callback_data=ui.cb(sid, "ed", "route")), Btn("📅 Date", callback_data=ui.cb(sid, "ed", "date"))],
            [Btn("👥 Passengers", callback_data=ui.cb(sid, "ed", "pax")), Btn("📞 Contact", callback_data=ui.cb(sid, "ed", "contact"))],
            [Btn("🎁 Offer", callback_data=ui.cb(sid, "ed", "offer"))],
            [Btn("↩️ Back to review", callback_data=ui.cb(sid, "rv"))]]


def view(wz: dict, rec: Optional[dict], error: str = "") -> tuple[str, Markup]:
    """The card for the current step: (html text, inline keyboard)."""
    step, idx = cur(wz)
    credits = rec["credits"] if rec else 0
    sid = wz["sid"]
    if step == "review":
        if wz.get("menu"):
            return "✏️ <b>What would you like to change?</b>", ui.kb(_change_menu(sid))
        rows = [[Btn("✅ Book now", callback_data=ui.cb(sid, "ok"))],
                [Btn("✏️ Change", callback_data=ui.cb(sid, "ch")), Btn("❌ Cancel", callback_data=ui.cb(sid, "cx"))]]
        text = review_text(wz, credits)
        return (f"⚠️ {error}\n\n{text}" if error else text), ui.kb(rows)
    body, rows = _step_body(wz, step, idx, credits)
    editing = bool(wz.get("edit"))
    keep = None if (wz.get("alt") and step in ("from", "to")) else value_of(wz, step, idx)
    parts = [ui.step_header(STAGE[step]), ""]
    ctx_lines = _context(wz)
    if ctx_lines:
        parts += [*ctx_lines, ""]
    if error:
        parts += [f"⚠️ {error}", ""]
    parts.append(body)
    return "\n".join(parts), ui.kb([*rows, *ui.nav_buttons(sid, back=wz["i"] > 0, keep=keep, edit=editing)])


# ── applying answers (each returns an error text, or None when accepted) ──
def _set_airport(wz: dict, kind: str, code: str) -> Optional[str]:
    if not re.fullmatch(r"[A-Z]{3}", code):
        return "That is not a valid city."
    if kind == "f":
        if code == wz["dest"]:
            return "That is already your destination — pick a different city to fly from."
        wz["origin"] = code
    else:
        if code == wz["origin"]:
            return "You are already flying from there — pick a different city to fly to."
        wz["dest"] = code
    wz["alt"] = None
    return None


def _airport_text(wz: dict, kind: str, text: str) -> Optional[str]:
    hits = airports.resolve(text)
    if len(hits) == 1:
        return _set_airport(wz, kind, hits[0])
    if hits:
        wz["alt"] = (kind, hits)
        return None
    raw = text.strip()
    if re.fullmatch(r"[A-Za-z]{3}", raw):                    # looks like a code we do not know: ask before using it
        wz["alt"] = (kind, [raw.upper()])
        return None
    return "I could not find that city. Try another spelling, or tap one of the cities below."


def _apply_date(wz: dict, d: Optional[date]) -> Optional[str]:
    today = _today()
    if d is None:
        return "That does not look like a date. Try 25 Dec or 25-12-2026."
    if d < today:
        return "That day has already passed — pick today or a later day."
    if (d - today).days > HORIZON:
        return f"Flights open about {HORIZON} days ahead — please pick an earlier day."
    wz["date"] = ui.d_store(d)
    wz["cal"] = (d.year, d.month)
    return None


def _apply_pax(wz: dict, n: Optional[int], credits: int) -> Optional[str]:
    if n is None or not 1 <= n <= config.MAX_PAX:
        return f"Choose a number from 1 to {config.MAX_PAX}."
    if credits < n:
        return f"You have {ui.credit_word(credits)}, but {ui.pax_word(n)} need {n}."
    if wz["n"] != n:
        wz["n"] = n
        wz["pax"] = wz["pax"][:n]
    return None


def _apply_name(wz: dict, idx: int, text: str) -> Optional[str]:
    parts = text.split()
    if len(parts) < 2:
        return "Please type the first and last name together, like RAHUL SHARMA."
    if len(" ".join(parts)) > 40 or any(not NAME_TOKEN.fullmatch(x) for x in parts):
        return "Please use English letters only."
    p = _p(wz, idx)
    p["fn"], p["ln"] = " ".join(parts[:-1]).upper(), parts[-1].upper()
    return None


def _apply_gender(wz: dict, idx: int, raw: str) -> Optional[str]:
    g = {"m": "M", "male": "M", "f": "F", "female": "F"}.get(raw.strip().lower())
    if not g:
        return "Please tap Male or Female."
    _p(wz, idx)["g"] = g
    return None


def _apply_email(wz: dict, text: str) -> Optional[str]:
    t = text.strip().lower()
    if not EMAIL_RE.fullmatch(t):
        return "That email address does not look right. Example: name@gmail.com"
    wz["email"] = t
    return None


def _apply_phone(wz: dict, text: str) -> Optional[str]:
    digits = re.sub(r"\D", "", text)
    digits = digits[-10:] if len(digits) > 10 and digits.startswith(("91", "0")) else digits
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        return "Please type a 10-digit mobile number (it starts with 6, 7, 8 or 9)."
    wz["phone"] = digits
    return None


def _apply_offer(wz: dict, offer: str, credits: int) -> Optional[str]:
    offer = {"1": "offer1", "offer 1": "offer1", "2": "offer2", "offer 2": "offer2"}.get(offer.strip().lower(), offer.strip().lower())
    if offer not in ("offer1", "offer2", "both"):
        return "Please tap one of the offers."
    need = (wz["n"] or 1) * (2 if offer == "both" else 1)
    if credits < need:
        return f"That choice needs {ui.credit_word(need)} and you have {credits}."
    wz["offer"] = offer
    return None


# ── driving the wizard ────────────────────────────────────────────────────
def _advance(wz: dict) -> None:
    """Next question - or straight back to the review when somebody is changing one thing."""
    s = steps(wz)
    wz["i"] += 1
    wz["alt"] = None
    wz["cal_open"] = False
    edit = wz.get("edit")
    if edit and GROUP[s[min(wz["i"], len(s) - 1)][0]] != edit:
        wz["i"], wz["edit"] = len(s) - 1, None


def _first_step_of(wz: dict, group: str) -> int:
    s = steps(wz)
    wanted = {"route": "from", "date": "date", "pax": "pax" if wz["cap"] > 1 else "name", "contact": "phone", "offer": "offer"}[group]
    return next(i for i, (name, _idx) in enumerate(s) if name == wanted)


async def _refresh(ctx: ContextTypes.DEFAULT_TYPE, uid: int, wz: dict, error: str = "") -> None:
    rec = await db.get_user(uid)
    text, markup = view(wz, rec, error)
    wz["mid"] = await common.show(ctx, uid, wz["mid"], text, markup)


async def _close(ctx: ContextTypes.DEFAULT_TYPE, uid: int, wz: dict, text: str, markup=None) -> None:
    ctx.user_data.pop(KEY, None)
    if wz.get("mid"):
        await common.show(ctx, uid, wz["mid"], text, markup)
    else:
        await common.send(ctx.bot, uid, text, markup)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point: /book, the menu button or a 'Book' button."""
    rec = await common.registered(update, ctx)
    if not rec:
        return
    uid = rec["telegram_id"]
    old = ctx.user_data.pop(KEY, None)
    if old and old.get("mid"):                                 # retire the previous card
        await common.edit(ctx.bot, uid, old["mid"], "🗂 A new booking was started below — this one is closed.")
    if await db.get_setting("bookings_paused") == "1":
        await common.send(ctx.bot, uid, "⏸ <b>Bookings are paused for a short while.</b>\nPlease try again a little later — "
                                        "nothing is lost.")
        return
    if Pace.cooldown_left() > 0:                               # IndiGo is refusing us right now: say so before they type anything
        await common.send(ctx.bot, uid, ui.busy_text(int(Pace.cooldown_left() // 60) + 1))
        return
    if rec["credits"] < 1:
        await common.send(ctx.bot, uid, "💳 <b>You have no credits yet.</b>\nEach passenger uses 1 credit. Ask your admin to "
                                        "add some, then tap <b>✈️ Book a flight</b> again.")
        return
    if await db.open_jobs_for_user(uid) >= config.MAX_OPEN_JOBS_PER_USER:
        await common.send(ctx.bot, uid, "⏳ <b>Your other bookings are still being made.</b>\nPlease wait until they finish, "
                                        "then start a new one.")
        return
    wz = new_session(rec["credits"])
    ctx.user_data[KEY] = wz
    await _refresh(ctx, uid, wz)


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Drop the booking in progress (the /cancel command). Returns False when there was nothing to cancel."""
    wz = ctx.user_data.get(KEY)
    if not wz:
        return False
    await _close(ctx, update.effective_user.id, wz, "❌ <b>Booking cancelled.</b>\nTap <b>✈️ Book a flight</b> whenever you are ready.")
    return True


def _expired(wz: dict) -> bool:
    return time.time() - wz["ts"] > TTL


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """A typed message while a booking is open. True when the wizard consumed it."""
    wz = ctx.user_data.get(KEY)
    if not wz:
        return False
    uid = update.effective_user.id
    msg = update.effective_message
    if _expired(wz):
        await _close(ctx, uid, wz, "⌛ <b>This booking was left open for too long.</b>\nTap <b>✈️ Book a flight</b> to start again.")
        return False
    wz["ts"] = time.time()
    text = (msg.text or "").strip()
    step, idx = cur(wz)
    rec = await db.get_user(uid)
    credits = rec["credits"] if rec else 0
    wz["alt"] = None
    err: Optional[str]
    if step in ("from", "to"):
        err = _airport_text(wz, "f" if step == "from" else "t", text)
    elif step == "date":
        err = _apply_date(wz, ui.parse_date(text, _today()))
    elif step == "pax":
        err = _apply_pax(wz, int(text) if re.fullmatch(r"\d{1,2}", text) else None, credits)
    elif step == "name":
        err = _apply_name(wz, idx, text)
    elif step == "g":
        err = _apply_gender(wz, idx, text)
    elif step == "email":
        err = _apply_email(wz, text)
    elif step == "phone":
        err = _apply_phone(wz, text)
    elif step == "offer":
        err = _apply_offer(wz, text, credits)
    else:
        err = "Tap ✅ Book now to book, or ✏️ Change to fix something."
    if not err and not wz["alt"] and step != "review":
        _advance(wz)
    await common.delete(ctx.bot, uid, msg.message_id)
    await _refresh(ctx, uid, wz, err or "")
    return True


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    toast: list = [None, False]                                 # [text, show as alert]
    try:
        await _dispatch(update, ctx, toast)
    except Exception:                                           # noqa: BLE001 - never leave a spinning button
        log.exception("wizard callback failed")
        toast[:] = ["Something went wrong — please try again.", True]
    finally:
        await common.answer(q, toast[0], toast[1])


async def _dispatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE, toast: list) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":", 3)                        # w : sid : cmd : arg
    sid, cmd, arg = (parts + ["", "", ""])[1:4]
    uid = update.effective_user.id
    wz = ctx.user_data.get(KEY)
    if not wz or wz["sid"] != sid or _expired(wz):
        toast[:] = ["This screen is out of date. Tap ✈️ Book a flight to start again.", True]
        return
    if cmd == "nop":
        return
    wz["ts"] = time.time()
    rec = await db.get_user(uid)
    if not rec or not rec["is_active"]:
        toast[:] = ["Your account is not active.", True]
        return
    credits = rec["credits"]
    step, idx = cur(wz)
    err: Optional[str] = None
    advance = True
    wz["alt"] = None
    if cmd != "ch":
        wz["menu"] = False

    if cmd == "cx":
        await _close(ctx, uid, wz, "❌ <b>Booking cancelled.</b>\nTap <b>✈️ Book a flight</b> whenever you are ready.")
        return
    if cmd == "bk":
        wz["i"] = max(0, wz["i"] - 1)
        await _refresh(ctx, uid, wz)
        return
    if cmd == "rv":                                             # leave 'change' mode: back to the review
        wz["edit"], wz["i"] = None, len(steps(wz)) - 1
        await _refresh(ctx, uid, wz)
        return
    if cmd == "ch" and step == "review":
        wz["menu"] = True
        await _refresh(ctx, uid, wz)
        return
    if cmd == "ed" and arg in ("route", "date", "pax", "contact", "offer"):
        wz["edit"], wz["i"] = arg, _first_step_of(wz, arg)
        await _refresh(ctx, uid, wz)
        return
    if cmd == "kp":
        if value_of(wz, step, idx) is None:
            toast[:] = ["Nothing to keep yet.", False]
            return
        _advance(wz)
        await _refresh(ctx, uid, wz)
        return
    if cmd == "ok":
        await _confirm(update, ctx, wz, rec, toast)
        return
    if cmd in ("cal", "qd") and step == "date":
        wz["cal_open"] = cmd == "cal"
        await _refresh(ctx, uid, wz)
        return
    if cmd == "cm":
        m = re.fullmatch(r"(\d{4})-(\d{2})", arg)
        if m:
            wz["cal"] = (int(m.group(1)), int(m.group(2)))
        wz["cal_open"] = True
        await _refresh(ctx, uid, wz)
        return

    if cmd in ("f", "t") and step == ("from" if cmd == "f" else "to"):
        err = _set_airport(wz, cmd, arg)
    elif cmd == "d" and step == "date":
        try:
            err = _apply_date(wz, date.fromisoformat(arg))
        except ValueError:
            err = "That day is not valid."
    elif cmd == "n" and step == "pax":
        err = _apply_pax(wz, int(arg) if arg.isdigit() else None, credits)
    elif cmd == "g" and step == "g":
        err = _apply_gender(wz, idx, arg)
    elif cmd == "o" and step == "offer":
        err = _apply_offer(wz, arg, credits)
        if err:
            toast[:] = [err, True]
            err = None
            advance = False
    else:
        toast[:] = ["That button belongs to an earlier screen.", False]
        return
    if not err and advance:
        _advance(wz)
    await _refresh(ctx, uid, wz, err or "")


async def _confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, wz: dict, rec: dict, toast: list) -> None:
    uid = rec["telegram_id"]
    if cur(wz)[0] != "review" or wz.get("offer") not in ("offer1", "offer2", "both"):
        toast[:] = ["Please finish the questions first.", True]
        return
    if await db.get_setting("bookings_paused") == "1":
        toast[:] = ["Bookings are paused for a short while. Please try again later.", True]
        return
    if Pace.cooldown_left() > 0:
        toast[:] = [f"The airline's website is busy — please try again in about {int(Pace.cooldown_left() // 60) + 1} min. "
                    "You were not charged.", True]
        return
    offers = ["offer1", "offer2"] if wz["offer"] == "both" else [wz["offer"]]
    n = wz["n"]
    if await db.open_jobs_for_user(uid) + len(offers) > config.MAX_OPEN_JOBS_PER_USER + 1:
        await _close(ctx, uid, wz, "⏳ <b>Your other bookings are still being made.</b>\nPlease try again when they finish.")
        return
    if worker.queue_size() >= config.MAX_QUEUE:
        await _close(ctx, uid, wz, "⚠️ <b>We are very busy right now.</b>\nPlease try again in a few minutes — no credits were used.")
        return
    rows = [{"job_id": uuid.uuid4().hex[:10].upper(), "offer": o, "origin": wz["origin"], "destination": wz["dest"],
             "travel_date": wz["date"], "passengers": [{"first_name": p["fn"], "last_name": p["ln"], "gender": p["g"],
                                                         "dob": ""} for p in wz["pax"][:n]],
             "contact_email": wz["email"], "contact_phone": wz["phone"], "credit_cost": n, "declared_eligible": True}
            for o in offers]
    ids = await db.create_booking_charged(user_tg_id=uid, bookings=rows, total_cost=n * len(offers))
    if ids is None:                                              # atomic: nothing was charged or created
        await _close(ctx, uid, wz, "❌ <b>Not enough credits.</b>\nAsk your admin to top you up, then try again.")
        return
    ahead = worker.jobs_ahead()
    for jid in ids:
        await worker.enqueue(jid)
    left = await db.get_user(uid)
    text = "\n".join([
        "✅ <b>Request received!</b>", "",
        f"🛫 {ui.esc(airports.short(wz['origin']))} → {ui.esc(airports.short(wz['dest']))}  ·  📅 {ui.d_long(ui.from_store(wz['date']))}",
        f"👥 {ui.pax_word(n)}  ·  🎁 {ui.esc(ui.offer_name(wz['offer']))}", "",
        f"⏱ You are <b>#{ahead + 1}</b> in line — your PNR will arrive here in {ui.eta_text(ahead + len(ids))}.",
        "You can close Telegram; I will message you.", "",
        f"💳 Credits left: <b>{left['credits'] if left else '—'}</b>"])
    await _close(ctx, uid, wz, text, ui.after_booking_markup())
