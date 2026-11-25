"""The Best Fare window: a sidebar (New booking · My bookings · Activity) and three pages.

Nothing on screen tells how the work is done: only what the traveller needs - the trip, the best fare, the offer, the PNR and how
long it lasts. (Debug mode, used by the developer, adds the technical story.)"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from PyQt6.QtCore import QEvent, QEventLoop, QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
                             QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QScrollArea, QSizePolicy, QStackedWidget,
                             QTabBar, QTableWidget, QTableWidgetItem, QTextEdit, QToolButton, QVBoxLayout, QWidget)

import engine as eng
import forms
import stations
from choice_panel import ChoicePanel
from store import Store
from theme import C
from widgets import (Card, DateField, ErrorLabel, OfferTile, StationBox, StepList, Toast, TravellerRow, WrapRow, button, divider, field,
                     label, repolish, set_error, shadow, shadows_for)

nl = chr(10)
DEFAULT_SIZE = (1320, 860)           # how big the window opens when the screen has room for it
MIN_SIZE = (820, 500)                # the smallest it may be made (or less if the screen itself is smaller): the form and the right-hand panel still fit side by side
EDGE = (8, 32, 8, 8)                 # what the window frame takes around the client area: left, top (title bar), right, bottom
SLIM_BELOW = 1000                    # a window narrower than this shows the menu as a slim rail of icons
HOLD_MINUTES = 30
SLOW_AFTER_S = 150                   # after this long the progress panel says it is taking longer than usual
HEARTBEAT_S = 600                    # the server is asked about credits and messages only now and then; WHO MAY BOOK is checked once, at the moment of booking (see _verified)
QUIET_CHECK_S = 300                  # during a booking the connection is left alone, unless the server has been silent this long
ACTIVE_EVERY_S = 10.0                # how often the window tells the engine "the traveller is working on the form"
INDIGO_HOME = "https://www.goindigo.in/"
activity_log = logging.getLogger("bestfare.activity")          # the customer-facing story, also kept in the log file


# ── time helpers ─────────────────────────────────────────────────────────────────────────────────────
def deadline(rec: dict) -> Optional[datetime]:
    """When the 30-minute hold ends (aware UTC)."""
    h = rec.get("hold_until")
    if h:
        try:
            d = datetime.fromisoformat(str(h).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if rec.get("status") == "done" and rec.get("kind", "record_locator") == "record_locator" and rec.get("finished_at"):
        return datetime.fromtimestamp(rec["finished_at"], tz=timezone.utc) + timedelta(minutes=HOLD_MINUTES)
    return None


def clock(d: datetime) -> str:
    return d.astimezone().strftime("%I:%M %p").lstrip("0")


def left_seconds(d: datetime, now: Optional[datetime] = None) -> int:
    return int((d - (now or datetime.now(timezone.utc))).total_seconds())


def mmss(secs: int) -> str:
    return f"{secs // 60}:{secs % 60:02d}"


def left_text(d: datetime) -> tuple[str, int]:
    secs = left_seconds(d)
    if secs <= 0:
        return "", 0
    return mmss(secs), secs


def hold_state(rec: dict) -> tuple[str, str, str, Optional[datetime]]:
    """(state, status text, time-left text, deadline) of a booking: waiting | held | expired | failed | order."""
    if rec["status"] in ("waiting", "running"):
        return "waiting", "Booking now…", "", None
    if rec["status"] == "failed":
        return "failed", "Not booked", "", None
    if (rec.get("kind") or "record_locator") != "record_locator":
        return "order", "Waiting for payment", "", None
    d = deadline(rec)
    if d is None:
        return "held", "On hold", "", None
    secs = left_seconds(d)
    if secs > 0:
        return "held", "On hold", mmss(secs), d
    return "expired", "Expired", "Expired", d


def credit_cell(rec: dict) -> tuple[str, str]:
    """(what the 'Credits' column says, its tooltip) for one booking: what was taken, and whether it came back."""
    used = rec.get("credits_used")
    if not used:
        return "—", "No credits were taken for this booking."
    s = "s" if used != 1 else ""
    if rec.get("status") in ("waiting", "running"):
        return f"{used} used", f"{used} credit{s} taken when the booking started."
    if rec.get("status") == "done":
        return f"{used} used", f"{used} credit{s} used for this booking."
    back = rec.get("credits_back")
    if back is None:
        return f"{used} used…", "The result is being reported to the server: the credits come back automatically when a booking fails."
    if back:
        return "returned ✓", f"{used} credit{s} were taken and given back because the booking did not go through."
    return f"{used} used", f"{used} credit{s} were taken and not returned automatically. Your admin has been told and will look at it."


def pax_cell(rec: dict) -> str:
    """The history table's 'Travellers' cell: 2, or '2 + 1 infant' when a baby rides along."""
    a, c, i = forms.record_mix(rec.get("travellers") or [])
    n = a + c if (a + c) else int(rec.get("pax_count") or 1)
    return str(n) + (f" + {i} infant{'s' if i != 1 else ''}" if i else "")


def travel_day(rec: dict) -> str:
    try:
        return forms.day_long(datetime.strptime(rec["travel_date"], "%Y-%m-%d").date())
    except (ValueError, KeyError):
        return rec.get("travel_date", "")


class Bridge(QObject):
    """Engine thread -> window thread (Qt queues the signal across threads)."""
    fired = pyqtSignal(dict)          # (not 'event': that name is QObject.event(), Qt's own dispatcher)
    account = pyqtSignal()            # the licence changed (credits, blocked, ...): may come from any thread
    checked = pyqtSignal()            # a check with the server has ended


# ── right-hand panels ────────────────────────────────────────────────────────────────────────────────
class SummaryPanel(QFrame):
    ROOMY, SNUG = (24, 22, 24, 22), (16, 18, 16, 18)           # the panel's inner padding: as designed, and a little less when the window is narrow

    def __init__(self):
        super().__init__()
        self.setObjectName("panel")
        shadow(self, 30, 26, 8)
        v = QVBoxLayout(self)
        self.lay = v
        v.setContentsMargins(*self.ROOMY)
        v.setSpacing(10)
        v.addWidget(label("YOUR TRIP", "kicker"))
        self.route = label("—  ✈  —", "route")
        self.cities = label("Choose where you fly from and to", "routecities", wrap=True)
        v.addWidget(self.route)
        v.addWidget(self.cities)
        v.addWidget(divider())
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.vals: dict[str, QLabel] = {}
        for r, (key, cap) in enumerate((("date", "📅  Date"), ("pax", "👥  Travellers"), ("offer", "🎁  Offer"),
                                         ("fare", "💰  Fare"))):
            grid.addWidget(label(cap, "small"), r, 0)
            val = label("—", "")
            val.setStyleSheet("font-weight: 600;")
            val.setWordWrap(True)
            grid.addWidget(val, r, 1)
            self.vals[key] = val
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        v.addSpacing(6)
        self.credit_line = label("", "small", wrap=True)
        self.credit_line.setStyleSheet("font-weight: 600;")
        self.credit_line.hide()
        v.addWidget(self.credit_line)
        self.notice = QFrame()
        self.notice.setObjectName("notebox")
        nl = QVBoxLayout(self.notice)
        nl.setContentsMargins(12, 10, 12, 10)
        self.notice_text = label("", "small", wrap=True)
        nl.addWidget(self.notice_text)
        self.notice.hide()
        v.addWidget(self.notice)
        self.warn = QFrame()
        self.warn.setObjectName("warnbox")
        wl = QVBoxLayout(self.warn)
        wl.setContentsMargins(12, 10, 12, 10)
        self.warn_text = label("", "small", wrap=True)
        self.warn_text.setStyleSheet(f"color: {C['warn']}; font-weight: 600;")
        wl.addWidget(self.warn_text)
        self.warn.hide()
        v.addWidget(self.warn)
        self.book = button("Book now", "primary")
        self.book.setMinimumHeight(52)
        v.addWidget(self.book)
        v.addWidget(label("Usually about 1–2 minutes. Nothing is paid here: you get a PNR, and IndiGo holds it for 30 minutes "
                          "so you can pay.", "muted", wrap=True))
        v.addStretch(1)

    def set_snug(self, snug: bool) -> None:
        self.lay.setContentsMargins(*(self.SNUG if snug else self.ROOMY))

    def update_from(self, o: forms.Order, fare_hint: str = "") -> None:
        if o.origin or o.dest:
            self.route.setText(f"{o.origin or '—'}  ✈  {o.dest or '—'}")
            self.cities.setText(f"{stations.city(o.origin) if o.origin else 'From?'}  →  {stations.city(o.dest) if o.dest else 'To?'}")
        else:
            self.route.setText("—  ✈  —")
            self.cities.setText("Choose where you fly from and to")
        self.vals["date"].setText(forms.day_long(o.day) if o.day else "—")
        self.vals["pax"].setText(forms.mix_text(*o.counts()))
        self.vals["offer"].setText(forms.OFFER_TITLE[o.offer] if o.offer else "—")
        self.vals["fare"].setText(fare_hint or "—")

    def set_warning(self, text: str) -> None:
        self.warn_text.setText(text)
        self.warn.setVisible(bool(text))

    def set_notice(self, text: str) -> None:
        """A message from the admin to everybody (shown calmly, above the Book button)."""
        self.notice_text.setText(text)
        self.notice.setVisible(bool(text))

    def set_credit_line(self, text: str, bad: bool = False) -> None:
        self.credit_line.setText(text)
        self.credit_line.setStyleSheet(f"font-weight: 600; color: {C['bad'] if bad else C['text2']};")
        self.credit_line.setVisible(bool(text))


class ProgressPanel(QFrame):
    stop_clicked = pyqtSignal(str)
    details_clicked = pyqtSignal()
    ROOMY, SNUG = (24, 22, 24, 22), (16, 18, 16, 18)

    def __init__(self):
        super().__init__()
        self.setObjectName("panel")
        shadow(self, 30, 26, 8)
        v = QVBoxLayout(self)
        self.lay = v
        v.setContentsMargins(*self.ROOMY)
        v.setSpacing(12)
        self.kicker = label("BOOKING", "kicker")
        v.addWidget(self.kicker)
        self.title = label("", "route")
        self.title.setStyleSheet("font-size: 15pt;")
        self.sub = label("", "routecities", wrap=True)
        v.addWidget(self.title)
        v.addWidget(self.sub)
        v.addWidget(divider())
        self.steps = StepList([(k, eng.STAGE_TEXT[k]) for k in eng.STAGES])
        v.addWidget(self.steps)
        self.notes = label("", "small", wrap=True)
        self.notes.setStyleSheet(f"color: {C['ok']}; font-weight: 600;")
        v.addWidget(self.notes)
        self.elapsed = label("", "elapsed", wrap=True)
        v.addWidget(self.elapsed)
        det = button("Show every step ›", "ghost", "The Activity page lists each step")
        det.clicked.connect(self.details_clicked.emit)
        v.addWidget(det, 0, Qt.AlignmentFlag.AlignLeft)
        box = QFrame()
        box.setObjectName("notebox")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(12, 10, 12, 10)
        bl.addWidget(label("Please keep this window open until your booking is ready.", "small", wrap=True))
        v.addWidget(box)
        self.stop = button("Stop this booking", "danger")
        self.stop.clicked.connect(lambda: self.stop_clicked.emit(self.bid or ""))
        v.addWidget(self.stop)
        v.addStretch(1)
        self.bid: Optional[str] = None
        self.t0 = 0.0
        self.waiting = False
        self._notes: list[str] = []

    def set_snug(self, snug: bool) -> None:
        self.lay.setContentsMargins(*(self.SNUG if snug else self.ROOMY))

    def begin(self, bid: str, offer: str, o_route: str, day: str, waiting: bool = False) -> None:
        self.bid = bid
        self.t0 = time.monotonic()
        self.waiting = waiting
        self.title.setText(forms.OFFER_TITLE[offer])
        self.sub.setText(f"{o_route}  ·  {day}")
        self.steps.reset()
        if not waiting:
            self.steps.set_active("browser")
        self._notes = []
        self.notes.setText("")
        self.stop.setEnabled(True)
        if waiting:
            self.note("Another booking is running — this one starts by itself right after it.")
        self.tick()

    def started(self) -> None:
        """The booking before it is done: this one has its turn now."""
        if self.waiting:
            self.waiting = False
            self.t0 = time.monotonic()
            self._notes = []
            self.notes.setText("")
            self.steps.set_active("browser")

    def note(self, text: str) -> None:
        if text and text not in self._notes:
            self._notes.append(text)
            self.notes.setText("\n".join(f"• {n}" for n in self._notes[-4:]))

    def tick(self) -> None:
        if self.bid and self.waiting:
            self.elapsed.setText("⏳  Waiting for your other booking to finish…")
        elif self.bid:
            s = int(time.monotonic() - self.t0)
            if s > SLOW_AFTER_S:                                      # honest, calm, and nothing is lost: do not make the traveller guess
                self.elapsed.setText(f"⏱  {s // 60}:{s % 60:02d} so far — taking a little longer than usual. "
                                     "Please keep this window open; nothing is charged.")
            else:
                self.elapsed.setText(f"⏱  {s // 60}:{s % 60:02d} so far — usually 1 to 2 minutes")


class ResultCard(QFrame):
    def __init__(self, rec: dict, copy_cb, retry_cb, details_cb=None):
        super().__init__()
        ok = rec["status"] == "done"
        self.rec = rec
        self.setObjectName("okbox" if ok else "badbox")
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(6)
        v.addWidget(label(forms.OFFER_TITLE[rec['offer']], "kicker"))
        self.timer_lbl: Optional[QLabel] = None
        if ok:
            kind = rec.get("kind") or "record_locator"
            v.addWidget(label("PNR — your booking code" if kind == "record_locator" else "Booking reference (order id)", "small"))
            code = label(rec["pnr"] or "", "pnr")
            code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            cp = button("Copy", "copy")
            cp.clicked.connect(lambda: copy_cb(rec["pnr"]))
            v.addWidget(WrapRow(code, cp, gap=10, left=True))                 # (the Copy button goes under the code when the panel is narrow)
            if rec.get("flight"):
                v.addWidget(label(f"✈  {rec['flight']}", "small", wrap=True))
            if rec.get("fare"):
                v.addWidget(label(f"Fare {forms.money(rec['fare'])}", "small"))
            if rec.get("coupon_applied") is True and rec.get("discount"):
                v.addWidget(label(f"🎁 Offer applied — you save {forms.money(rec['discount'])}", "save", wrap=True))
            elif rec.get("coupon_applied") is True:
                v.addWidget(label("🎁 Offer applied", "save"))
            elif rec.get("coupon_applied") is False:
                w = label("⚠ IndiGo did not apply the offer to this flight — this is the regular fare. Check before paying.",
                          "small", wrap=True)
                w.setStyleSheet(f"color: {C['warn']}; font-weight: 600;")
                v.addWidget(w)
            if rec.get("verified") == "ok":
                v.addWidget(label("✓ Checked with IndiGo: names, flight, date and contact match" if rec.get("flight")
                                  else "✓ Checked with IndiGo: names, route, date and contact match", "save", wrap=True))
            elif rec.get("verified") == "mismatch":
                w = label("⚠ " + (rec.get("verify_note") or "Please check the details before you pay."), "small", wrap=True)
                w.setStyleSheet(f"color: {C['bad']}; font-weight: 700;")
                v.addWidget(w)
            self.timer_lbl = label("", "timer", wrap=True)
            v.addWidget(self.timer_lbl)
            how = button("How to pay", "secondary")
            how.clicked.connect(self._how)
            if rec.get("pay_url") and kind != "record_locator":
                other = button("Open payment page", "secondary")
                other.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(rec["pay_url"])))
            else:
                other = button("Open IndiGo", "secondary")
                other.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(INDIGO_HOME)))
            v.addWidget(WrapRow(how, other, gap=8, left=True))
            self.tick()
        else:
            v.addWidget(label("✕  Not booked", "bigbad"))
            v.addWidget(label(rec.get("error") or "The booking could not be completed.", "small", wrap=True))
            again = button("Try again", "secondary")
            again.clicked.connect(lambda: retry_cb(rec))
            v.addWidget(again, 0, Qt.AlignmentFlag.AlignLeft)
            if details_cb:
                why = button("What happened? ›", "ghost", "Open the Activity page")
                why.clicked.connect(details_cb)
                v.addWidget(why, 0, Qt.AlignmentFlag.AlignLeft)

    def tick(self) -> None:
        if not self.timer_lbl:
            return
        state, _status, left, d = hold_state(self.rec)
        if state == "held" and d is not None:
            secs = left_seconds(d)
            self.timer_lbl.setText(f"⏳ Held for you until {clock(d)}  —  {left} left. Pay before then.")
            repolish(self.timer_lbl, urgent=secs < 300, over=False)
        elif state == "expired" and d is not None:
            self.timer_lbl.setText(f"The hold ended at {clock(d)}. If you paid before then, the ticket is confirmed.")
            repolish(self.timer_lbl, over=True, urgent=False)
        else:
            self.timer_lbl.setText(f"⏳ Pay within {HOLD_MINUTES} minutes")

    def _how(self) -> None:
        r = self.rec
        if (r.get("kind") or "record_locator") == "record_locator":
            body = (f"<p><b>Your PNR: {r['pnr']}</b></p><ol>"
                    "<li>Open <b>goindigo.in</b> (or the IndiGo app).</li>"
                    "<li>Go to <b>Trips → Manage Booking</b>.</li>"
                    f"<li>Enter the PNR <b>{r['pnr']}</b> and the email address (or the surname) you booked with.</li>"
                    "<li>Press <b>Pay Now</b> and finish the payment.</li></ol>"
                    f"<p>IndiGo holds the booking for {HOLD_MINUTES} minutes. Pay before the time shown — an unpaid hold is "
                    "cancelled automatically and nothing is charged.</p>")
        else:
            body = (f"<p><b>Booking reference: {r['pnr']}</b></p><p>Use <b>Open payment page</b> to pay. "
                    "The payment page is valid for about 12 minutes; the fare is held for 30 minutes.</p>")
        QMessageBox.information(self, "How to pay", body)


class ResultPanel(QFrame):
    another = pyqtSignal()
    back_to_form = pyqtSignal()
    ROOMY, SNUG = (24, 22, 24, 22), (16, 18, 16, 18)

    def __init__(self, copy_cb, retry_cb, details_cb=None):
        super().__init__()
        self.setObjectName("panel")
        shadow(self, 30, 26, 8)
        self.copy_cb, self.retry_cb, self.details_cb = copy_cb, retry_cb, details_cb
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(0, 0, 0, 0)
        self.body: Optional[QWidget] = None
        self.v: QVBoxLayout = QVBoxLayout()
        self.cards: list[ResultCard] = []
        self.snug = False

    def set_snug(self, snug: bool) -> None:
        self.snug = snug
        if self.body is not None:
            self.v.setContentsMargins(*(self.SNUG if snug else self.ROOMY))

    def show_group(self, recs: list[dict]) -> None:
        # a brand-new container each time: nothing of the previous result can stay on screen
        if self.body is not None:
            self.body.setParent(None)
            self.body.deleteLater()
        self.body = QWidget()
        self.body.setObjectName("clear")
        self.outer.addWidget(self.body)
        self.v = QVBoxLayout(self.body)
        self.v.setContentsMargins(*(self.SNUG if self.snug else self.ROOMY))
        self.v.setSpacing(12)
        self.cards = []
        done = [r for r in recs if r["status"] == "done"]
        if done:
            self.v.addWidget(label("🎉  Your booking is ready!", "bigok", wrap=True))
        else:
            self.v.addWidget(label("We could not book this", "bigbad", wrap=True))
        r0 = recs[0]
        self.v.addWidget(label(f"{stations.label(r0['origin'])}  →  {stations.label(r0['dest'])}\n{travel_day(r0)}  ·  "
                               f"{forms.mix_text(*forms.record_mix(r0['travellers']))}", "routecities", wrap=True))
        for r in recs:
            c = ResultCard(r, self.copy_cb, self.retry_cb, self.details_cb)
            self.cards.append(c)
            self.v.addWidget(c)
        if done:
            self.v.addWidget(label("Your PNR is also kept under “My bookings”, with the time left to pay.", "muted", wrap=True))
        row = QVBoxLayout()                                   # stacked: the panel is narrow
        row.setSpacing(8)
        if not done:
            edit = button("Change details and try again", "primary")
            edit.clicked.connect(self.back_to_form.emit)
            row.addWidget(edit)
        again = button("Book another flight", "primary" if done else "secondary")
        again.clicked.connect(self.another.emit)
        row.addWidget(again)
        self.v.addLayout(row)
        self.v.addStretch(1)

    def tick(self) -> None:
        for c in self.cards:
            c.tick()


# ── page: new booking ────────────────────────────────────────────────────────────────────────────────
class BookPage(QWidget):
    book_requested = pyqtSignal(object)
    stop_requested = pyqtSignal(str)
    details_requested = pyqtSignal()
    fares_requested = pyqtSignal(str, str, object, int)       # from, to, first day, number of days
    flight_chosen = pyqtSignal(str, str)                      # booking id, the flight the traveller picked
    choice_cancelled = pyqtSignal(str)                        # booking id
    another_requested = pyqtSignal()                          # "Book another": a new tab with a clean form
    state_changed = pyqtSignal()                              # the tab's title may need to change

    def __init__(self, store: Store, copy_cb):
        super().__init__()
        self.setObjectName("page")
        self.store = store
        self.rows: list[TravellerRow] = []
        self.state = "draft"                                      # draft | waiting | running | choose | done | failed  (what this tab is doing)
        self.group: Optional[str] = None                          # the booking(s) this tab started
        self.group_ids: list[str] = []
        self.offers_allowed: Optional[list[str]] = None           # None = every offer (practice mode, development)
        self.toast_cb = lambda text: None                         # the window shows small confirmations ("Saved ...")
        self.result_pnr = ""
        self.credit_text = None                                   # set by the window when there is a licence: n travellers -> (words, too_few)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # left: the form
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("pagebody")
        self.form = QVBoxLayout(body)
        self.form.setContentsMargins(30, 26, 18, 30)
        self.form.setSpacing(16)
        self.tier = 0                                             # 0 = wide, 1 = medium, 2 = narrow: how much room the page has (see _adapt)
        self.form.addWidget(label("Book a flight", "h1"))
        self.form.addWidget(label("Fill in the four boxes and press Book now. Best Fare shows IndiGo's flights with the lowest fare of "
                                  "each — you pick one, and it applies your offer and gets you a PNR.", "sub", wrap=True))
        self._trip_card()
        self._travellers_card()
        self._contact_card()
        self._offer_card()
        self.form.addStretch(1)
        self.form_scroll.setWidget(body)
        self.cards = [self.form.itemAt(i).widget() for i in range(self.form.count()) if isinstance(self.form.itemAt(i).widget(), Card)]
        shadows_for(body, *self.cards)
        outer.addWidget(self.form_scroll, 1)

        # right: summary / progress / result
        right = QWidget()
        right.setObjectName("pagebody")
        right.setFixedWidth(self.RIGHT[0])
        self.right = right
        rl = QVBoxLayout(right)
        self.rl = rl
        rl.setContentsMargins(*self.SIDE[0])
        self.stack = QStackedWidget()
        self.stack.setObjectName("pagebody")                         # (the page's own background, also in the margin the soft shadows need)
        self.stack.setContentsMargins(*self.SHADE[0])                # (room for the panels' soft shadows: the panels sit where they always did)
        self.stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)   # the column keeps its width, whatever the widest hidden page would like
        self.summary = SummaryPanel()
        self.progress = ProgressPanel()
        self.result = ResultPanel(copy_cb, self._retry, self.details_requested.emit)
        self.choice = ChoicePanel()
        for w in (self.summary, self.progress, self.choice, self.result):
            self.stack.addWidget(w)
        shadows_for(self.stack, self.summary, self.progress, self.choice, self.result)
        rscroll = QScrollArea()
        rscroll.setWidgetResizable(True)
        rscroll.setFrameShape(QFrame.Shape.NoFrame)
        rscroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rscroll.setWidget(self.stack)
        rl.addWidget(rscroll)
        outer.addWidget(right)

        self.summary.book.clicked.connect(self._book_clicked)
        self.progress.stop_clicked.connect(self.stop_requested.emit)
        self.progress.details_clicked.connect(self.details_requested.emit)
        self.choice.chosen.connect(self.flight_chosen.emit)
        self.choice.cancelled.connect(self.choice_cancelled.emit)
        self.result.another.connect(self.another_requested.emit)
        self.result.back_to_form.connect(self.back_to_form)
        self.refresh_summary()
        self._adapt()

    # the room: a wide window is laid out as it always was; a narrow one gives up a little at a time ------------------------------------
    RIGHT = (370, 340, 320)                                       # the right column's width in each tier
    BREAK = (960, 780)                                            # the page is 'wide' from 960 pixels, 'medium' from 780, 'narrow' below

    def _tier_for(self, width: int) -> int:
        return 0 if width >= self.BREAK[0] else (1 if width >= self.BREAK[1] else 2)

    def resizeEvent(self, e) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(e)
        self._adapt()

    MARGINS = ((30, 26, 18, 30), (22, 20, 12, 24), (16, 16, 8, 20))     # the form's outer margins in each tier
    SIDE = ((0, 18, 10, 8), (0, 18, 8, 8), (0, 14, 4, 6))              # the right column's margins (outer)
    SHADE = ((8, 8, 15, 18), (10, 8, 12, 16), (8, 6, 8, 12))           # ... and the room the panels' soft shadows get inside it (tier 0: the panel sits where it always did)

    def _adapt(self) -> None:
        """A wide window is laid out exactly as designed; a narrow one gives up a little room at a time: a slimmer right column, smaller margins, tighter
        cards. (The rows inside the cards - the traveller's line, the date and its quick buttons - fit themselves to their own width.)"""
        tier = self._tier_for(self.width())
        if tier == self.tier:
            return
        self.tier = tier
        self.right.setFixedWidth(self.RIGHT[tier])
        self.form.setContentsMargins(*self.MARGINS[tier])
        self.form.setSpacing(16 if tier == 0 else 12)
        for c in self.cards:
            c.set_snug(tier == 2)
        self.rl.setContentsMargins(*self.SIDE[tier])
        self.stack.setContentsMargins(*self.SHADE[tier])
        for p in (self.summary, self.progress, self.choice, self.result):
            p.set_snug(tier == 2)

    # cards ------------------------------------------------------------------------------------------
    def _trip_card(self) -> None:
        card = Card(1, "Trip", "Type a city, an old name (Bombay, Vizag) or an airport code — pick from the list.")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.origin = StationBox("From — e.g. Delhi")
        self.dest = StationBox("To — e.g. Mumbai")
        self.e_origin, self.e_dest = ErrorLabel(), ErrorLabel()
        row.addWidget(field("FROM", self.origin, self.e_origin), 1)
        swap = button("⇄", "swap", "Swap From and To")
        swap.clicked.connect(self._swap)
        sw = QVBoxLayout()
        sw.addSpacing(20)
        sw.addWidget(swap)
        sw.addStretch(1)
        row.addLayout(sw)
        row.addWidget(field("TO", self.dest, self.e_dest), 1)
        card.lay.addLayout(row)

        t = forms.today_ist()
        self.date_field = DateField(t, forms.last_bookable(t), t + timedelta(days=1))
        self.e_day = ErrorLabel()
        quick = QWidget()                                          # the quick buttons: beside the date while there is room, under it when there is not
        quick.setObjectName("clear")
        ql = QVBoxLayout(quick)
        ql.setContentsMargins(0, 0, 0, 0)
        qrow = QHBoxLayout()
        qrow.setSpacing(6)
        for text, days in (("Today", 0), ("Tomorrow", 1), ("Next week", 7)):
            b = button(text, "chip")
            b.clicked.connect(lambda _c=False, d=days: self.date_field.set_date(forms.today_ist() + timedelta(days=d)))
            qrow.addWidget(b)
        ql.addLayout(qrow)
        ql.addStretch(1)
        card.lay.addWidget(WrapRow(field("TRAVEL DATE — tap to see the fare of every day", self.date_field, self.e_day), quick, gap=8, drop=22))

        # the form moves on by itself: From -> To -> the calendar -> the first traveller
        self.origin.chosen.connect(lambda _c: self.dest.setFocus())
        self.dest.chosen.connect(lambda _c: QTimer.singleShot(180, self.date_field.open_calendar))
        self.date_field.calendar.picked.connect(lambda _d: self.rows and self.rows[0].first.setFocus())
        self.date_field.calendar.need_fares.connect(self._need_fares)
        for w in (self.origin, self.dest):
            w.changed.connect(self._route_changed)
        self.date_field.changed.connect(self._day_changed)
        self.form.addWidget(card)

    def _travellers_card(self) -> None:
        card = Card(2, "Travellers", "Names exactly as on the photo ID. No surname? Type the first name again. "
                                     "A child (2 to 11 years) needs a date of birth; an infant (under 2 years) rides on an adult's lap and needs a name and date of birth.")
        self.count = label("", "muted")
        card.right.addWidget(self.count)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(10)
        card.lay.addLayout(self.rows_box)
        self.e_trav = ErrorLabel()
        card.lay.addWidget(self.e_trav)
        adds = QHBoxLayout()
        adds.setSpacing(8)
        self.add_btn = button("+  Adult", "ghost")
        self.add_child = button("+  Child", "ghost", "A child of 2 to 11 years: has a seat, needs a date of birth")
        self.add_infant = button("+  Infant", "ghost", "A baby under 2 years: rides on an adult's lap (one infant per adult), needs a name and date of birth")
        self.add_btn.clicked.connect(lambda: self.add_row())
        self.add_child.clicked.connect(lambda: self.add_row(kind=forms.CHILD))
        self.add_infant.clicked.connect(lambda: self.add_row(kind=forms.INFANT))
        for b in (self.add_btn, self.add_child, self.add_infant):
            adds.addWidget(b)
        adds.addStretch(1)
        card.lay.addLayout(adds)
        self.form.addWidget(card)
        self.add_row()

    def _contact_card(self) -> None:
        card = Card(3, "Contact", "IndiGo sends the booking details to this mobile number and email.")
        self.saved_btn = QToolButton()
        self.saved_btn.setObjectName("savedbtn")
        self.saved_btn.setText("👤  Saved contacts  ▾")
        self.saved_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.saved_btn.setToolTip("Use a contact you saved before, or save this one for later")
        self.saved_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.saved_menu = QMenu(self.saved_btn)
        self.saved_menu.aboutToShow.connect(self._fill_contacts_menu)
        self.saved_btn.setMenu(self.saved_menu)
        card.right.addWidget(self.saved_btn)
        row = QHBoxLayout()
        row.setSpacing(14)
        ph = QWidget()
        ph.setObjectName("clear")
        pl = QHBoxLayout(ph)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        pre = QLabel("+91")
        pre.setObjectName("prefix")
        self.phone = QLineEdit()
        self.phone.setObjectName("withprefix")
        self.phone.setPlaceholderText("10-digit mobile number")
        self.phone.setMaxLength(14)
        pl.addWidget(pre)
        pl.addWidget(self.phone, 1)
        self.e_phone = ErrorLabel()
        row.addWidget(field("MOBILE", ph, self.e_phone), 1)
        self.email = QLineEdit()
        self.email.setPlaceholderText("name@gmail.com")
        self.e_email = ErrorLabel()
        row.addWidget(field("EMAIL", self.email, self.e_email), 1)
        card.lay.addLayout(row)
        self.save_contact_btn = button("💾  Save this contact for later", "ghost", "Keep this mobile number and email in your list of saved contacts")
        self.save_contact_btn.clicked.connect(self._save_contact)
        card.lay.addWidget(self.save_contact_btn, 0, Qt.AlignmentFlag.AlignLeft)
        for e in (self.phone, self.email):
            e.textChanged.connect(lambda _t, w=e: set_error(w, False))
        self.form.addWidget(card)

    # saved contacts: any number of them, kept on this computer, usable in any booking ------------------------------------------
    def contacts(self) -> list[dict]:
        saved = self.store.setting("contacts")
        if saved is None:
            old = self.store.setting("contact")                      # (the single contact an earlier version remembered)
            saved = [{"id": uuid.uuid4().hex[:8], "name": "Saved contact", "phone": old.get("phone", ""), "email": old.get("email", "")}] if old else []
            self.store.set_setting("contacts", saved)
            self.store.set_setting("contact", None)
        return [c for c in saved if isinstance(c, dict) and c.get("phone") and c.get("email")]

    def _fill_contacts_menu(self) -> None:
        m = self.saved_menu
        m.clear()
        saved = self.contacts()
        if not saved:
            a = m.addAction("No saved contacts yet")
            a.setEnabled(False)
        for c in saved:
            m.addAction(f"{c['name']}  ·  {c['phone']}  ·  {c['email']}", lambda c=c: self.use_contact(c))
        m.addSeparator()
        m.addAction("💾  Save the contact typed below…", self._save_contact)
        if saved:
            rm = m.addMenu("🗑  Remove a saved contact")
            for c in saved:
                rm.addAction(f"{c['name']}  ·  {c['phone']}", lambda c=c: self.remove_contact(c))

    def use_contact(self, c: dict) -> None:
        self.phone.setText(c["phone"])
        self.email.setText(c["email"])
        set_error(self.phone, False)
        set_error(self.email, False)
        self.e_phone.show_text("")
        self.e_email.show_text("")
        self.refresh_summary()

    def _save_contact(self) -> None:
        phone, e1 = forms.clean_phone(self.phone.text())
        email, e2 = forms.clean_email(self.email.text())
        if e1 or e2:
            set_error(self.phone, bool(e1))
            set_error(self.email, bool(e2))
            self.e_phone.show_text(e1)
            self.e_email.show_text(e2)
            QMessageBox.information(self, "Save this contact", "Please type a correct mobile number and email first, then save them.")
            return
        saved = self.contacts()
        known = next((c for c in saved if c["phone"] == phone and c["email"] == email), None)
        if known:
            QMessageBox.information(self, "Save this contact", f"This contact is already saved as “{known['name']}”.")
            return
        name, ok = QInputDialog.getText(self, "Save this contact", "Give it a name, so you can find it later:", text=email.split("@")[0].replace(".", " ").title())
        name = " ".join((name or "").split())[:30]
        if not ok or not name:
            return
        saved.append({"id": uuid.uuid4().hex[:8], "name": name, "phone": phone, "email": email})
        self.store.set_setting("contacts", saved)
        self.toast_cb(f"Saved “{name}”")

    def remove_contact(self, c: dict) -> None:
        self.store.set_setting("contacts", [x for x in self.contacts() if x["id"] != c["id"]])
        self.toast_cb(f"Removed “{c['name']}”")

    def _offer_card(self) -> None:
        card = Card(4, "Offer", "Choose the offer the travellers are entitled to. One offer per booking.")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.offer_group = QButtonGroup(self)
        self.tiles: dict[str, OfferTile] = {}
        for key in forms.OFFER_ORDER:
            t = OfferTile(key)
            self.offer_group.addButton(t)
            self.tiles[key] = t
            row.addWidget(t, 1)
            t.toggled.connect(lambda _on: self._offer_changed())
        card.lay.addLayout(row)
        self.e_offer = ErrorLabel()
        card.lay.addWidget(self.e_offer)
        self.form.addWidget(card)

    # fares ------------------------------------------------------------------------------------------
    def route(self) -> tuple[str, str]:
        return self.origin._code or "", self.dest._code or ""            # noqa: SLF001 (no re-parse while typing)

    def _route_changed(self, _code: str = "") -> None:
        o, d = self.route()
        self.date_field.calendar.reset_fares(f"{o}-{d}" if o and d else "")
        self.refresh_summary()
        self.state_changed.emit()

    def _need_fares(self, start: date) -> None:
        o, d = self.route()
        if o and d:
            self.date_field.calendar.set_loading()
            self.fares_requested.emit(o, d, start, 60)

    def on_fares(self, ev: dict) -> None:
        o, d = self.route()
        if (ev.get("origin"), ev.get("dest")) != (o, d):
            return                                                       # the answer to a route that is no longer on screen
        self.date_field.calendar.set_fares(date.fromisoformat(ev["start"]), ev["days"], ev.get("fares") or {}, bool(ev.get("empty")),
                                           ev.get("error"))
        self.refresh_summary()

    def fare_hint(self) -> str:
        cal = self.date_field.calendar
        iso = self.date_field.date().isoformat()
        if cal.state == "ok" and iso in cal.fares:
            price, cat = cal.fares[iso]
            word = {"L": "low", "M": "medium", "H": "high"}.get(cat, "")
            return f"from ₹{price:,.0f} that day ({word})" if word else f"from ₹{price:,.0f} that day"
        return ""

    # behaviour --------------------------------------------------------------------------------------
    def _day_changed(self, day) -> None:
        for r in self.rows:
            r.set_travel_day(day)                                   # a child's or baby's age counts on the day of the flight
        self.refresh_summary()

    def _mix(self) -> tuple[int, int, int]:
        kinds = [r.kind for r in self.rows]
        return kinds.count(forms.ADULT), kinds.count(forms.CHILD), kinds.count(forms.INFANT)

    def add_row(self, t: Optional[forms.Traveller] = None, kind: str = forms.ADULT) -> None:
        a, c, i = self._mix()
        kind = t.kind if t else kind
        if kind == forms.INFANT:
            if i >= a or i >= forms.MAX_INFANTS:                   # one infant per adult, and at most 4
                return
        elif a + c >= forms.MAX_TRAVELLERS or (kind == forms.CHILD and c >= forms.MAX_CHILDREN):
            return
        r = TravellerRow(len(self.rows), kind)
        r.removed.connect(self._remove_row)
        r.edited.connect(self.refresh_summary)
        r.kind_changed.connect(self._rows_changed)
        r.resized.connect(self._rows_fit)
        r.measured.connect(self._rows_fit)
        r.set_travel_day(self.date_field.date())
        if t:
            r.set_value(t)
        self.rows.append(r)
        self.rows_box.addWidget(r)
        if len(self.rows) > 1:
            r.set_compact(self.rows[0].compact)                     # (the rows stand alike)
        self._rows_changed()
        if not t:
            r.first.setFocus()

    def _rows_fit(self) -> None:
        """The traveller rows all stand the same way - in one line when there is room, in two when there is not (never cut off); whichever needs the most room
        decides. A few pixels of hysteresis: a scrollbar appearing must not flip them back and forth."""
        known = [r for r in self.rows if r.is_measured]
        if not known:
            return
        width, need = known[0].width(), max(r.wide_need for r in known)
        compact = known[0].compact
        if not compact and width < need:
            compact = True
        elif compact and width >= need + TravellerRow.HYSTERESIS:
            compact = False
        for r in self.rows:
            r.set_compact(compact)

    def _remove_row(self, r: TravellerRow) -> None:
        if len(self.rows) <= 1:
            return
        self.rows.remove(r)
        r.setParent(None)
        r.deleteLater()
        for i, row in enumerate(self.rows):
            row.set_index(i)
        self._rows_changed()

    def _rows_changed(self) -> None:
        a, c, i = self._mix()
        self.count.setText(f"{a + c} of {forms.MAX_TRAVELLERS} seats" + (f"  ·  {i} infant{'s' if i != 1 else ''}" if i else ""))
        self.add_btn.setEnabled(a + c < forms.MAX_TRAVELLERS)
        self.add_child.setEnabled(a + c < forms.MAX_TRAVELLERS and c < forms.MAX_CHILDREN)
        self.add_infant.setEnabled(i < a and i < forms.MAX_INFANTS)
        for r in self.rows:
            r.rm.setVisible(len(self.rows) > 1)
        self.refresh_summary()

    def _swap(self) -> None:
        a, b = self.origin.code(), self.dest.code()
        self.origin.set_code(b)
        self.dest.set_code(a)
        self.refresh_summary()

    def _offer_changed(self) -> None:
        for t in self.tiles.values():
            set_error(t, False)
        self.e_offer.show_text("")
        self.refresh_summary()

    def order(self) -> forms.Order:
        offer = next((k for k, t in self.tiles.items() if t.isChecked()), "")
        return forms.Order(origin=self.origin.code() or "", dest=self.dest.code() or "", day=self.date_field.date(),
                           travellers=[r.value() for r in self.rows], phone=self.phone.text(), email=self.email.text(),
                           offer=offer)

    def refresh_summary(self) -> None:
        if not hasattr(self, "summary"):                 # called while the form is still being built
            return
        o = forms.Order(origin=self.origin._code or "", dest=self.dest._code or "", day=self.date_field.date(),   # noqa: SLF001
                        travellers=[r.value() for r in self.rows],
                        offer=next((k for k, t in self.tiles.items() if t.isChecked()), "") if hasattr(self, "tiles") else "")
        self.summary.update_from(o, self.fare_hint())
        if self.credit_text is not None:
            a, c, i = o.counts()
            text, bad = self.credit_text(a + c, i)
            if text:
                self.summary.set_credit_line(text, bad)

    def show_errors(self, errs: dict[str, str]) -> None:
        first_bad: Optional[QWidget] = None

        def mark(w: QWidget, el: Optional[ErrorLabel], key: str) -> None:
            nonlocal first_bad
            m = errs.get(key)
            set_error(w, bool(m))
            if el is not None:
                el.show_text(m)
            if m and first_bad is None:
                first_bad = w

        mark(self.origin, self.e_origin, "origin")
        mark(self.dest, self.e_dest, "dest")
        mark(self.date_field, self.e_day, "day")
        for i, r in enumerate(self.rows):
            bad = r.show_errors(errs, i)
            if bad and first_bad is None:
                first_bad = bad[0]
        self.e_trav.show_text(errs.get("travellers"))
        mark(self.phone, self.e_phone, "phone")
        mark(self.email, self.e_email, "email")
        offer_msg = errs.get("offer")
        for t in self.tiles.values():
            set_error(t, "offer" in errs)
        self.e_offer.show_text(offer_msg)
        if offer_msg and first_bad is None:
            first_bad = self.tiles["offer1"]
        if first_bad is not None:
            self.form_scroll.ensureWidgetVisible(first_bad, 40, 80)
            first_bad.setFocus()

    def _book_clicked(self) -> None:
        o = self.order()
        errs = forms.validate(o)
        self.show_errors(errs)
        if errs:
            return
        self._save(o)
        self.book_requested.emit(o)

    def _save(self, o: forms.Order) -> None:
        """Nothing is carried over to the next booking: a new tab is a clean form (saved contacts are there to pick from)."""

    def _retry(self, rec: dict) -> None:
        """'Try again': the same form; this booking's offer stays chosen."""
        self.back_to_form()
        if rec.get("offer") in forms.OFFER_ORDER:
            self.tiles[rec["offer"]].setChecked(True)
        self.refresh_summary()
        self._book_clicked()                         # shows what to fix if something is missing

    # modes ------------------------------------------------------------------------------------------
    def set_busy(self, busy: bool) -> None:
        w = self.form_scroll.widget()
        if w is not None:
            w.setEnabled(not busy)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
        self.state_changed.emit()

    def show_progress(self, waiting: bool = False) -> None:
        self.set_busy(True)
        self.stack.setCurrentWidget(self.progress)
        self._set_state("waiting" if waiting else "running")

    def show_choice(self, bid: str, options: list[dict], route: str, day: str, pax, seconds: float) -> None:
        """IndiGo's flights are in: the traveller picks one (the booking waits for it)."""
        self.set_busy(True)
        self.choice.show_flights(bid, options, route, day, pax, seconds)
        self.stack.setCurrentWidget(self.choice)
        self._set_state("choose")

    def show_result(self, recs: list[dict]) -> None:
        self.set_busy(False)
        self.result.show_group(recs)
        self.stack.setCurrentWidget(self.result)
        self.result_pnr = next((r.get("pnr") for r in recs if r and r.get("pnr")), "")
        self._set_state("done" if any(r and r["status"] == "done" for r in recs) else "failed")

    def back_to_form(self) -> None:
        self.set_busy(False)
        self.stack.setCurrentWidget(self.summary)
        self._set_state("draft")

    def new_booking(self) -> None:
        """A fresh form: the travellers, offer and date are cleared; a remembered contact and the last route stay."""
        for r in list(self.rows):
            r.setParent(None)
            r.deleteLater()
        self.rows = []
        self.add_row()
        self.date_field.set_date(forms.today_ist() + timedelta(days=1), emit=False)
        self.offer_group.setExclusive(False)
        for t in self.tiles.values():
            t.setChecked(False)
        self.offer_group.setExclusive(True)
        self.phone.clear()
        self.email.clear()
        self.origin.set_code("")
        self.dest.set_code("")
        self.group, self.group_ids = None, []
        self.show_errors({})
        self.state = "draft"
        self.state_changed.emit()
        self.back_to_form()
        self.form_scroll.verticalScrollBar().setValue(0)
        self.refresh_summary()
        self.origin.setFocus()

    def _uncheck(self, tile) -> None:
        self.offer_group.setExclusive(False)                              # (an exclusive group never lets its checked button be unchecked)
        tile.setChecked(False)
        self.offer_group.setExclusive(True)

    def tab_title(self) -> tuple[str, str]:
        """(text, mood) for this booking's tab: mood = idle | busy | attention | ok | bad. The text is the route, as soon as there is one."""
        o, d = self.route()
        route = f"{o} → {d}" if o and d else (f"{o} → …" if o else "New booking")
        return {"draft": (route, "idle"), "waiting": ("⏳  " + route, "busy"), "running": ("●  " + route, "busy"),
                "choose": ("❓  Choose a flight", "attention"),
                "done": (f"✓  {route}" + (f" · {self.result_pnr}" if self.result_pnr else ""), "ok"), "failed": (f"✕  {route}", "bad")}.get(self.state, (route, "idle"))

    def set_offers(self, offers: Optional[dict]) -> None:
        """Which offer tiles are shown: only the ones this key may use (None = all of them, for practice mode and development)."""
        self.offers_allowed = None if offers is None else sorted(offers)
        usable = []
        for key, tile in self.tiles.items():
            shown = offers is None or key in offers
            tile.setVisible(shown)
            on = shown and (offers is None or bool((offers.get(key) or {}).get("enabled", True)))
            if shown:
                tile.set_available(on)
            if on:
                usable.append(tile)
            elif tile.isChecked():
                self._uncheck(tile)                                       # an offer that is gone or switched off is no longer chosen
        if len(usable) == 1 and not any(t.isChecked() for t in self.tiles.values()):
            usable[0].setChecked(True)                                    # only one choice: it is already chosen
        self.refresh_summary()

    def tick(self) -> None:
        self.progress.tick()
        self.choice.tick()
        self.result.tick()


# ── page: my bookings ────────────────────────────────────────────────────────────────────────────────
class BookingsPage(QWidget):
    COLS = ("Booked", "Route", "Travel date", "Flight", "Travellers", "Offer", "PNR", "Fare", "Credits", "Pay before", "Time left", "Status")
    C_PNR, C_CREDITS, C_BEFORE, C_LEFT, C_STATUS = 6, 8, 9, 10, 11

    def __init__(self, store: Store, copy_cb):
        super().__init__()
        self.setObjectName("page")
        self.store, self.copy_cb = store, copy_cb
        v = QVBoxLayout(self)
        v.setContentsMargins(34, 28, 34, 30)
        v.setSpacing(14)
        head = QHBoxLayout()
        tl = QVBoxLayout()
        tl.addWidget(label("My bookings", "h1"))
        tl.addWidget(label(f"IndiGo holds each PNR for {HOLD_MINUTES} minutes. Pay before the time shown — after that it is cancelled "
                           "automatically. Names, phone numbers and emails are removed from this list 2 hours after a booking.",
                           "sub", wrap=True))
        head.addLayout(tl, 1)
        self.copy_btn = button("Copy PNR", "secondary")
        self.copy_btn.clicked.connect(self._copy_selected)
        head.addWidget(self.copy_btn, 0, Qt.AlignmentFlag.AlignBottom)
        clear = button("Clear history", "danger")
        clear.clicked.connect(self._clear)
        head.addWidget(clear, 0, Qt.AlignmentFlag.AlignBottom)
        v.addLayout(head)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        vh = self.table.verticalHeader()
        if vh is not None:
            vh.setVisible(False)
            vh.setDefaultSectionSize(46)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        hh = self.table.horizontalHeader()
        if hh is not None:
            hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)         # (not ResizeToContents: that re-measures every row at every change)
            hh.setSectionResizeMode(len(self.COLS) - 1, QHeaderView.ResizeMode.Stretch)
            hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._copy_row(r))
        shadow(self.table, 22, 14, 4)
        v.addWidget(self.table, 1)
        shadows_for(self, self.table)
        self.empty = label("No bookings yet. Your PNRs will appear here.", "sub")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.empty)
        v.addWidget(label("Tip: double-click a row to copy its PNR.", "muted"))
        self.recs: list[dict] = []
        self.states: list[str] = []

    COLORS = {"waiting": C["primary"], "failed": C["bad"], "held": C["warn"], "expired": C["muted"], "order": C["warn"]}

    SHOW_LAST = 150                       # rows shown (older ones stay in the file; a table of hundreds of rows is what made the window freeze)

    def refresh(self) -> None:
        """Rebuild the table. Done only when the page is on screen (see MainWindow._history_changed), with painting switched off while it is filled and the
        column widths measured ONCE afterwards - measuring them again for every cell made the cost grow with the square of the rows (0.5 s at 300 rows on a fast
        computer, many seconds - 'Not responding' - on a slow one)."""
        self.recs = self.store.bookings(self.SHOW_LAST)
        self.states = []
        t = self.table
        t.setUpdatesEnabled(False)
        try:
            t.setRowCount(len(self.recs))
            for i, r in enumerate(self.recs):
                booked = datetime.fromtimestamp(r["created_at"]).strftime("%d %b, %I:%M %p").replace(" 0", " ")
                credit, credit_tip = credit_cell(r)
                vals = (booked, f"{r['origin']} → {r['dest']}", travel_day(r), r.get("flight") or "—", pax_cell(r),
                        forms.OFFER_TITLE.get(r["offer"], r["offer"]), r.get("pnr") or "—",
                        forms.money(r["fare"]) if r.get("fare") else "—", credit, "", "", "")
                for j, val in enumerate(vals):
                    it = QTableWidgetItem(val)
                    if j == self.C_PNR and r.get("pnr"):
                        f = it.font()
                        f.setBold(True)
                        f.setFamily("Consolas")
                        f.setPointSize(11)
                        it.setFont(f)
                    if j == self.C_CREDITS:
                        it.setToolTip(credit_tip)
                    t.setItem(i, j, it)
                self.states.append("")
                self._paint_hold(i)
            t.resizeColumnsToContents()                            # (once, here; the countdown columns get room for their longest text below)
            for col in (self.C_BEFORE, self.C_LEFT):
                t.setColumnWidth(col, max(t.columnWidth(col), 96))
        finally:
            t.setUpdatesEnabled(True)
        self.empty.setVisible(not self.recs)
        t.setVisible(bool(self.recs))

    def _paint_hold(self, i: int) -> None:
        """The time-related cells of one row: Pay before / Time left / Status (and the PNR dimmed once it has expired)."""
        r = self.recs[i]
        state, status, left, d = hold_state(r)
        colour = QColor(self.COLORS.get(state, C["text"]))
        if state == "held" and d is not None and left_seconds(d) < 300:
            colour = QColor(C["bad"])
        cells = {self.C_BEFORE: clock(d) if d is not None and state in ("held", "expired") else "—",
                 self.C_LEFT: left or "—", self.C_STATUS: status}
        for col, text in cells.items():
            it = self.table.item(i, col)
            if it is None:
                continue
            it.setText(text)
            it.setForeground(colour if col != self.C_BEFORE else QColor(C["text2"]))
            if col == self.C_LEFT and state == "held":
                f = it.font()
                f.setBold(True)
                it.setFont(f)
        status_item = self.table.item(i, self.C_STATUS)
        if status_item is not None:
            status_item.setToolTip(r.get("error") or ("A PNR is held for 30 minutes; after that IndiGo cancels it if it is not paid."
                                                     if state in ("held", "expired") else status))
        pnr = self.table.item(i, self.C_PNR)
        if pnr is not None:
            f = pnr.font()
            f.setStrikeOut(state == "expired")
            pnr.setFont(f)
            pnr.setForeground(QColor(C["muted"] if state == "expired" else C["text"]))
        self.states[i] = state

    def tick(self) -> None:
        """Called every second while this page is open: the countdowns run live."""
        for i, r in enumerate(self.recs):
            if self.states[i] in ("held", "waiting", "expired") or r["status"] in ("waiting", "running"):
                if self.states[i] == "expired":
                    continue
                self._paint_hold(i)

    def _copy_row(self, row: int) -> None:
        if 0 <= row < len(self.recs) and self.recs[row].get("pnr"):
            self.copy_cb(self.recs[row]["pnr"])

    def _copy_selected(self) -> None:
        sm = self.table.selectionModel()
        rows = sm.selectedRows() if sm is not None else []
        if rows:
            self._copy_row(rows[0].row())
        elif self.recs:
            self._copy_row(0)

    def _clear(self) -> None:
        if not self.recs:
            return
        if QMessageBox.question(self, "Clear history", "Remove all finished bookings from this computer?\n"
                                "(This does not cancel anything at IndiGo.)") == QMessageBox.StandardButton.Yes:
            self.store.clear_history()
            self.refresh()


# ── page: activity ───────────────────────────────────────────────────────────────────────────────────
class ActivityPage(QWidget):
    """What is happening, step by step, in plain words (newest at the bottom). Nothing about how the work is done; in debug
    mode (the developer's) the technical story is added."""
    MAX_LINES = 2000
    COLORS = {"info": C["text2"], "ok": C["ok"], "warn": C["warn"], "error": C["bad"], "step": C["primary"]}

    def __init__(self, debug: bool = False):
        super().__init__()
        self.setObjectName("page")
        self.debug = debug
        v = QVBoxLayout(self)
        v.setContentsMargins(34, 28, 34, 30)
        v.setSpacing(14)
        head = QHBoxLayout()
        tl = QVBoxLayout()
        tl.addWidget(label("Activity", "h1"))
        tl.addWidget(label("What Best Fare is doing, step by step. If a booking does not work, the reason is here.", "sub", wrap=True))
        head.addLayout(tl, 1)
        buttons = [("Copy all", "secondary", self.copy_all)]
        if debug:
            buttons.append(("Open log folder", "secondary", self.open_folder))
        buttons.append(("Clear", "danger", self.clear))
        for text, name, fn in buttons:
            b = button(text, name)
            b.clicked.connect(fn)
            head.addWidget(b, 0, Qt.AlignmentFlag.AlignBottom)
        v.addLayout(head)
        self.view = QTextEdit()
        self.view.setObjectName("activity")
        self.view.setReadOnly(True)
        self.view.setPlaceholderText("Nothing yet. When you book a flight, every step shows up here.")
        shadow(self.view, 22, 14, 4)
        v.addWidget(self.view, 1)
        shadows_for(self, self.view)
        self.lines = 0

    def add(self, level: str, text: str, t: Optional[float] = None) -> None:
        from html import escape
        stamp = datetime.fromtimestamp(t or time.time()).strftime("%H:%M:%S")
        color = self.COLORS.get(level, self.COLORS["info"])
        weight = "600" if level in ("step", "ok", "error") else "400"
        self.view.append(f'<span style="color:{C["muted"]}">{stamp}</span>&nbsp;&nbsp;'
                         f'<span style="color:{color}; font-weight:{weight}">{escape(text)}</span>')
        self.lines += 1
        if self.lines > self.MAX_LINES:
            cur = self.view.textCursor()
            cur.movePosition(cur.MoveOperation.Start)
            cur.movePosition(cur.MoveOperation.Down, cur.MoveMode.KeepAnchor, 200)
            cur.removeSelectedText()
            self.lines -= 200
        sb = self.view.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    def text(self) -> str:
        return self.view.toPlainText()

    def copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self.text())

    def open_folder(self) -> None:
        import paths
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.log_dir())))

    def clear(self) -> None:
        self.view.clear()
        self.lines = 0


# ── sidebar ──────────────────────────────────────────────────────────────────────────────────────────
class Sidebar(QFrame):
    def __init__(self, pages: list[str]):
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(self.FULL)
        self.compact = False
        self._pages = list(pages)
        v = QVBoxLayout(self)
        self.lay = v
        v.setContentsMargins(16, 24, 16, 18)
        v.setSpacing(4)
        self.brand = label("✈  Best Fare", "brand")
        self.tag = label("IndiGo bookings with your offer", "tagline")
        v.addWidget(self.brand)
        v.addWidget(self.tag)
        v.addSpacing(22)
        self.group = QButtonGroup(self)
        self.nav: list = []
        for i, text in enumerate(pages):
            b = button(text, "nav")
            b.setCheckable(True)
            self.group.addButton(b, i)
            self.nav.append(b)
            v.addWidget(b)
        self.nav[0].setChecked(True)
        v.addStretch(1)
        self.chip = QFrame()
        self.chip.setObjectName("statusbox")
        cv = QVBoxLayout(self.chip)
        cv.setContentsMargins(12, 10, 12, 10)
        cv.setSpacing(2)
        self.chip_name = label("", "statustitle", wrap=True)
        self.chip_credits = label("", "statustext")
        self.chip_note = label("", "statustext", wrap=True)
        for w in (self.chip_name, self.chip_credits, self.chip_note):
            cv.addWidget(w)
        self.chip.hide()
        v.addWidget(self.chip)
        v.addSpacing(8)
        try:
            import buildinfo
            version = buildinfo.VERSION
        except ImportError:
            version = ""
        self.version = label(f"Version {version}" if version else "", "tagline")
        v.addWidget(self.version)
        self._account = ("", "", "", False)

    FULL, SLIM = 232, 68

    def set_account(self, name: str, credits_text: str, note: str = "", low: bool = False) -> None:
        self._account = (name, credits_text, note, low)
        self.chip_name.setText("👤  " + name)
        self.chip_credits.setText(credits_text)
        self.chip_credits.setStyleSheet("color: #FFD58A; font-weight: 700;" if low else "font-weight: 700;")
        self.chip_note.setText(note)
        self.chip_note.setVisible(bool(note) and not self.compact)
        self.chip_name.setVisible(not self.compact)
        self.chip.setToolTip(nl.join([x for x in (name, credits_text, note) if x]))
        self.chip.show()

    def set_compact(self, on: bool) -> None:
        """A narrow window: the menu becomes a slim rail of icons (the names are in the tooltips), so the booking form keeps its room."""
        if on == self.compact:
            return
        self.compact = on
        self.setFixedWidth(self.SLIM if on else self.FULL)
        self.lay.setContentsMargins(*((8, 20, 8, 14) if on else (16, 24, 16, 18)))
        self.brand.setText("✈" if on else "✈  Best Fare")
        self.brand.setAlignment(Qt.AlignmentFlag.AlignCenter if on else Qt.AlignmentFlag.AlignLeft)
        self.tag.setVisible(not on)
        self.version.setVisible(not on)
        for b, full in zip(self.nav, self._pages):
            b.setText(full.split()[0] if on else full)
            b.setToolTip(" ".join(full.split()[1:]) if on else "")
            repolish(b, compact=on)
        name, credits_text, note, low = self._account
        if name or credits_text:
            self.set_account(name, credits_text, note, low)


# ── the booking tabs ─────────────────────────────────────────────────────────────────────────────────
MOOD_COLORS = {"idle": C["text2"], "busy": C["primary"], "attention": C["warn"], "ok": C["ok"], "bad": C["bad"]}


class BookTabs(QWidget):
    """The 'New booking' page: tabs along the top like a browser's - one booking in each, a clean form in every new tab - and the booking
    workspace (form on the left, summary / progress / flight list / result on the right) of the selected tab below."""
    closing = pyqtSignal(object)                       # the traveller pressed ✕ on this page's tab (the window decides what happens)

    def __init__(self, make_page):
        super().__init__()
        self.setObjectName("page")
        self._make = make_page
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        strip = QFrame()
        strip.setObjectName("tabstrip")
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(22, 10, 18, 0)
        sl.setSpacing(6)
        self.bar = QTabBar()
        self.bar.setObjectName("booktabs")
        self.bar.setDocumentMode(True)
        self.bar.setTabsClosable(False)                    # (the ✕ is our own button: the system's close icon does not match the look)
        self.bar.setMovable(True)
        self.bar.setExpanding(False)
        self.bar.setElideMode(Qt.TextElideMode.ElideRight)
        self.bar.setUsesScrollButtons(True)
        self.bar.setDrawBase(False)
        self.bar.setCursor(Qt.CursorShape.PointingHandCursor)
        sl.addWidget(self.bar)
        self.plus = QToolButton()
        self.plus.setObjectName("newtab")
        self.plus.setText("+  New booking")
        self.plus.setToolTip("Start another booking in a new tab — the one running carries on")
        self.plus.setCursor(Qt.CursorShape.PointingHandCursor)
        sl.addWidget(self.plus)
        sl.addStretch(1)
        v.addWidget(strip)
        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)
        self.plus.clicked.connect(lambda: self.add())
        self.bar.currentChanged.connect(self._selected)
        self.bar.tabMoved.connect(lambda _a, _b: self._follow_order())

    # pages ---------------------------------------------------------------------------------------------
    def pages(self) -> list["BookPage"]:
        return [self.stack.widget(i) for i in range(self.stack.count())]

    def page_at(self, bar_index: int) -> "BookPage":
        return self.bar.tabData(bar_index)

    def current(self) -> "BookPage":
        return self.stack.currentWidget()

    def add(self, select: bool = True) -> "BookPage":
        page = self._make()
        page.state_changed.connect(self.refresh_titles)
        self.stack.addWidget(page)
        i = self.bar.addTab("New booking")
        self.bar.setTabData(i, page)
        x = QToolButton()
        x.setObjectName("tabclose")
        x.setText("✕")
        x.setToolTip("Close this booking")
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.clicked.connect(lambda _c=False, p=page: self.closing.emit(p))
        self.bar.setTabButton(i, QTabBar.ButtonPosition.RightSide, x)
        self.refresh_titles()
        if select:
            self.bar.setCurrentIndex(i)
            page.origin.setFocus()
        return page

    def _selected(self, i: int) -> None:
        if 0 <= i < self.bar.count():
            self.stack.setCurrentWidget(self.page_at(i))

    def _follow_order(self) -> None:
        self._selected(self.bar.currentIndex())

    def select(self, page: "BookPage") -> None:
        for i in range(self.bar.count()):
            if self.bar.tabData(i) is page:
                self.bar.setCurrentIndex(i)
                return

    def remove(self, page: "BookPage") -> None:
        for i in range(self.bar.count()):
            if self.bar.tabData(i) is page:
                self.bar.removeTab(i)
                break
        self.stack.removeWidget(page)
        page.setParent(None)
        page.deleteLater()
        if not self.bar.count():
            self.add()

    def refresh_titles(self) -> None:
        for i in range(self.bar.count()):
            page = self.bar.tabData(i)
            if page is None:
                continue
            text, mood = page.tab_title()
            self.bar.setTabText(i, text)
            self.bar.setTabTextColor(i, QColor(MOOD_COLORS.get(mood, C["text2"])))
            self.bar.setTabToolTip(i, {"waiting": "Waiting for the booking before it to finish", "running": "This booking is running",
                                       "choose": "IndiGo's flights are in: choose one", "done": "Booked", "failed": "Not booked"}.get(page.state, "Fill in the form and press Book now"))

    def needs_attention(self) -> list["BookPage"]:
        return [p for p in self.pages() if p.state == "choose"]


# ── the window ───────────────────────────────────────────────────────────────────────────────────────
class StallWatch:
    """If the window stops answering for 3 seconds the place it was stuck in (the Python stack of the window's thread) is written to the log file - so a
    'hang' is never a mystery again. Costs one tiny timer and one sleeping thread."""
    LIMIT_S = 3.0

    def __init__(self):
        self._beat = time.monotonic()
        self._stop = False
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        self._main = threading.main_thread().ident
        threading.Thread(target=self._watch, name="stall-watch", daemon=True).start()

    def _tick(self) -> None:
        self._beat = time.monotonic()

    def stop(self) -> None:
        self._stop = True
        self.timer.stop()

    def _watch(self) -> None:
        import sys
        import traceback
        reported = False
        while not self._stop:
            time.sleep(1.0)
            gap = time.monotonic() - self._beat
            if gap > self.LIMIT_S and not reported:
                reported = True
                frame = sys._current_frames().get(self._main)
                where = "".join(traceback.format_stack(frame)[-6:]) if frame is not None else "unknown"
                logging.getLogger("bestfare.stall").warning("the window did not answer for %.0f s; it was in:" + chr(10) + "%s", gap, where)
            elif gap < 1.0:
                reported = False


def fit_window(saved, screens, default=DEFAULT_SIZE, floor=MIN_SIZE, edge=EDGE):
    """Where and how big the window opens, as (x, y, w, h, maximized) of its client area. `screens` are the usable areas (x, y, w, h) of the screens, the main one first.
    Where it was left last time, if that place is still on a screen (a second monitor may have been unplugged); otherwise the default size, centred on the main
    screen. Never bigger than the screen's usable area less the frame, never smaller than `floor` - unless the screen itself is."""
    if not screens:
        return 0, 0, default[0], default[1], False
    fw, fh = edge[0] + edge[2], edge[1] + edge[3]

    def sized(w, h, scr):
        max_w, max_h = max(200, scr[2] - fw), max(200, scr[3] - fh)
        return max(min(floor[0], max_w), min(int(w), max_w)), max(min(floor[1], max_h), min(int(h), max_h))

    if isinstance(saved, dict):
        try:
            sx, sy, sw, sh = (int(saved[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            sx = sy = sw = sh = 0
        for scr in screens if sw > 0 and sh > 0 else ():
            if scr[0] <= sx + sw // 2 < scr[0] + scr[2] and scr[1] <= sy + sh // 2 < scr[1] + scr[3]:
                w, h = sized(sw, sh, scr)
                x = min(max(sx, scr[0] + edge[0]), scr[0] + scr[2] - edge[2] - w)
                y = min(max(sy, scr[1] + edge[1]), scr[1] + scr[3] - edge[3] - h)
                return x, y, w, h, bool(saved.get("max"))
    scr = screens[0]
    w, h = sized(default[0], default[1], scr)
    return scr[0] + edge[0] + (scr[2] - fw - w) // 2, scr[1] + edge[1] + (scr[3] - fh - h) // 2, w, h, False


class AccessLock(QFrame):
    """Covers the whole window when the server says this key may not be used (blocked, ended, moved to another computer) - or when the
    server has not been reachable for too long. Nothing under it can be clicked or typed into: a stopped key means a stopped app."""
    check_clicked = pyqtSignal()
    key_clicked = pyqtSignal()
    leave_clicked = pyqtSignal()

    LOOK = {"stopped": ("⛔", "Your access has been stopped"), "ended": ("⌛", "Your licence has ended"), "signin": ("🔑", "Please sign in again"),
            "offline": ("🔌", "Best Fare needs the internet")}

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("lockveil")
        self.setStyleSheet("QFrame#lockveil { background: rgba(243, 245, 251, 246); } QFrame#lockcard { background: white; border: 1px solid #E3E7F1; border-radius: 18px; }"
                           "QLabel#lockicon { font-size: 34pt; } QLabel#locktitle { font-size: 17pt; font-weight: 700; color: #1B2140; }"
                           "QLabel#lockmsg { font-size: 11pt; color: #525B7A; } QLabel#lockstate { font-size: 9.5pt; color: #8A93AD; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.card = QFrame()
        self.card.setObjectName("lockcard")
        self.card.setMaximumWidth(520)                           # (520 as designed; less on a window that is narrower than that)
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        v = QVBoxLayout(self.card)
        v.setContentsMargins(36, 32, 36, 28)
        v.setSpacing(10)
        self.icon = QLabel("⛔")
        self.icon.setObjectName("lockicon")
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = QLabel("")
        self.title.setObjectName("locktitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        self.msg = QLabel("")
        self.msg.setObjectName("lockmsg")
        self.msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.msg.setWordWrap(True)
        self.state = QLabel("")
        self.state.setObjectName("lockstate")
        self.state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for w in (self.icon, self.title, self.msg, self.state):
            v.addWidget(w)
        v.addSpacing(8)
        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.leave = button("Close Best Fare", "ghost")
        self.key = button("Use another key", "secondary")
        self.check = button("Check again", "primary")
        btns.addWidget(self.leave)
        btns.addStretch(1)
        btns.addWidget(self.key)
        btns.addWidget(self.check)
        v.addLayout(btns)
        row.addWidget(self.card, 100)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)
        self.check.clicked.connect(self._check)
        self.key.clicked.connect(self.key_clicked)
        self.leave.clicked.connect(self.leave_clicked)
        self.hide()

    def _check(self) -> None:
        self.check.setEnabled(False)
        self.state.setText("Checking with the server…")
        self.check_clicked.emit()

    def show_for(self, kind: str, message: str) -> None:
        icon, title = self.LOOK.get(kind, self.LOOK["stopped"])
        self.icon.setText(icon)
        self.title.setText(title)
        self.msg.setText(message)
        self.key.setVisible(kind != "offline")
        self.check.setText("Try again" if kind == "offline" else "Check again")
        self.check.setEnabled(True)
        if self.isHidden():
            self.state.setText("")
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self.check.setFocus()

    def checked(self, still_locked: bool) -> None:
        """A check has finished: say so (the message above is already the server's newest)."""
        self.check.setEnabled(True)
        if self.state.text().startswith("Checking"):
            self.state.setText("Still not possible. " + ("Please try again in a moment." if still_locked else ""))


PAGES = ("✈   New booking", "🧾   My bookings", "📋   Activity")
PAGE_BOOK, PAGE_HISTORY, PAGE_ACTIVITY = 0, 1, 2


class MainWindow(QMainWindow):
    def __init__(self, store: Store, engine_factory=None, demo: bool = False, debug: bool = False, licence=None):
        super().__init__()
        self.store = store
        self.licence = licence                                   # who is signed in (credits, access) - None in practice mode / development
        self.demo = demo
        self.debug = debug
        self.setWindowTitle("Best Fare — PRACTICE MODE" if demo else "Best Fare — IndiGo bookings")
        self._place()
        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.sidebar = Sidebar(list(PAGES))
        h.addWidget(self.sidebar)
        self.pages = QStackedWidget()
        self.tabs = BookTabs(self._new_page)                     # one booking per tab; the first tab is made below, once the engine exists
        self.history = BookingsPage(store, self.copy)
        self.activity = ActivityPage(debug)
        for p in (self.tabs, self.history, self.activity):
            self.pages.addWidget(p)
        self._side_box = QVBoxLayout()
        self._side_box.setContentsMargins(0, 0, 0, 0)
        self._side_box.setSpacing(0)
        self._side_box.addWidget(self.pages, 1)
        h.addLayout(self._side_box, 1)
        self.setCentralWidget(root)
        self.toast = Toast(root)
        self.veil = AccessLock(root)                             # covers everything when the key may not be used (see _apply_lock)
        self.veil.check_clicked.connect(self._check_now)
        self.veil.key_clicked.connect(self._other_key)
        self.veil.leave_clicked.connect(self.close)
        self.stall = StallWatch()                                # (writes down where the window was stuck, should it ever stop answering)
        self._history_stale = True                               # the bookings list is rebuilt when it is opened, not at every change
        self._credits_seen: Optional[int] = None                 # the balance last shown: a change is told in a small message
        self._hard: Optional[tuple[str, str]] = None             # what the server last said 'no' with: stays until the server says 'yes' (offline does not undo it)
        self._lock_shown: Optional[tuple[str, str]] = None
        self._checking = False
        self._verifying = False
        self._last_check = 0.0
        self._opened = time.time()
        self.sidebar.group.idClicked.connect(self._go)

        self.bridge = Bridge()
        self.bridge.fired.connect(self.on_event)
        self.bridge.account.connect(self._refresh_account)
        if self.licence is not None:
            self.licence.on_change(lambda _l: self.bridge.account.emit())
        factory = engine_factory or (lambda emit: eng.Engine(store, emit, debug=debug))
        self.engine = factory(self.bridge.fired.emit)
        self.tabs.closing.connect(self._close_tab)
        self.tabs.add()
        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self._tick)
        self.ticker.start(1000)
        self.heartbeat = QTimer(self)                            # the server is asked now and then: credits, blocked, a new version
        self.heartbeat.timeout.connect(self._heartbeat)
        self._closing = False
        self._last_active = 0.0

    # where and how big -------------------------------------------------------------------------------------
    def _place(self) -> None:
        """The window opens at a size that fits THIS screen (the usual 1320 x 860 where there is room, smaller where there is not), where it was left last time,
        and it can be made small - down to MIN_SIZE - so it works on a small or scaled-up screen too (see BookPage._adapt for how the page gives way)."""
        screens = [s for s in QGuiApplication.screens()]
        main = QGuiApplication.primaryScreen()
        if main is not None and main in screens:
            screens.remove(main)
            screens.insert(0, main)
        areas = [(a.x(), a.y(), a.width(), a.height()) for a in (s.availableGeometry() for s in screens)]
        try:
            saved = self.store.setting("window")
        except Exception:
            saved = None
        x, y, w, h, maximized = fit_window(saved, areas)
        if areas:
            self.setMinimumSize(min(MIN_SIZE[0], max(200, areas[0][2] - EDGE[0] - EDGE[2])), min(MIN_SIZE[1], max(200, areas[0][3] - EDGE[1] - EDGE[3])))
        else:
            self.setMinimumSize(*MIN_SIZE)
        self.setGeometry(x, y, w, h)
        self._maximize_on_show = maximized

    def _remember_place(self) -> None:
        try:
            g = self.normalGeometry() if (self.isMaximized() or self.isFullScreen()) else self.geometry()
            if g.width() > 0 and g.height() > 0 and not self.isMinimized():
                self.store.set_setting("window", {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height(), "max": self.isMaximized()})
        except Exception:
            logging.getLogger("bestfare").debug("could not remember the window's place", exc_info=True)

    def showEvent(self, e) -> None:  # noqa: N802 (Qt name)
        super().showEvent(e)
        if getattr(self, "_maximize_on_show", False):
            self._maximize_on_show = False
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def resizeEvent(self, e) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(e)
        if hasattr(self, "veil"):
            self.veil.setGeometry(self.centralWidget().rect())
        if hasattr(self, "sidebar"):
            self.sidebar.set_compact(e.size().width() < SLIM_BELOW)

    # the booking tabs ---------------------------------------------------------------------------------------
    @property
    def book(self) -> "BookPage":
        """The booking workspace of the selected tab (what 'the form' means to the rest of the window)."""
        return self.tabs.current()

    @property
    def group(self) -> Optional[str]:
        return self.book.group

    @property
    def group_ids(self) -> list[str]:
        return self.book.group_ids

    def _new_page(self) -> "BookPage":
        page = BookPage(self.store, self.copy)
        page.toast_cb = self.toast.say
        if self.licence is not None:
            page.credit_text = self._credit_text
        page.book_requested.connect(lambda o, p=page: self.start_booking(o, p))
        page.stop_requested.connect(lambda bid, p=page: self._stop(p, bid))
        page.flight_chosen.connect(lambda bid, key: self.engine.choose_flight(bid, key))
        page.choice_cancelled.connect(lambda _bid, p=page: self._cancel_choice(p))
        page.details_requested.connect(lambda: self._show_page(PAGE_ACTIVITY))
        page.fares_requested.connect(lambda o, d, s, n: self.engine.request_fares(o, d, s, n) if self._lock_shown is None else None)
        page.another_requested.connect(lambda: self.tabs.add())
        self._apply_account(page)
        return page

    def _page_of(self, ev: dict) -> Optional["BookPage"]:
        bid = ev.get("id")
        for p in self.tabs.pages():
            if bid and (p.progress.bid == bid or bid in p.group_ids):
                return p
        return None

    def _close_tab(self, page: "BookPage") -> None:
        if page.state in ("waiting", "running", "choose"):
            if QMessageBox.question(self, "Close this booking?", "This booking is still running. Stop it and close this tab? "
                                    "Nothing is paid; anything IndiGo is holding is released automatically.") != QMessageBox.StandardButton.Yes:
                return
            for i in page.group_ids:
                rec = self.store.get(i)
                if rec and rec["status"] in ("waiting", "running"):
                    self.engine.stop(i)
        self.tabs.remove(page)

    def start(self) -> None:
        if self.demo:
            from demo import DemoBar
            self._side_box.insertWidget(0, DemoBar(self.engine))
        self._say("step", "Best Fare started" + (" in practice mode — nothing is sent to IndiGo" if self.demo else ""))
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)                         # (to notice that the traveller is typing - see eventFilter)
        self.engine.start()                                      # (the booking browser does NOT start here: only when it is needed)
        self._history_changed()
        if self.licence is not None:
            self._refresh_account()
            self.heartbeat.start(HEARTBEAT_S * 1000)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 (Qt name)
        """Somebody is typing or clicking in the window: if the fare calendar has started the browser, it is kept (and kept warm) for the
        form - so that Book now does not have to wait for it. Never starts anything."""
        if ev.type() in (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress) and not self._closing:
            now = time.monotonic()
            if now - self._last_active > ACTIVE_EVERY_S:
                self._last_active = now
                keep = getattr(self.engine, "keep_open", None)
                if keep is not None:
                    keep(90)
        return False

    # navigation / helpers ---------------------------------------------------------------------------
    def _show_page(self, i: int) -> None:
        self.sidebar.nav[i].setChecked(True)
        self._go(i)

    def _go(self, i: int) -> None:
        came_from = self.pages.currentIndex()
        self.pages.setCurrentIndex(i)
        if i == PAGE_BOOK and came_from != PAGE_BOOK and self.tabs.current().state in ("done", "failed"):
            self.tabs.add()                                      # coming back after a booking: a clean form is ready, nothing to wipe
        if i == PAGE_HISTORY and self._history_stale:
            self._history_stale = False
            self.history.refresh()

    def _history_changed(self) -> None:
        """The list of bookings changed. It is rebuilt only when it is on screen - otherwise when it is next opened (it used to be rebuilt in full, with every
        row, at the start and the end of every booking: the window froze at exactly those moments)."""
        if self.pages.currentWidget() is self.history:
            self._history_stale = False
            self.history.refresh()
        else:
            self._history_stale = True

    def copy(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.toast.say(f"PNR {text} copied")

    def _tick(self) -> None:
        for p in self.tabs.pages():
            p.tick()
        if self.pages.currentWidget() is self.history:
            self.history.tick()

    # the licence -----------------------------------------------------------------------------------
    def _credit_text(self, n: int, infants: int = 0) -> tuple[str, bool]:
        L = self.licence
        if L is None:
            return "", False
        if not L.usable:
            return "", False
        have = L.account.credits
        cost = L.account.cost(n, infants)
        return (f"💳  This booking uses {cost} credit{'s' if cost != 1 else ''} — you have {have}.", have < cost)

    def _refresh_account(self) -> None:
        L = self.licence
        if L is None:
            return
        a = L.account
        if L.usable:
            if self._credits_seen is not None and a.credits != self._credits_seen:
                d = a.credits - self._credits_seen
                self.toast.say(f"💳  {abs(d)} credit{'s' if abs(d) != 1 else ''} {'used' if d < 0 else 'added back'} — you have {a.credits} now", 4000)
            self._credits_seen = a.credits
            low = a.credits < 3
            self.sidebar.set_account(a.label or "Signed in", f"{a.credits} credit{'s' if a.credits != 1 else ''}", "Out of credits — ask your admin" if a.credits == 0 else "", low)
        elif L.state == "offline" and self._hard is None:
            self.sidebar.set_account(a.label or "Signed in", f"{a.credits} credit{'s' if a.credits != 1 else ''}", "Not connected — trying again", True)
        else:
            msg = L.message or "Your access has been stopped. Please contact your admin."
            self.sidebar.set_account(a.label or "Not signed in", "Access stopped", msg, True)
        for page in self.tabs.pages():
            self._apply_account(page)
        self._apply_lock(from_server=True)

    def _apply_account(self, page: "BookPage") -> None:
        """What the signed-in key allows, in one booking tab: its offers, its credits, the admin's message - or 'access stopped'."""
        L = self.licence
        if L is None:
            page.set_offers(None)
            return
        a = L.account
        page.set_offers(a.offers if a.offers else None)
        if L.usable:
            page.summary.set_notice(a.broadcast)
            page.summary.book.setEnabled(True)
            page.refresh_summary()
        else:
            msg = L.message or "Your access has been stopped. Please contact your admin."
            page.summary.set_notice("")
            page.summary.set_credit_line("⛔  " + msg, True)
            page.summary.book.setEnabled(False)

    # who may use the window: the server's last word, felt within seconds --------------------------------------------------------
    def _heartbeat(self) -> None:
        L = self.licence
        if L is None or self._closing:
            return
        if self.engine.busy and time.time() - max(L.last_ok or 0.0, self._opened) < QUIET_CHECK_S:
            return                                               # (a running booking is not disturbed by a check)
        self._check_now()

    def _verified(self) -> bool:
        """THE check: one question to the server right now, for the booking that is about to start. True = this key may book. The window keeps answering while it waits
        (a nested event loop, a few seconds at most); a 'no' covers the window with the lock card, an unreachable server is said in plain words. (The server refuses a
        blocked key's booking anyway - this makes the window match, at the moment it matters, without asking over and over.)"""
        L = self.licence
        if L is None:
            return True
        if self._verifying:
            return False
        self._verifying = True
        loop = QEventLoop(self)
        ended = {"yes": False}
        self.bridge.checked.connect(lambda: ended.update(yes=True))
        self.bridge.checked.connect(loop.quit)
        QTimer.singleShot(10000, loop.quit)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._check_now()
            if self._checking:
                loop.exec()
            else:
                ended["yes"] = True                              # (it was over before we looked)
        finally:
            QApplication.restoreOverrideCursor()
            try:
                self.bridge.checked.disconnect()                 # (its own two listeners)
            except TypeError:
                pass
            self._verifying = False
        ok = L.usable and ended["yes"]                           # (no answer within the time given is not a 'yes')
        if not ok:
            self._apply_lock(from_server=True)
            if self._lock_shown is None:                         # (not 'stopped': the server could not be reached)
                QMessageBox.information(self, "Best Fare", L.message or "Cannot reach the Best Fare server. Please check the internet connection and try again.")
        return ok

    def _check_now(self) -> None:
        if self.licence is None or self._checking or self._closing:
            return
        self._checking = True
        self._last_check = time.monotonic()

        def run() -> None:
            try:
                self.licence.refresh()
            finally:
                self._checking = False
                self.bridge.account.emit()                       # (even when nothing changed: the lock card learns the check is over)
                self.bridge.checked.emit()
        threading.Thread(target=run, name="licence-refresh", daemon=True).start()

    def _lock_reason(self) -> Optional[tuple[str, str]]:
        """(kind, words) when the window must be locked, else None. 'Stopped / ended / sign in again' hold until the server says yes - going offline does not undo them."""
        L = self.licence
        if L is None:
            return None
        st = L.state
        if st == "ok" and L.account.status == "active":
            self._hard = None
            return None
        if st in ("blocked", "expired", "new") or st == "ok":
            kind = "ended" if st == "expired" else "signin" if st == "new" else "stopped"
            words = L.message or ("Please type your key again." if kind == "signin" else "Your access has been stopped. Please contact your admin.")
            self._hard = (kind, words)
            return self._hard
        if self._hard is not None:                               # (it said 'stopped' before: a missing connection does not undo that)
            return self._hard
        return None

    def _apply_lock(self, from_server: bool = False) -> None:
        why = self._lock_reason()
        if why == self._lock_shown:
            if why is not None and from_server:
                self.veil.checked(True)                          # (a check has just ended and the answer is still 'no')
            return
        self._lock_shown = why
        locked = why is not None
        self.sidebar.setEnabled(not locked)
        self.pages.setEnabled(not locked)
        if hasattr(self.engine, "keep_warm"):
            self.engine.keep_warm = not locked                   # a locked window keeps nothing alive on IndiGo's side
        if locked and hasattr(self.engine, "release_browser"):
            self.engine.release_browser()
        if locked:
            self.veil.show_for(*why)
            if why[0] != "offline":                              # a stopped key stops what it started (a missing connection does not)
                for rec in self.store.bookings(50):
                    if rec["status"] in ("waiting", "running"):
                        self.engine.stop(rec["id"])
            self._say("warn", "Access stopped — " + why[1])
        else:
            self.veil.hide()
            self._say("ok", "Access is back")
            self._apply_account_all()

    def _apply_account_all(self) -> None:
        for page in self.tabs.pages():
            self._apply_account(page)

    def _other_key(self) -> None:
        """'Use another key' on the lock card: the same window as at start-up."""
        try:
            import shell
            import shell_ui
            dlg = shell_ui.LoginDialog(self.licence, shell.computer_text())
        except Exception:  # noqa: BLE001 - a development copy has no shell
            self.veil.state.setText("Please close Best Fare and open it again to type another key.")
            return
        dlg.exec()
        self.bridge.account.emit()

    def _say(self, level: str, text: str, t: Optional[float] = None) -> None:
        """One line of the Activity page - also kept in the log file (the customer-facing story, nothing technical)."""
        self.activity.add(level, text, t)
        activity_log.info(text)

    # booking ----------------------------------------------------------------------------------------
    def start_booking(self, order: forms.Order, page: Optional["BookPage"] = None) -> None:
        page = page or self.book
        L = self.licence
        if L is not None:                                        # the server decides in the end; this is only the quick, friendly answer
            adults, children, infants = order.counts()
            n = adults + children
            if not self._verified():                             # one check with the server, now - not a stream of them
                return
            if not L.can_afford(n, infants):
                need = L.account.cost(n, infants)
                QMessageBox.information(self, "Not enough credits",
                                        f"This booking needs {need} credit{'s' if need != 1 else ''} but you have {L.account.credits}. "
                                        "Please contact your admin to get more credits.")
                return
        waiting = bool(self.engine.busy)                          # another tab's booking is running: this one queues behind it
        page.group, page.group_ids = self.engine.submit(order)
        page.show_progress(waiting)
        self._begin(page, self.store.get(page.group_ids[0]), waiting)
        self._history_changed()

    def _begin(self, page: "BookPage", rec: dict, waiting: bool = False) -> None:
        route = f"{stations.label(rec['origin'])} → {stations.label(rec['dest'])}"
        page.progress.begin(rec["id"], rec["offer"], route, travel_day(rec), waiting)

    def _stop(self, page: "BookPage", bid: str) -> None:
        if not bid:
            return
        if QMessageBox.question(self, "Stop this booking?",
                                "Stop this booking now? Nothing is paid; anything IndiGo is holding is released "
                                "automatically.") == QMessageBox.StandardButton.Yes:
            page.progress.stop.setEnabled(False)
            for i in page.group_ids:
                rec = self.store.get(i)
                if rec and rec["status"] in ("waiting", "running"):
                    self.engine.stop(i)

    def _cancel_choice(self, page: "BookPage") -> None:
        """'Cancel this booking' on the flight list: nothing is held yet, so no question is asked."""
        for i in page.group_ids:
            rec = self.store.get(i)
            if rec and rec["status"] in ("waiting", "running"):
                self.engine.stop(i)

    def on_event(self, ev: dict) -> None:
        t = ev.get("type")
        self._log_event(ev)
        page = self._page_of(ev)
        if t == "fares":
            for p in self.tabs.pages():
                p.on_fares(ev)                                   # (each tab keeps only the answer for the route it shows)
        elif t == "started" and page is not None:
            page.progress.started()
            page._set_state("running")                           # noqa: SLF001
        elif t == "choose" and page is not None:
            rec = self.store.get(ev["id"]) or {}
            route = f"{stations.label(rec['origin'])} → {stations.label(rec['dest'])}" if rec else ""
            page.show_choice(ev["id"], ev.get("flights") or [], route, travel_day(rec) if rec else "",
                             forms.record_mix(rec.get("travellers") or []) if rec else (1, 0, 0), ev.get("seconds", eng.CHOICE_WAIT_S))
            self.tabs.select(page)                               # the fares are kept for a few minutes only: this tab comes to the front
            self._go_book()
            QApplication.alert(self, 0)                          # the booking waits for the traveller: flash the taskbar
        elif t == "chosen" and page is not None:
            page.show_progress()                                 # (the picked flight goes ahead; the steps continue)
        elif t == "stage" and page is not None:
            page.progress.started()
            page.progress.steps.set_active(ev["stage"])
        elif t == "note" and page is not None:
            page.progress.note(ev["text"])
        elif t == "finished":
            rec = ev.get("record") or {}
            for p in self.tabs.pages():
                if ev.get("id") == p.progress.bid:
                    p.progress.steps.finish(rec.get("status") == "done")
            self._history_changed()
            for p in self.tabs.pages():
                if ev.get("grp") and ev.get("grp") == p.group:
                    recs = [self.store.get(i) for i in p.group_ids]
                    if all(r and r["status"] in ("done", "failed") for r in recs):
                        p.show_result(recs)
                        QApplication.alert(self, 0)
                        if p is not self.tabs.current() or self.pages.currentWidget() is not self.tabs:
                            self.toast.say(f"Booking done — PNR {p.result_pnr}" if p.state == "done" else "A booking could not be completed")
                        self._go_book()

    def _go_book(self) -> None:
        if self.pages.currentWidget() is self.activity:        # the user is reading the steps: do not pull them away
            return
        self.sidebar.nav[PAGE_BOOK].setChecked(True)
        self.pages.setCurrentWidget(self.tabs)

    def _log_event(self, ev: dict) -> None:
        """The engine's events, told as lines of the Activity page (customer words only; debug mode adds the technical lines)."""
        t = ev.get("type")
        if t == "log":
            if self.debug:
                self._say(ev.get("level", "info"), ev["text"], ev.get("t"))
        elif t == "started":
            self._say("step", f"Booking started — {forms.OFFER_TITLE.get(ev.get('offer'), '')}")
        elif t == "stage":
            self._say("step", eng.STAGE_TEXT.get(ev["stage"], ev["stage"]))
        elif t == "choose":
            n = len(ev.get("flights") or [])
            self._say("step", f"Choose your flight — {n} flight{'s' if n != 1 else ''} found")
        elif t == "note":
            self._say("info", ev["text"])
        elif t == "fares":
            if ev.get("error"):
                self._say("info", "Fares are not available right now")
            elif ev.get("empty"):
                self._say("info", "IndiGo shows no fares for this route in the calendar — pick any date")
            else:
                self._say("info", f"Fares loaded for {stations.city(ev['origin'])} → {stations.city(ev['dest'])}")
        elif t == "finished":
            r = ev.get("record") or {}
            if r.get("status") == "done":
                self._say("ok", f"Booking done — PNR {r.get('pnr')}")
            else:
                self._say("error", "Booking not completed — " + (r.get("error") or "please try again"))

    # closing ----------------------------------------------------------------------------------------
    def closeEvent(self, e) -> None:  # noqa: N802 (Qt name)
        if self._closing:
            e.accept()
            return
        if self.engine.busy:
            if QMessageBox.question(self, "A booking is running",
                                    "A booking is still running. Close Best Fare anyway? The booking will stop.") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self._closing = True
        self._remember_place()
        self.heartbeat.stop()
        self.stall.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.ticker.stop()
        self.engine.shutdown(close_browser=True)
        e.accept()
