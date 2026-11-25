"""The right-hand panel that asks "Choose your flight": every flight IndiGo has for the trip, each with its brief details and its
lowest fare. The traveller taps one (the lowest fare is ready), presses Continue, and the booking goes ahead with that flight."""
from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

import flights
import forms
from widgets import FitButton, WrapRow, button, divider, label, repolish, shadow


class FlightCard(QFrame):
    """One flight: when it leaves and lands, how long it takes, stops, flight number, and its lowest fare."""
    clicked = pyqtSignal(str)
    confirmed = pyqtSignal(str)

    def __init__(self, o: dict, lowest: bool, pax: int, mixed: bool = False):
        super().__init__()
        self.o = o
        self.key: str = o["key"]
        self.setObjectName("flightcard")
        self.setProperty("sel", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        v = QVBoxLayout(self)
        v.setContentsMargins(13, 10, 13, 10)
        v.setSpacing(3)
        r1 = QHBoxLayout()
        self.when = label(flights.timeline(o), "flighttime", wrap=True)         # (the lines wrap when the panel is narrow, instead of being cut)
        self.price = label(flights.money(o), "flightprice")
        r1.addWidget(self.when)
        r1.addStretch(1)
        r1.addWidget(self.price)
        v.addLayout(r1)
        r2 = QHBoxLayout()                                       # how long, stops  |  what each traveller pays
        r2.setSpacing(8)
        meta = " · ".join(x for x in (flights.duration(o.get("minutes")), flights.stops_text(o)) if x)
        self.meta = label(meta, "flightmeta", wrap=True)
        r2.addWidget(self.meta, 1)
        self.each = None
        if pax > 1 and not mixed:                                # (children and infants pay other fares: no "each" then, only the total)
            self.each = label(f"{forms.money(o['amount'] / pax)} each", "flightmeta")
            r2.addWidget(self.each, 0, Qt.AlignmentFlag.AlignRight)
        v.addLayout(r2)
        ident = QWidget()                                        # flight number, "Lowest fare"  |  the offer (or none): in one line, or - when the panel is narrow - two
        ident.setObjectName("clear")
        r3 = QHBoxLayout(ident)
        r3.setContentsMargins(0, 0, 0, 0)
        r3.setSpacing(8)
        self.number = label(flights.flight_number(o), "flightmeta")
        r3.addWidget(self.number)
        self.pill = label("Lowest fare", "pill") if lowest else None
        if self.pill:
            r3.addWidget(self.pill)
        r3.addStretch(1)
        save = flights.saving(o)
        self.save = label(f"Offer: save {forms.money(save)}", "flightsave") if save > 0 else label("Regular fare", "flightmeta")
        v.addWidget(WrapRow(ident, self.save, gap=3, spread=True))

    def set_selected(self, on: bool) -> None:
        repolish(self, sel=on)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.key)
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self.confirmed.emit(self.key)


class ChoicePanel(QFrame):
    chosen = pyqtSignal(str, str)          # booking id, the flight's key
    cancelled = pyqtSignal(str)            # booking id
    ROOMY, SNUG = (20, 20, 20, 18), (14, 16, 14, 14)       # the panel's inner padding: as designed, and a little less when the window is narrow

    def __init__(self):
        super().__init__()
        self.setObjectName("panel")
        shadow(self, 30, 26, 8)
        v = QVBoxLayout(self)
        self.lay = v
        v.setContentsMargins(*self.ROOMY)
        v.setSpacing(9)
        v.addWidget(label("CHOOSE YOUR FLIGHT", "kicker"))
        self.route = label("", "route", wrap=True)
        self.route.setStyleSheet("font-size: 14.5pt;")
        self.sub = label("", "routecities", wrap=True)
        v.addWidget(self.route)
        v.addWidget(self.sub)

        sorts = QWidget()                                          # "Lowest fare | Earliest"  and  "[ ] Non-stop only": side by side, or one under the other when narrow
        sorts.setObjectName("clear")
        sl = QHBoxLayout(sorts)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self.sort_group = QButtonGroup(self)
        self.sort_btns = {}
        for pos, (key, text) in zip(("left", "right"), flights.SORTS):
            b = button(text, "seg")
            b.setCheckable(True)
            repolish(b, pos=pos)
            self.sort_group.addButton(b)
            self.sort_btns[key] = b
            sl.addWidget(b)
        self.sort_btns[flights.SORTS[0][0]].setChecked(True)
        self.nonstop = QCheckBox("Non-stop only")
        v.addWidget(WrapRow(sorts, self.nonstop, gap=12, left=True))

        self.listview = QScrollArea()
        self.listview.setWidgetResizable(True)
        self.listview.setFrameShape(QFrame.Shape.NoFrame)
        self.listview.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.listview.setMinimumHeight(250)
        self.listview.setStyleSheet("QScrollArea { background: transparent; }")
        self.list_body = QWidget()
        self.list_body.setObjectName("clear")
        self.list_lay = QVBoxLayout(self.list_body)
        self.list_lay.setContentsMargins(0, 0, 4, 0)
        self.list_lay.setSpacing(7)
        self.listview.setWidget(self.list_body)
        self.listview.viewport().setAutoFillBackground(False)
        v.addWidget(self.listview, 1)

        self.infant_note = label("The fares above are for the adults and children. The infant's fee is added when the fare is held — you see the total "
                                 "before you pay.", "muted", wrap=True)
        self.infant_note.hide()
        v.addWidget(self.infant_note)
        v.addWidget(divider())
        self.pick = label("", "small", wrap=True)
        v.addWidget(self.pick)
        self.go = FitButton("Continue with this flight", "primary")
        self.go.setMinimumHeight(46)
        v.addWidget(self.go)
        self.timer = label("", "timer", wrap=True)
        v.addWidget(self.timer)
        self.cancel = button("Cancel this booking", "ghost")
        v.addWidget(self.cancel, 0, Qt.AlignmentFlag.AlignLeft)

        self.bid: Optional[str] = None
        self.options: list[dict] = []
        self.cards: dict[str, FlightCard] = {}              # the cards that are SHOWN, in order
        self._all: dict[str, FlightCard] = {}               # every card of this list (built once; sorting and filtering only rearrange them)
        self.selected: Optional[str] = None
        self.pax = 1
        self.mixed = False
        self.deadline = 0.0
        self.sort_group.buttonClicked.connect(lambda _b: self._arrange())
        self.nonstop.toggled.connect(lambda _on: self._arrange())
        self.go.clicked.connect(self._confirm)
        self.cancel.clicked.connect(lambda: self.cancelled.emit(self.bid or ""))

    def set_snug(self, snug: bool) -> None:
        self.lay.setContentsMargins(*(self.SNUG if snug else self.ROOMY))

    # ── showing a list ───────────────────────────────────────────────────────────────────────────────
    def show_flights(self, bid: str, options: list[dict], route: str, day: str, pax, seconds: float) -> None:
        """`pax` = how many travellers, or (adults, children, infants)."""
        a, c, i = pax if isinstance(pax, (tuple, list)) else (int(pax), 0, 0)
        self.mixed = bool(c or i)
        self.infant_note.setVisible(bool(i))
        self.bid, self.options, self.pax = bid, list(options), max(1, a + c)
        self.deadline = time.monotonic() + seconds
        self.route.setText(route)
        n = len(self.options)
        who = forms.mix_text(a, c, i)
        self.sub.setText(f"{day}  ·  {who}  ·  {n} flight{'s' if n != 1 else ''}")
        has_connections = any((o.get("stops") or 0) > 0 for o in self.options)
        self.nonstop.setVisible(has_connections and any((o.get("stops") or 0) == 0 for o in self.options))
        self.nonstop.blockSignals(True)
        self.nonstop.setChecked(False)
        self.nonstop.blockSignals(False)
        self.sort_btns[flights.SORTS[0][0]].setChecked(True)
        self.go.setEnabled(True)
        self.selected = flights.lowest_key(self.options)                  # the lowest fare is ready; the traveller may pick another
        self._build()
        self._arrange()
        self.tick()

    def _build(self) -> None:
        """One card per flight, made once per list (a slow computer feels every widget it has to create)."""
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            w = it.widget() if it else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._all = {}
        low = flights.lowest_key(self.options)
        for o in self.options:
            c = FlightCard(o, o["key"] == low, self.pax, self.mixed)
            c.clicked.connect(self._select)
            c.confirmed.connect(lambda k: (self._select(k), self._confirm()))
            c.hide()
            self._all[o["key"]] = c

    def _arrange(self) -> None:
        """Sorting and filtering: the existing cards are put in the new order (and the filtered ones hidden) - nothing is rebuilt."""
        while self.list_lay.count():
            self.list_lay.takeAt(0)                                        # (the cards stay alive; only their place in the list is taken)
        sort = "time" if self.sort_btns["time"].isChecked() else "price"
        shown = flights.arranged(self.options, sort, self.nonstop.isChecked())
        keep = {o["key"] for o in shown}
        for k, c in self._all.items():
            if k not in keep:
                c.hide()
        self.cards = {}
        for o in shown:
            c = self._all[o["key"]]
            self.cards[o["key"]] = c
            self.list_lay.addWidget(c)
            c.show()
        self.list_lay.addStretch(1)
        if self.selected not in self.cards:                                # the pick was filtered away: the lowest fare shown
            self.selected = flights.lowest_key(shown)
        self._mark()

    def _select(self, key: str) -> None:
        self.selected = key
        self._mark()

    def _mark(self) -> None:
        for k, c in self.cards.items():
            c.set_selected(k == self.selected)
        o = self.option(self.selected)
        if o:
            self.pick.setText(f"Your flight:  {flights.line(o)}  ·  {flights.money(o)}")
            self.go.set_wordings(f"Continue with {flights.flight_number(o)} · {flights.money(o)}", f"Continue · {flights.money(o)}", "Continue")
            self.go.setEnabled(True)
            card = self.cards.get(o["key"])
            if card is not None:                                           # (after the list has been laid out)
                QTimer.singleShot(0, lambda c=card: self._reveal(c))
        else:
            self.pick.setText("No flight matches. Turn off “Non-stop only”.")
            self.go.set_wordings("Continue")
            self.go.setEnabled(False)

    def _reveal(self, card: FlightCard) -> None:
        try:
            if card.key in self.cards and self.cards[card.key] is card:
                self.listview.ensureWidgetVisible(card, 0, 24)
        except RuntimeError:                                               # the card was replaced by a newer list meanwhile
            pass

    def option(self, key: Optional[str]) -> Optional[dict]:
        return next((o for o in self.options if o["key"] == key), None)

    def _confirm(self) -> None:
        if self.bid and self.selected and self.go.isEnabled():
            self.go.setEnabled(False)                                      # one press is enough
            self.chosen.emit(self.bid, self.selected)

    # ── the clock: the fares on the list stay valid for a few minutes only ──────────────────────────────
    def left(self) -> int:
        return max(0, int(self.deadline - time.monotonic()))

    def tick(self) -> None:
        if not self.bid:
            return
        s = self.left()
        self.timer.setText(f"⏳ Please choose within {s // 60}:{s % 60:02d} — the fares are kept for a few minutes only.")
        repolish(self.timer, urgent=s < 60, over=False)
