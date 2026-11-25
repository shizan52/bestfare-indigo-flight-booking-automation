"""Presentation layer: every text, button and keyboard the bot shows. Pure functions - no Telegram I/O - so the whole
look-and-feel can be unit-tested. English only. Brand: Best Fare.

Rule for everything a customer sees: plain words, one idea per screen, no codes or jargon. The coupon codes are never shown
to customers (only "Offer 1" / "Offer 2"); admins see them.
"""
from __future__ import annotations

import calendar
import html
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import ReplyKeyboardMarkup

import airports
import config

BRAND = "Best Fare"
esc = html.escape

# ── main menu (persistent keyboard at the bottom of the chat) ─────────────
MENU_BOOK, MENU_CREDITS = "✈️ Book a flight", "💳 My credits"
MENU_BOOKINGS, MENU_HELP = "📋 My bookings", "❓ Help"
MENU_ADMIN, MENU_STATUS = "🛠 Admin panel", "🔄 Check my status"
MENU_TEXTS = {MENU_BOOK, MENU_CREDITS, MENU_BOOKINGS, MENU_HELP, MENU_ADMIN, MENU_STATUS}


def main_menu(admin: bool = False) -> ReplyKeyboardMarkup:
    """One big button for the one thing people come to do, the rest underneath."""
    rows = [[MENU_BOOK], [MENU_BOOKINGS, MENU_CREDITS], [MENU_HELP, MENU_ADMIN] if admin else [MENU_HELP]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True, input_field_placeholder="Tap a button below")


def pending_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[MENU_STATUS], [MENU_HELP]], resize_keyboard=True, is_persistent=True)


# ── formatting ────────────────────────────────────────────────────────────
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


def money(x: float) -> str:
    return f"₹{x:,.0f}"


def d_long(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day:02d} {MONTHS[d.month - 1][:3]} {d.year}"


def d_short(d: date) -> str:
    return f"{d.day:02d} {MONTHS[d.month - 1][:3]}"


def d_store(d: date) -> str:
    """The DD-MM-YYYY form the booking engine and database use."""
    return d.strftime("%d-%m-%Y")


def from_store(s: str) -> date:
    return datetime.strptime(s, "%d-%m-%Y").date()


_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %m %Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y")


def parse_date(text: str, today: date, *, relative: bool = True, no_year: bool = True) -> Optional[date]:
    """25-12-2026, 25/12/2026, 25 Dec 2026, 25 Dec, tomorrow, +7 ... -> date (None when it is not a date)."""
    t = " ".join((text or "").strip().replace(",", " ").split())
    if not t:
        return None
    low = t.lower()
    if relative:
        if low == "today":
            return today
        if low in ("tomorrow", "tmrw"):
            return today + timedelta(days=1)
        m = re.fullmatch(r"\+(\d{1,3})(?:\s*d(?:ays?)?)?", low)
        if m:
            return today + timedelta(days=int(m.group(1)))
    for f in _FORMATS:
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            pass
    if no_year:
        m = re.fullmatch(r"(\d{1,2})[ /.-]([A-Za-z]{3,9}|\d{1,2})", t)
        if m:
            for f in ("%d %m %Y", "%d %b %Y", "%d %B %Y"):
                try:
                    d = datetime.strptime(f"{m.group(1)} {m.group(2)} {today.year}", f).date()
                except ValueError:
                    continue
                if d < today:
                    try:
                        d = d.replace(year=d.year + 1)
                    except ValueError:
                        return None
                return d
    return None


STEP_NAMES = {1: "Trip", 2: "Passengers", 3: "Contact", 4: "Offer", 5: "Confirm"}


def step_header(stage: int) -> str:
    return f"✈️ <b>Book a flight</b>  ·  Step {stage} of {len(STEP_NAMES)} — {STEP_NAMES[stage]}"


def eta_text(jobs: int) -> str:
    """Rough waiting time: one booking takes about 100 seconds."""
    mins = max(1, round(max(1, jobs) * 100 / 60))
    return f"about {mins} min"


def hold_until_text(iso: Optional[str]) -> str:
    """'2026-11-17T11:09:06Z' -> '17 Nov 16:39 BDT / 16:09 IST' (the hold deadline in the customers' local times)."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return ""
    bd, ist = t + timedelta(hours=6), t + timedelta(hours=5, minutes=30)
    return f"{bd:%d} {MONTHS[bd.month - 1][:3]} {bd:%H:%M} BDT / {ist:%H:%M} IST"


def hold_expired(iso: Optional[str]) -> Optional[bool]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except ValueError:
        return None


def route(origin: str, dest: str, *, short: bool = False) -> str:
    f = airports.short if short else airports.label
    return f"{f(origin)} → {f(dest)}"


def offer_short(offer: str) -> str:
    """'Offer 1 · 6ETST001' - with the coupon code. For admins and logs only, never for customers."""
    if offer == "both":
        return "Both offers"
    n = "1" if offer == "offer1" else "2"
    return f"Offer {n} · {config.COUPONS.get(offer, '')}"


def offer_long(offer: str) -> str:
    return {"offer1": f"Offer 1 — Defence / NCC ({config.COUPONS['offer1']})",
            "offer2": f"Offer 2 — Senior Citizen ({config.COUPONS['offer2']})",
            "both": "Both — Offer 1 + Offer 2 (2 separate bookings)"}.get(offer, offer)


def offer_name(offer: str) -> str:
    """What a CUSTOMER sees: plain names, no codes."""
    return {"offer1": "Offer 1 — Defence / NCC", "offer2": "Offer 2 — Senior Citizen",
            "both": "Both offers — 2 tickets"}.get(offer, offer)


def offer_tag(offer: str) -> str:
    return {"offer1": "Offer 1", "offer2": "Offer 2", "both": "Both offers"}.get(offer, offer)


def pax_word(n: int) -> str:
    return f"{n} passenger" + ("" if n == 1 else "s")


def credit_word(n: int) -> str:
    return f"{n} credit" + ("" if n == 1 else "s")


STATUS_ICON = {"done": "✅", "pending": "⏳", "running": "🔄", "failed": "❌"}


def ledger_label(reason: str) -> str:
    r = (reason or "").lower()
    if r.startswith("booking"):
        return "Booking"
    if r.startswith("refund"):
        return "Refund"
    if "welcome" in r or "initial" in r:
        return "Welcome credits"
    if "admin" in r:
        return "Adjusted by admin"
    return reason or "—"


def stamp(ts: Optional[str]) -> str:
    """'2026-11-17 14:20:21' (UTC) -> '17 Nov 20:20' (Bangladesh time, what the team uses)."""
    if not ts:
        return "—"
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=6)
    except ValueError:
        return ts
    return f"{t.day:02d} {MONTHS[t.month - 1][:3]} {t:%H:%M}"


def person(u: dict) -> str:
    """'Rahul Sharma (@rahul)' - whatever we know about a user."""
    name = (u.get("full_name") or "").strip()
    un = u.get("username")
    if name and un:
        return f"{esc(name)} (@{esc(un)})"
    if name:
        return esc(name)
    if un:
        return f"@{esc(un)}"
    return f"User {u.get('telegram_id') or u.get('id') or ''}".strip()


# ── generic keyboards ─────────────────────────────────────────────────────
def kb(rows: list[list[Btn]]) -> Markup:
    return Markup(rows)


def cb(sid: str, cmd: str, arg: str = "") -> str:
    """Wizard callback data: w:<session>:<command>[:<argument>]   (always < 64 bytes)."""
    return f"w:{sid}:{cmd}" + (f":{arg}" if arg != "" else "")


def airport_buttons(sid: str, kind: str, exclude: Optional[str] = None) -> list[list[Btn]]:
    """Twelve big city buttons (just the city name - nobody needs to know airport codes)."""
    codes = [c for c in airports.POPULAR if c != exclude]
    for extra in airports.SPARE:                                   # keep the grid full when one is excluded
        if len(codes) >= 12:
            break
        if extra != exclude and extra not in codes:
            codes.append(extra)
    codes = codes[:12]
    return [[Btn(airports.short(c), callback_data=cb(sid, kind, c)) for c in codes[i:i + 3]]
            for i in range(0, len(codes), 3)]


def choice_buttons(sid: str, kind: str, codes: list[str]) -> list[list[Btn]]:
    rows = []
    for c in codes[:6]:
        text = airports.label(c) if airports.known(c) else f"Yes, use {c}"
        rows.append([Btn(text, callback_data=cb(sid, kind, c))])
    return rows


def quick_date_buttons(sid: str, today: date, horizon: int) -> list[list[Btn]]:
    """Today, tomorrow and the next six days as one-tap buttons, plus a way to open the calendar."""
    days = [today + timedelta(days=i) for i in range(8) if i <= horizon]

    def label(i: int, d: date) -> str:
        return f"Today · {d_short(d)}" if i == 0 else f"Tomorrow · {d_short(d)}" if i == 1 else f"{WEEKDAYS[d.weekday()]} {d_short(d)}"
    btns = [Btn(label(i, d), callback_data=cb(sid, "d", d.isoformat())) for i, d in enumerate(days)]
    rows = [btns[:2]] + [btns[i:i + 3] for i in range(2, len(btns), 3)]
    rows.append([Btn("📅 Pick another date", callback_data=cb(sid, "cal"))])
    return rows


def calendar_buttons(sid: str, year: int, month: int, today: date, horizon: int) -> list[list[Btn]]:
    last_ok = today + timedelta(days=horizon)
    blank = "⠀"
    nop = cb(sid, "nop")
    prev_ok = (year, month) > (today.year, today.month)
    next_ok = (year, month) < (last_ok.year, last_ok.month)
    py, pm = (year, month - 1) if month > 1 else (year - 1, 12)
    ny, nm = (year, month + 1) if month < 12 else (year + 1, 1)
    rows = [[Btn("◀" if prev_ok else blank, callback_data=cb(sid, "cm", f"{py}-{pm:02d}") if prev_ok else nop),
             Btn(f"{MONTHS[month - 1]} {year}", callback_data=nop),
             Btn("▶" if next_ok else blank, callback_data=cb(sid, "cm", f"{ny}-{nm:02d}") if next_ok else nop)],
            [Btn(w[:2], callback_data=nop) for w in WEEKDAYS]]
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        row = []
        for d in week:
            if d == 0:
                row.append(Btn(blank, callback_data=nop))
                continue
            dt = date(year, month, d)
            if today <= dt <= last_ok:
                row.append(Btn(f"[{d}]" if dt == today else str(d), callback_data=cb(sid, "d", dt.isoformat())))
            else:
                row.append(Btn("·", callback_data=nop))
        rows.append(row)
    rows.append([Btn("⬅️ Quick dates", callback_data=cb(sid, "qd"))])
    return rows


def number_buttons(sid: str, cmd: str, top: int) -> list[list[Btn]]:
    return [[Btn(str(i), callback_data=cb(sid, cmd, str(i))) for i in range(1, top + 1)]]


def nav_buttons(sid: str, *, back: bool = True, keep: Optional[str] = None, edit: bool = False) -> list[list[Btn]]:
    """👍 Keep (when there is already an answer), then Back (or 'Back to review' while changing something) and Cancel."""
    rows = []
    if keep:
        rows.append([Btn(f"👍 Keep: {keep[:26]}", callback_data=cb(sid, "kp"))])
    row = []
    if edit:
        row.append(Btn("↩️ Back to review", callback_data=cb(sid, "rv")))
    elif back:
        row.append(Btn("⬅️ Back", callback_data=cb(sid, "bk")))
    row.append(Btn("❌ Cancel", callback_data=cb(sid, "cx")))
    rows.append(row)
    return rows


# ── screens ───────────────────────────────────────────────────────────────
def welcome_text(first_name: str, credits: int, admin: bool) -> str:
    lines = [f"👋 <b>Hi {esc(first_name or 'there')}!</b> Welcome to {BRAND}.", "",
             "Book IndiGo flights at the lowest fare — it takes about a minute.",
             f"💳 You have <b>{credit_word(credits)}</b>   <i>(1 credit = 1 passenger)</i>" if credits > 0 else
             "💳 You have no credits yet — ask your admin to add some.", ""]
    lines.append("Tap <b>✈️ Book a flight</b> below to start." if credits > 0 else
                 "Once you have credits, tap <b>✈️ Book a flight</b>.")
    if admin:
        lines += ["", "🛠 You are an admin — the <b>Admin panel</b> button is your control centre."]
    return "\n".join(lines)


def pending_text(user_id: int) -> str:
    return (f"👋 <b>Welcome to {BRAND}!</b>\n\n"
            "🔒 <b>Your account is waiting for approval.</b>\n"
            "I have told the admins — you will get a message here as soon as you are in.\n\n"
            f"🆔 Your Telegram ID: <code>{user_id}</code>  <i>(an admin may ask for it)</i>")


def deactivated_text() -> str:
    return ("🔒 <b>Your account is switched off right now.</b>\n"
            "Please ask an admin — they can switch it back on in a moment.")


def busy_text(minutes: int) -> str:
    return ("⏳ <b>The airline's website is busy right now.</b>\n"
            f"Please try again in about <b>{max(1, minutes)} minute{'s' if minutes > 1 else ''}</b> — nothing was charged and "
            "your credits are safe.")


def home_markup() -> Markup:
    return kb([[Btn("✈️ Book a flight", callback_data="m:book")],
               [Btn("📋 My bookings", callback_data="m:bookings"), Btn("💳 My credits", callback_data="m:credits")]])


def after_booking_markup() -> Markup:
    return kb([[Btn("📋 My bookings", callback_data="m:bookings"), Btn("✈️ Book another", callback_data="m:book")]])


HELP_TEXT = ("❓ <b>How Best Fare works</b>\n\n"
             "1️⃣ Tap <b>✈️ Book a flight</b>\n"
             "2️⃣ Choose where, when and who is travelling\n"
             "3️⃣ Pick your offer and tap <b>Book now</b>\n"
             "4️⃣ Your <b>PNR</b> (booking code) arrives here in about 2 minutes\n\n"
             "💳 Each passenger uses 1 credit. If a booking fails, your credits come back automatically.\n"
             "💡 To pay: goindigo.in → <b>Manage Booking</b> → enter your PNR → <b>Pay Now</b> (within 30 minutes).\n"
             "🙋 Need credits or help? Ask your admin.")


def help_markup() -> Markup:
    return kb([[Btn("✈️ Book a flight", callback_data="m:book")]])


def credits_text(rec: dict, ledger: list[dict]) -> str:
    lines = ["💳 <b>Your credits</b>", "", f"You have <b>{credit_word(rec['credits'])}</b>",
             "<i>Each passenger uses 1 credit.</i>", ""]
    if ledger:
        lines.append("<b>Recent activity</b>")
        for r in ledger[:5]:
            lines.append(f"• {stamp(r['created_at'])} · <b>{r['delta']:+d}</b> · {esc(ledger_label(r['reason']))}")
    if rec["credits"] < 1:
        lines += ["", "Out of credits? Ask your admin to top you up."]
    return "\n".join(lines)


def book_markup() -> Markup:
    return kb([[Btn("✈️ Book a flight", callback_data="m:book")]])


def _nice_date(stored: str) -> str:
    try:
        return d_long(from_store(stored))
    except ValueError:
        return stored


def _short_stored(stored: str) -> str:
    try:
        return d_short(from_store(stored))
    except ValueError:
        return stored


def booking_card(b: dict) -> str:
    icon = STATUS_ICON.get(b["status"], "❔")
    head = f"{icon} <b>{esc(airports.short(b['origin']))} → {esc(airports.short(b['destination']))}</b> · {esc(_nice_date(b['travel_date']))}"
    meta = f"👥 {pax_word(b['pax_count'])} · 🎁 {esc(offer_tag(b['offer']))}"
    if b["status"] == "done":
        label = "PNR" if b.get("pnr_kind") == "record_locator" else "Reference"
        line = f"🔖 {label}: <code>{esc(b.get('pnr') or '—')}</code>"
        expired = hold_expired(b.get("hold_until"))
        if b.get("pnr_kind") == "record_locator" and b.get("hold_until"):
            line += ("\n⌛ The hold has ended — book again if you still need it." if expired else
                     f"\n⏳ Pay before <b>{hold_until_text(b['hold_until'])}</b>")
        return f"{head}\n{meta}\n{line}"
    if b["status"] == "failed":
        return f"{head}\n{meta}\n💳 This one could not be booked — your credits are back."
    if b["status"] == "running":
        return f"{head}\n{meta}\n🔄 Booking it now…"
    return f"{head}\n{meta}\n⏳ Waiting for its turn — you can cancel it below for a full refund."


def bookings_text(rows: list[dict]) -> str:
    if not rows:
        return ("📋 <b>My bookings</b>\n\nNothing here yet.\nTap <b>✈️ Book a flight</b> to make your first booking.\n\n"
                "<i>Booking details are removed automatically 2 hours after delivery.</i>")
    return ("📋 <b>My bookings</b>\n\n" + "\n\n".join(booking_card(b) for b in rows) +
            "\n\n<i>Booking details are removed automatically 2 hours after delivery.</i>")


def bookings_markup(rows: tuple | list = ()) -> Markup:
    """Refresh / Book, plus a Cancel button for every booking that has not started yet."""
    cancel = [[Btn(f"❌ Cancel {airports.short(b['origin'])} → {airports.short(b['destination'])} · {_short_stored(b['travel_date'])}",
                   callback_data=f"m:cx:{b['job_id']}")] for b in rows if b["status"] == "pending"]
    return kb(cancel + [[Btn("🔄 Refresh", callback_data="m:refresh"), Btn("✈️ Book a flight", callback_data="m:book")]])


def request_alert_text(user) -> str:
    name = user.full_name if hasattr(user, "full_name") else ""
    un = getattr(user, "username", None)
    who = person({"full_name": name, "username": un, "telegram_id": user.id})
    return (f"🔔 <b>New access request</b>\n👤 {who}\n🆔 <code>{user.id}</code>\n\n"
            "Approve with starting credits:")


def request_alert_markup(tg: int) -> Markup:
    return kb([[Btn(f"✅ +{n}", callback_data=f"a:ok:{tg}:{n}") for n in (0, 3, 5, 10)],
               [Btn("🚫 Dismiss", callback_data=f"a:no:{tg}"), Btn("🕓 All requests", callback_data="a:ap:0")]])
