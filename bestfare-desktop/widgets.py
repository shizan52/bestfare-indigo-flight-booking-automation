"""Small building blocks of the window: cards, the From / To boxes with suggestions, the date field with its fare calendar,
traveller rows, offer tiles, progress steps, toasts."""
from __future__ import annotations

import calendar as pycal
from datetime import date, timedelta
from typing import Optional

from PyQt6.QtCore import QDate, QEvent, QModelIndex, QPoint, QRectF, QRegularExpression, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QRegularExpressionValidator, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QBoxLayout, QButtonGroup, QCompleter, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListView, QPushButton, QSizePolicy,
                             QStyle, QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget)

import forms
import stations
from shadows import ShadowLayer
from theme import C


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    if wrap:
        lb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return lb


class FlowLabel(QLabel):
    """A label that stays on ONE line while it fits and wraps only when it has to. (A plain word-wrapped QLabel prefers a 'nicely shaped' block of text: it breaks
    even a short caption in two for no reason, and asks for no more width than that block.)"""

    def __init__(self, text: str = "", name: str = ""):
        super().__init__(text)
        if name:
            self.setObjectName(name)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        text = self.text()
        flags = 0 if chr(10) in text else int(Qt.TextFlag.TextSingleLine)          # (a text with line breaks: the width of its longest line)
        w = self.fontMetrics().boundingRect(0, 0, 100000, 100000, flags, text).width() + 2 * self.margin() + 2
        return QSize(w, self.heightForWidth(w))


def button(text: str, name: str = "secondary", tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setObjectName(name)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    return b


class FitButton(QPushButton):
    """A button whose wording gives way instead of asking for more width than it has: it shows the first of its wordings that fits and, as the last resort, the
    shortest one cut with an ellipsis - so a long label (two flight numbers and a fare) can never make a narrow panel wider than its column.
    The full wording stays in the tooltip."""
    PAD = 44                                                   # the style sheet's padding (20 px each side) and the border

    def __init__(self, text: str = "", name: str = "primary"):
        super().__init__(text)
        self.setObjectName(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._wordings = [text]

    def set_wordings(self, *texts: str) -> None:
        """The wordings, longest first."""
        self._wordings = [t for t in texts if t] or [""]
        self._fit()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(60, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(self.fontMetrics().horizontalAdvance(self._wordings[0]) + self.PAD, super().sizeHint().height())

    def resizeEvent(self, e) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(e)
        self._fit()

    def showEvent(self, e) -> None:  # noqa: N802 (Qt name)
        super().showEvent(e)
        self._fit()                                                # (a resize that came while the button was still hidden - or before the style sheet gave it its font - decided nothing)

    def changeEvent(self, e) -> None:  # noqa: N802 (Qt name)
        super().changeEvent(e)
        if e.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and self.isVisible():
            self._fit()

    def _fit(self) -> None:
        if self.width() <= 1 or not self.isVisible():              # (not laid out yet: show the full wording; the first resize decides)
            shown = self._wordings[0]
        else:
            fm, room = self.fontMetrics(), self.width() - self.PAD
            shown = next((t for t in self._wordings if fm.horizontalAdvance(t) <= room), "")
            if not shown:
                shown = fm.elidedText(self._wordings[-1], Qt.TextElideMode.ElideRight, max(room, 20))
        if shown != self.text():
            self.setText(shown)
        self.setToolTip(self._wordings[0] if shown != self._wordings[0] else "")


def divider() -> QFrame:
    f = QFrame()
    f.setObjectName("divider")
    return f


def set_error(w: QWidget, on: bool) -> None:
    """Red outline on a field (re-polish so the style sheet sees the property change)."""
    if bool(w.property("error")) == on:
        return
    w.setProperty("error", on)
    st = w.style()
    if st is not None:
        st.unpolish(w)
        st.polish(w)


def repolish(w: QWidget, **props) -> None:
    for k, v in props.items():
        w.setProperty(k, v)
    st = w.style()
    if st is not None:
        st.unpolish(w)
        st.polish(w)
    w.updateGeometry()                                            # (a style can change a widget's size - padding, font - and the layouts around it must hear of it)


def shadow(w: QWidget, blur: int = 28, alpha: int = 28, dy: int = 6, radius: int = 16) -> None:
    """The widget wants a soft shadow. It is NOT an effect on the widget (that makes Qt re-draw and re-blur the whole widget at every change inside it):
    the shadow is only noted here, and the layer in the widget's parent paints it (shadows.ShadowLayer.watch_all)."""
    w.setProperty("shadow", (blur, alpha, dy, radius))


def shadows_for(host: QWidget, *widgets: QWidget) -> "ShadowLayer":
    """Put the shadows of `widgets` (children of `host`, each asked for one with shadow()) behind them."""
    layer = ShadowLayer(host)
    for w in widgets:
        blur, alpha, dy, radius = w.property("shadow") or (22, 16, 4, 16)
        layer.watch(w, blur, alpha, dy, radius)
    return layer


class ErrorLabel(QLabel):
    def __init__(self):
        super().__init__("")
        self.setObjectName("err")
        self.setWordWrap(True)
        self.hide()

    def show_text(self, text: Optional[str]) -> None:
        self.setText(text or "")
        self.setVisible(bool(text))


class Card(QFrame):
    """A white card with a numbered title: ① Trip, ② Travellers, ..."""
    ROOMY, SNUG = (22, 18, 22, 20), (14, 14, 14, 16)             # the inner padding: as designed, and a little less when the window is narrow

    def __init__(self, number: int, title: str, hint: str = ""):
        super().__init__()
        self.setObjectName("card")
        shadow(self, 22, 16, 4)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(*self.ROOMY)
        self.lay.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(10)
        num = QLabel(str(number))
        num.setObjectName("stepnum")
        head.addWidget(num)
        t = label(title, "cardtitle")
        head.addWidget(t)
        head.addStretch(1)
        self.right = QHBoxLayout()
        head.addLayout(self.right)
        self.lay.addLayout(head)
        if hint:
            self.lay.addWidget(label(hint, "cardhint", wrap=True))

    def set_snug(self, snug: bool) -> None:
        self.lay.setContentsMargins(*(self.SNUG if snug else self.ROOMY))


def field(caption: str, widget: QWidget, err: Optional[ErrorLabel] = None) -> QWidget:
    w = QWidget()
    w.setObjectName("clear")
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(5)
    v.addWidget(FlowLabel(caption, "label"))                      # (one line while it fits; a long caption wraps instead of making the whole form wider than the window)
    v.addWidget(widget)
    if err is not None:
        v.addWidget(err)
    return w


class WrapRow(QWidget):
    """Two blocks side by side while there is room, one under the other when there is not - so nothing is ever cut off. It decides by its OWN width (whatever the
    window, the scrollbar or the scale), with a little hysteresis so that a scrollbar appearing - a few pixels less room - cannot make it flip back and forth."""
    HYSTERESIS = 24

    def __init__(self, first: QWidget, second: QWidget, gap: int = 8, drop: int = 0, left: bool = False, spread: bool = False):
        super().__init__()
        self.setObjectName("clear")
        self.box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(gap)
        align = Qt.AlignmentFlag.AlignLeft if left else Qt.AlignmentFlag(0)       # left: each block keeps its own natural width, also when one stands under the other
        self.box.addWidget(first, 0, align)
        if spread:
            self.box.addStretch(1)                                  # spread: the second block sits at the far end (when there is room beside the first)
        self.box.addWidget(second, 0, align)
        if not spread:
            self.box.addStretch(1)
        self.first = first
        self.second = second
        self.drop = drop                                           # how far the second block is let down beside the first (to line up with the box under a caption)
        self.stacked = False
        self._need = 0
        self._set_drop(drop)

    def _set_drop(self, top: int) -> None:
        lay = self.second.layout()
        if lay is not None:
            lay.setContentsMargins(0, top, 0, 0)

    def _stack(self, on: bool) -> None:
        self.stacked = on
        self.box.setDirection(QBoxLayout.Direction.TopToBottom if on else QBoxLayout.Direction.LeftToRight)
        self._set_drop(0 if on else self.drop)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        """Side by side needs more room than the two blocks need one under the other - and the row can stand either way, so it asks for the smaller of the two
        (otherwise the window around it could never get narrow enough for the row to give way)."""
        s = super().minimumSizeHint()
        if self.stacked:
            return s
        return QSize(max(self.first.minimumSizeHint().width(), self.second.minimumSizeHint().width()), s.height())

    def resizeEvent(self, e) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(e)
        w = e.size().width()
        if not self.stacked:
            self._need = self.box.minimumSize().width()            # what the side-by-side arrangement needs, measured live
            if w < self._need:
                self._stack(True)
        elif w >= self._need + self.HYSTERESIS:
            self._stack(False)


# ── From / To: type a few letters, pick from IndiGo's own airport list ────────────────────────────────────────────
CODE_ROLE = Qt.ItemDataRole.UserRole + 1
AIRPORT_ROLE = Qt.ItemDataRole.UserRole + 2
STATE_ROLE = Qt.ItemDataRole.UserRole + 3


class StationDelegate(QStyledItemDelegate):
    """One suggestion: the city in bold, the airport's name under it, the code on the right - like IndiGo's own list."""
    H = 54

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), self.H)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p.save()
        r = QRectF(option.rect).adjusted(6, 2, -6, -2)
        hot = bool(option.state & QStyle.StateFlag.State_Selected) or bool(option.state & QStyle.StateFlag.State_MouseOver)
        if hot:
            path = QPainterPath()
            path.addRoundedRect(r, 9, 9)
            p.fillPath(path, QColor(C["primary_soft"]))
        city = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        city = city.rsplit(" (", 1)[0]
        code = str(index.data(CODE_ROLE) or "")
        airport = str(index.data(AIRPORT_ROLE) or "")
        f = QFont(option.font)
        f.setPointSizeF(10.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(C["text"]))
        p.drawText(QRectF(r.left() + 12, r.top() + 5, r.width() - 80, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, city)
        f2 = QFont(option.font)
        f2.setPointSizeF(8.5)
        p.setFont(f2)
        p.setPen(QColor(C["muted"]))
        fm = p.fontMetrics()
        p.drawText(QRectF(r.left() + 12, r.top() + 27, r.width() - 80, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(airport, Qt.TextElideMode.ElideRight, int(r.width() - 90)))
        f3 = QFont(option.font)
        f3.setPointSizeF(10)
        f3.setBold(True)
        p.setFont(f3)
        p.setPen(QColor(C["primary"]))
        p.drawText(QRectF(r.right() - 64, r.top(), 56, r.height()), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, code)
        p.restore()


class StationBox(QLineEdit):
    """A From / To box that suggests as you type (city, old name, airport name or code) and understands what it is given.
    Pick with the mouse or Enter / Tab; the first suggestion is always ready."""
    changed = pyqtSignal(str)        # the airport code now chosen, or "" when the text no longer names one
    chosen = pyqtSignal(str)         # an airport was picked from the list (the form moves on by itself)

    def __init__(self, placeholder: str):
        super().__init__()
        self.setPlaceholderText(placeholder)
        self._code: Optional[str] = None
        self._listed: Optional[str] = None
        self._model = QStandardItemModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setMaxVisibleItems(7)
        popup = QListView()
        popup.setObjectName("stationpopup")
        popup.setMouseTracking(True)
        popup.setUniformItemSizes(True)
        popup.setFrameShape(QFrame.Shape.NoFrame)
        popup.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._completer.setPopup(popup)
        popup.setItemDelegate(StationDelegate(popup))                       # (after setPopup: Qt puts its own delegate in there)
        self.setCompleter(self._completer)
        self._completer.activated[QModelIndex].connect(self._picked)
        self.textEdited.connect(self._edited)
        self.editingFinished.connect(self._settle)

    # suggestions ----------------------------------------------------------------------------------------
    def _fill(self, text: str) -> int:
        self._model.clear()
        self._listed = text                                                  # (what the list on offer was made for)
        rows = stations.search(text, 7)
        for s in rows:
            it = QStandardItem(s.label)
            it.setData(s.code, CODE_ROLE)
            it.setData(s.airport, AIRPORT_ROLE)
            it.setData(s.state, STATE_ROLE)
            self._model.appendRow(it)
        return len(rows)

    def _show_list(self, text: str) -> None:
        if self._fill(text):
            self._completer.setCompletionPrefix("")
            self._completer.complete()
            pop = self._completer.popup()
            if pop is not None:
                # the first suggestion is ready: Enter takes it. (Qt copies a highlighted suggestion INTO the box; the box must
                # keep what the person typed - otherwise "m" turns into "Mumbai (BOM)" and the next letter lands behind it.)
                keep, pos = self.text(), self.cursorPosition()
                pop.setCurrentIndex(self._model.index(0, 0))
                if self.text() != keep:
                    self.setText(keep)
                    self.setCursorPosition(pos)
        else:
            pop = self._completer.popup()
            if pop is not None:
                pop.hide()

    def _edited(self, text: str) -> None:
        if self._code is not None:                                         # the text no longer names that airport
            self._code = None
            self.changed.emit("")
        self._show_list(text)

    def focusInEvent(self, e) -> None:  # noqa: N802
        super().focusInEvent(e)
        if e.reason() != Qt.FocusReason.PopupFocusReason:
            QTimer.singleShot(0, self.selectAll)
            if not self.text().strip() or self._code is not None:
                QTimer.singleShot(0, lambda: self._show_list("" if self._code is not None else self.text()))

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() == Qt.Key.Key_Tab and self._completer.popup() is not None and self._completer.popup().isVisible():
            idx = self._completer.popup().currentIndex()
            if idx.isValid():
                self._picked(idx, advance=False)
        super().keyPressEvent(e)

    # choosing -------------------------------------------------------------------------------------------
    def _picked(self, index: QModelIndex, advance: bool = True) -> None:
        code = index.data(CODE_ROLE)
        if code:
            self.set_code(code)
            if advance:
                self.chosen.emit(code)

    def _settle(self) -> None:
        text = self.text().strip()
        if self._code and text == stations.label(self._code):
            return
        code = stations.resolve(text)
        if code:
            self.set_code(code)
        elif not text:
            self._set(None)
        elif self._listed == text and self._model.rowCount():
            # the person leaves the box (Tab, a click elsewhere) while suggestions for exactly this text were on offer:
            # the first suggestion is taken, as in any search box. The box then shows the full name, so it is never a surprise.
            self.set_code(self._model.index(0, 0).data(CODE_ROLE))

    def _set(self, code: Optional[str]) -> None:
        if code != self._code:
            self._code = code
            self.changed.emit(code or "")

    def set_code(self, code: Optional[str]) -> None:
        if not code or not stations.known(code):
            self.clear()
            self._set(None)
            return
        self.setText(stations.label(code))
        self._set(code)

    def code(self) -> Optional[str]:
        if self._code is None:
            self._settle()
        return self._code


# ── the date: a field that opens a calendar with IndiGo's lowest fare of every day ───────────────────────────────────
CAT_STYLE = {"L": ("#E5F5EE", "#BFE6D3", C["ok"]), "M": ("#FFF4DF", "#F3DFB2", C["warn"]), "H": ("#FDECEC", "#F3C4C4", C["bad"])}
WEEKDAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    m = d.year * 12 + (d.month - 1) + n
    return date(m // 12, m % 12 + 1, 1)


def price_text(p: float) -> str:
    return "₹" + f"{p:,.0f}"


class DayCell(QAbstractButton):
    """One day: the date number, and under it the lowest fare of that day, tinted by how cheap it is."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(58, 48)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.d: Optional[date] = None
        self.price: Optional[float] = None
        self.cat: Optional[str] = None
        self.enabled_day = False
        self.selected = False
        self.cheapest = False
        self.note = ""                                   # "" | "…" (loading) | "—" (no flight)

    def paintEvent(self, _e) -> None:  # noqa: N802
        if self.d is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        bg, edge, fg = "#FFFFFF", "#EEF0F7", C["text"]
        if not self.enabled_day:
            bg, edge, fg = "#F7F8FB", "#F7F8FB", "#B9BFD0"
        elif self.price is not None and self.cat in CAT_STYLE:
            bg, edge, fg = CAT_STYLE[self.cat][0], CAT_STYLE[self.cat][1], C["text"]
        if self.selected:
            bg, edge, fg = C["primary"], C["primary"], "#FFFFFF"
        elif self.underMouse() and self.enabled_day:
            edge = C["primary"]
        path = QPainterPath()
        path.addRoundedRect(r, 9, 9)
        p.fillPath(path, QColor(bg))
        p.setPen(QPen(QColor(edge), 1.4 if self.cheapest and not self.selected else 1))
        p.drawPath(path)
        f = QFont(self.font())
        f.setPointSizeF(10.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(fg))
        p.drawText(QRectF(r.left(), r.top() + 3, r.width(), 20), Qt.AlignmentFlag.AlignCenter, str(self.d.day))
        f2 = QFont(self.font())
        f2.setPointSizeF(7.8)
        p.setFont(f2)
        if self.price is not None:
            p.setPen(QColor("#FFFFFF" if self.selected else (CAT_STYLE.get(self.cat or "", ("", "", C["text2"]))[2])))
            p.drawText(QRectF(r.left(), r.top() + 23, r.width(), 18), Qt.AlignmentFlag.AlignCenter, price_text(self.price))
        elif self.note:
            p.setPen(QColor("#FFFFFF" if self.selected else "#B9BFD0"))
            p.drawText(QRectF(r.left(), r.top() + 23, r.width(), 18), Qt.AlignmentFlag.AlignCenter, self.note)
        if self.cheapest and not self.selected:
            p.setPen(QColor(C["ok"]))
            f3 = QFont(self.font())
            f3.setPointSizeF(8)
            p.setFont(f3)
            p.drawText(QRectF(r.right() - 14, r.top() + 1, 13, 13), Qt.AlignmentFlag.AlignCenter, "★")
        p.end()

    def enterEvent(self, e) -> None:  # noqa: N802
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(e)


class FareCalendar(QFrame):
    """A pop-up calendar of two months. While IndiGo's lowest fares load the days can already be picked; when they arrive each day
    shows its fare (green = low, amber = medium, red = high), days without a flight are greyed out, and the cheapest day is starred."""
    picked = pyqtSignal(object)         # a date
    need_fares = pyqtSignal(object)     # the first day of a window of fares the calendar would like to show

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("calpopup")                     # (a pop-up window cannot draw outside itself: its shadow was always cut off, so it has none - the border is its edge)
        self.first = date.today()
        self.last = date.today() + timedelta(days=330)
        self.selected: Optional[date] = None
        self.view = month_start(date.today())
        self.fares: dict[str, tuple[float, str]] = {}
        self.loaded: list[tuple[date, date]] = []          # windows of days IndiGo has answered for
        self.state = "idle"                                # idle | loading | ok | empty | error | busy
        self.route = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        head = QHBoxLayout()
        self.prev_btn = button("‹", "swap")
        self.next_btn = button("›", "swap")
        self.prev_btn.clicked.connect(lambda: self._go(-1))
        self.next_btn.clicked.connect(lambda: self._go(1))
        self.title1, self.title2 = label("", "cardtitle"), label("", "cardtitle")
        head.addWidget(self.prev_btn)
        head.addStretch(1)
        head.addWidget(self.title1)
        head.addSpacing(250)
        self.head = head
        head.addWidget(self.title2)
        head.addStretch(1)
        head.addWidget(self.next_btn)
        lay.addLayout(head)
        months = QHBoxLayout()
        months.setSpacing(22)
        self.cells: list[list[DayCell]] = []
        self.month_boxes: list[QWidget] = []
        self.months_shown = 2                              # two months side by side; ONE on a screen too narrow for both (see popup_at)
        for _m in range(2):
            grid = QGridLayout()
            grid.setSpacing(3)
            for i, wd in enumerate(WEEKDAYS):
                lb = label(wd, "muted")
                lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
                grid.addWidget(lb, 0, i)
            block = []
            for k in range(42):
                c = DayCell()
                keep = c.sizePolicy()
                keep.setRetainSizeWhenHidden(True)            # an unused cell keeps its place: both months are always six rows high
                c.setSizePolicy(keep)
                c.clicked.connect(lambda _x=False, cell=c: self._click(cell))
                grid.addWidget(c, 1 + k // 7, k % 7)
                block.append(c)
            self.cells.append(block)
            box = QWidget()
            box.setObjectName("clear")
            box.setLayout(grid)
            self.month_boxes.append(box)
            months.addWidget(box)
            months.setAlignment(box, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(months)
        self.status = label("", "small", wrap=True)
        lay.addWidget(self.status)
        foot = QHBoxLayout()
        self.legend = label("<span style='color:%s'>●</span> low &nbsp; <span style='color:%s'>●</span> medium &nbsp; "
                            "<span style='color:%s'>●</span> high &nbsp;&nbsp; ★ cheapest day" % (C["ok"], C["warn"], C["bad"]), "muted")
        foot.addWidget(self.legend)
        foot.addStretch(1)
        self.cheap_btn = button("", "secondary")
        self.cheap_btn.clicked.connect(self._pick_cheapest)
        self.cheap_btn.hide()
        foot.addWidget(self.cheap_btn)
        lay.addLayout(foot)
        lay.addWidget(label("Lowest fare of each day as IndiGo shows it (it can include nearby airports). The booking uses the best fare for exactly your airports, and your offer is applied then.", "muted", wrap=True))
        self.render()

    # data -----------------------------------------------------------------------------------------------
    def configure(self, first: date, last: date, selected: Optional[date]) -> None:
        self.first, self.last, self.selected = first, last, selected
        base = selected or first
        self.view = month_start(max(first, base))
        self.render()

    def reset_fares(self, route: str = "") -> None:
        self.fares, self.loaded, self.route = {}, [], route
        self.state = "idle"
        self.render()

    def set_loading(self) -> None:
        self.state = "loading"
        self.render()

    def set_fares(self, start: date, days: int, fares: dict, empty: bool = False, error: Optional[str] = None) -> None:
        if error:
            self.state = "busy" if error == "busy" else "error"
        elif empty and not fares:
            self.state = "empty"
        else:
            self.state = "ok"
            for d, v in fares.items():
                self.fares[d] = (float(v[0]), str(v[1]).upper())
            self.loaded.append((start, start + timedelta(days=days - 1)))
        self.render()

    def has_loaded(self, d: date) -> bool:
        return any(a <= d <= b for a, b in self.loaded)

    def cheapest_day(self) -> Optional[tuple[date, float]]:
        best = None
        for iso, (price, _c) in self.fares.items():
            d = date.fromisoformat(iso)
            if self.first <= d <= self.last and (best is None or (price, d) < (best[1], best[0])):
                best = (d, price)
        return best

    # painting -------------------------------------------------------------------------------------------
    def render(self) -> None:
        cheapest = self.cheapest_day() if self.state == "ok" else None
        for m in range(2):
            ms = add_months(self.view, m)
            (self.title1 if m == 0 else self.title2).setText(ms.strftime("%B %Y"))
            first_wd, ndays = pycal.monthrange(ms.year, ms.month)           # Monday = 0
            for k, c in enumerate(self.cells[m]):
                day_n = k - first_wd + 1
                if not 1 <= day_n <= ndays:
                    c.d = None
                    c.hide()
                    continue
                c.show()
                d = ms.replace(day=day_n)
                c.d = d
                inside = self.first <= d <= self.last
                key = d.isoformat()
                c.price = c.cat = None
                c.note = ""
                c.enabled_day = inside
                if inside and self.state == "loading" and not self.has_loaded(d):
                    c.note = "…"
                elif inside and self.state == "ok" and self.has_loaded(d):
                    if key in self.fares:
                        c.price, c.cat = self.fares[key]
                    else:
                        c.enabled_day = False
                        c.note = "—"
                c.selected = self.selected == d
                c.cheapest = bool(cheapest and cheapest[0] == d)
                c.setEnabled(c.enabled_day)
                c.update()
        self.prev_btn.setEnabled(self.view > month_start(self.first))
        self.next_btn.setEnabled(add_months(self.view, self.months_shown) <= month_start(self.last) + timedelta(days=1))
        msg = {"loading": "Loading fares…", "empty": "IndiGo shows no fares for this route here — you can still pick any date; the flights are listed when you book.",
               "error": "Fares are not available right now — you can still pick a date.",
               "busy": "Fares will show a little later — you can still pick a date."}.get(self.state, "")
        self.status.setText(msg)
        self.status.setVisible(bool(msg))
        self.legend.setVisible(self.state == "ok")
        if cheapest:
            self.cheap_btn.setText(f"Cheapest: {cheapest[0].strftime('%a %d %b')} · {price_text(cheapest[1])}")
            self.cheap_btn.show()
        else:
            self.cheap_btn.hide()

    # moving / picking -----------------------------------------------------------------------------------
    def _go(self, n: int) -> None:
        self.view = add_months(self.view, n)
        self.render()
        self.ask_for_view()

    def ask_for_view(self) -> None:
        """Fares for the month(s) on screen: ask for any window IndiGo has not answered for yet."""
        for m in range(self.months_shown):
            ms = add_months(self.view, m)
            lo, hi = max(ms, self.first), min(add_months(ms, 1) - timedelta(days=1), self.last)
            if lo > hi:
                continue
            d = lo
            while d <= hi:
                if not self.has_loaded(d):
                    self.need_fares.emit(d)
                    return
                d += timedelta(days=1)

    def _click(self, cell: DayCell) -> None:
        if cell.d is not None and cell.enabled_day:
            self.selected = cell.d
            self.render()
            self.picked.emit(cell.d)
            self.close()

    def _pick_cheapest(self) -> None:
        best = self.cheapest_day()
        if best:
            self.selected = best[0]
            self.render()
            self.picked.emit(best[0])
            self.close()

    def show_months(self, n: int) -> None:
        """One month on a narrow screen, two on a wide one (the pop-up is then about 460 or 910 pixels wide)."""
        n = 2 if n >= 2 else 1
        self.months_shown = n
        self.month_boxes[1].setVisible(n == 2)
        self.title2.setVisible(n == 2)
        gap = self.head.itemAt(3)
        if gap is not None:
            gap.changeSize(250 if n == 2 else 0, 0)
        self.head.invalidate()
        self.layout().activate()

    def popup_at(self, anchor: QWidget) -> None:
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        self.show_months(2 if screen is None or screen.availableGeometry().width() >= 1000 else 1)
        self.adjustSize()
        if screen is not None:
            g = screen.availableGeometry()
            pos.setX(max(g.left() + 8, min(pos.x(), g.right() - self.width() - 8)))
            pos.setY(max(g.top() + 8, min(pos.y(), g.bottom() - self.height() - 8)))
        self.move(pos)
        self.show()


class DateField(QPushButton):
    """The travel date: shows it like 'Fri, 28 Nov 2026' and opens the fare calendar when pressed."""
    changed = pyqtSignal(object)            # the new date
    calendar_opened = pyqtSignal()

    def __init__(self, first: date, last: date, start: date):
        super().__init__()
        self.setObjectName("datefield")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._first, self._last, self._day = first, last, start
        self.calendar = FareCalendar(self)
        self.calendar.picked.connect(self._picked)
        self.clicked.connect(self.open_calendar)
        self._show()

    def _show(self) -> None:
        self.setText(f"📅   {forms.day_long(self._day)}")

    def date(self) -> date:
        return self._day

    def set_date(self, d: date, emit: bool = True) -> None:
        d = max(self._first, min(self._last, d))
        if d != self._day:
            self._day = d
            self._show()
            if emit:
                self.changed.emit(d)

    def set_range(self, first: date, last: date) -> None:
        self._first, self._last = first, last
        self.set_date(self._day, emit=False)

    def _picked(self, d: date) -> None:
        self._day = d
        self._show()
        self.changed.emit(d)

    def open_calendar(self) -> None:
        self.calendar.configure(self._first, self._last, self._day)
        self.calendar.popup_at(self)
        self.calendar_opened.emit()
        self.calendar.ask_for_view()


# ── travellers ───────────────────────────────────────────────────────────────────────────────────────
NAME_CHARS = QRegularExpression(r"[A-Za-z .'\-]{0,32}")


class UpperEdit(QLineEdit):
    """Names go to IndiGo in capitals, as on the ID: show them that way while typing."""

    def __init__(self, placeholder: str):
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setValidator(QRegularExpressionValidator(NAME_CHARS, self))
        self.textEdited.connect(self._upper)

    def _upper(self, text: str) -> None:
        up = text.upper()
        if up != text:
            pos = self.cursorPosition()
            self.setText(up)
            self.setCursorPosition(pos)


class DobEdit(QLineEdit):
    """A date of birth, typed as digits: the slashes put themselves in (25032019 -> 25/03/2019)."""

    def __init__(self):
        super().__init__()
        self.setPlaceholderText("DD/MM/YYYY")
        self.setMaxLength(10)
        self.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9/.\-]{0,10}"), self))
        self.textEdited.connect(self._format)

    def _format(self, text: str) -> None:
        digits = "".join(ch for ch in text if ch.isdigit())[:8]
        out = digits[:2] + (("/" + digits[2:4]) if len(digits) > 2 else "") + (("/" + digits[4:]) if len(digits) > 4 else "")
        if text.endswith(("/", ".", "-")) and len(digits) in (2, 4):                  # (the traveller typed the separator himself)
            out += "/"
        if out != text:
            self.setText(out)
            self.setCursorPosition(len(out))


KIND_CHOICES = ((forms.ADULT, "Adult", "12 years and older"), (forms.CHILD, "Child", "2 to 11 years: has a seat, needs a date of birth"),
                (forms.INFANT, "Infant", "Under 2 years: rides on an adult's lap, needs a name and a date of birth"))


class TravellerRow(QFrame):
    removed = pyqtSignal(object)
    edited = pyqtSignal()
    kind_changed = pyqtSignal()
    resized = pyqtSignal()                                      # the row's width changed: the page decides how all the rows stand (one line or two)
    measured = pyqtSignal()                                     # the row knows what each of its two arrangements needs

    def __init__(self, index: int, kind: str = forms.ADULT):
        super().__init__()
        self.setObjectName("clear")
        self._travel_day: Optional[date] = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        row = QGridLayout()
        row.setHorizontalSpacing(10)
        row.setVerticalSpacing(8)
        self.grid = row
        self.compact = False
        self.num = label("", "stepnum")
        self.num.setStyleSheet(f"background: #F1F3FA; color: {C['text2']};")
        kseg = QHBoxLayout()                                       # who is this: an adult, a child (own seat) or an infant (on an adult's lap)
        kseg.setSpacing(0)
        self.kind_group = QButtonGroup(self)
        self.kind_btns: dict[str, QPushButton] = {}
        for i, (k, text, tip) in enumerate(KIND_CHOICES):
            b = button(text, "seg", tip)
            b.setCheckable(True)
            b.setProperty("pos", "left" if i == 0 else ("right" if i == len(KIND_CHOICES) - 1 else "mid"))
            self.kind_group.addButton(b)
            self.kind_btns[k] = b
            kseg.addWidget(b)
            b.toggled.connect(lambda on, _k=k: on and self._kind_changed(_k))
        self.kseg = kseg
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.male = button("Male", "seg")
        self.female = button("Female", "seg")
        self.male.setProperty("pos", "left")
        self.female.setProperty("pos", "right")
        self.group = QButtonGroup(self)
        for b in (self.male, self.female):
            b.setCheckable(True)
            self.group.addButton(b)
            seg.addWidget(b)
            b.toggled.connect(lambda _on: self.edited.emit())
        self.seg = seg
        self.first = UpperEdit("First name (as on ID)")
        self.last = UpperEdit("Surname")
        for e in (self.first, self.last):
            e.setMinimumWidth(96)
            e.textChanged.connect(lambda _t: self.edited.emit())
        self.names = QWidget()                                     # first name + surname: one box that moves as a whole (next to the buttons, or under them)
        self.names.setObjectName("clear")
        self.names_lay = QHBoxLayout(self.names)
        self.names_lay.setContentsMargins(0, 0, 0, 0)
        self.names_lay.setSpacing(10)
        self.names_lay.addWidget(self.first, 3)
        self.names_lay.addWidget(self.last, 2)
        self.rm = button("✕", "remove", "Remove this traveller")
        self.rm.clicked.connect(lambda: self.removed.emit(self))
        v.addLayout(row)
        # a child's and an infant's date of birth (IndiGo asks for it and checks the age on the day of the flight)
        self.dob_row = QWidget()
        self.dob_row.setObjectName("clear")
        dl = QHBoxLayout(self.dob_row)
        self.dob_lay = dl
        dl.setContentsMargins(36, 0, 0, 0)                         # (under the first name; at the left edge when the row stands in two lines)
        dl.setSpacing(10)
        self.dob_caption = label("Date of birth", "muted")
        self.dob = DobEdit()
        self.dob.setFixedWidth(150)
        self.dob.textChanged.connect(lambda _t: (set_error(self.dob, False), self._age_hint(), self.edited.emit()))
        self.age = label("", "muted", wrap=True)                  # (wraps when there is little room, instead of making the whole row wider)
        dl.addWidget(self.dob_caption)
        dl.addWidget(self.dob)
        dl.addWidget(self.age, 1)
        v.addWidget(self.dob_row)
        self.err = ErrorLabel()
        self.err.setContentsMargins(36, 0, 0, 0)
        v.addWidget(self.err)
        self.set_index(index)
        self.set_kind(kind)
        self.wide_need = self._compact_need = 0                    # what each arrangement needs at least (measured when the row is first shown)
        self._measured = False
        self.grid.addWidget(self.num, 0, 0)
        self.grid.addLayout(self.kseg, 0, 1, Qt.AlignmentFlag.AlignLeft)
        self.grid.addLayout(self.seg, 0, 2, Qt.AlignmentFlag.AlignLeft)
        self._arrange(False)

    HYSTERESIS = 24

    def _arrange(self, compact: bool) -> None:
        """Wide: number, who, gender, the two names, ✕ in ONE line. Narrow: number, who, gender, ✕ in the first line, the two names under them."""
        g = self.grid
        g.removeWidget(self.names)
        g.removeWidget(self.rm)
        self.compact = compact
        for b in list(self.kind_btns.values()) + [self.male, self.female]:
            repolish(b, compact=compact)
        self.dob_lay.setContentsMargins(0 if compact else 36, 0, 0, 0)
        self.err.setContentsMargins(0 if compact else 36, 0, 0, 0)
        for col in range(5):
            g.setColumnStretch(col, 0)
        if not compact:
            g.addWidget(self.names, 0, 3)
            g.addWidget(self.rm, 0, 4)
            g.setColumnStretch(3, 1)
            self.names_lay.setStretch(0, 3)
            self.names_lay.setStretch(1, 2)
        else:
            g.addWidget(self.rm, 0, 3, Qt.AlignmentFlag.AlignRight)
            g.addWidget(self.names, 1, 0, 1, 4)
            g.setColumnStretch(3, 1)
            self.names_lay.setStretch(0, 1)
            self.names_lay.setStretch(1, 1)

    def set_compact(self, compact: bool) -> None:
        if compact != self.compact:
            self._arrange(compact)

    def _measure(self) -> None:
        """What each arrangement needs at least. Measured live, once the row is shown (fonts and style sheet are known by then; sizes remembered from before
        that are forgotten first)."""
        for ch in self.findChildren(QWidget):
            ch.updateGeometry()
        was = self.compact
        self._arrange(False)
        self.wide_need = QFrame.minimumSizeHint(self).width()
        self._arrange(True)
        self._compact_need = QFrame.minimumSizeHint(self).width()
        self._arrange(was)
        self._measured = True
        self.updateGeometry()
        self.measured.emit()

    @property
    def is_measured(self) -> bool:
        return self._measured

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        """The row can stand in one line or in two; it asks for the room the two-line way needs, so the window around it can get narrow enough for it to give way."""
        s = super().minimumSizeHint()
        return QSize(self._compact_need, s.height()) if self._measured else s

    def showEvent(self, e) -> None:  # noqa: N802 (Qt name)
        super().showEvent(e)
        if not self._measured:
            self._measure()

    def resizeEvent(self, e) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(e)
        self.resized.emit()

    # who ----------------------------------------------------------------------------------------------
    @property
    def kind(self) -> str:
        return next((k for k, b in self.kind_btns.items() if b.isChecked()), forms.ADULT)

    def set_kind(self, kind: str) -> None:
        b = self.kind_btns.get(kind) or self.kind_btns[forms.ADULT]
        b.blockSignals(True)
        b.setChecked(True)
        b.blockSignals(False)
        self._show_dob()

    def _kind_changed(self, _kind: str) -> None:
        self._show_dob()
        self.err.show_text("")
        self.kind_changed.emit()
        self.edited.emit()

    def _show_dob(self) -> None:
        young = self.kind != forms.ADULT
        self.dob_row.setVisible(young)
        self.dob_caption.setText("Child's date of birth" if self.kind == forms.CHILD else "Baby's date of birth")
        self.first.setPlaceholderText("First name (as on ID)" if not young else "First name")
        self._age_hint()

    def set_travel_day(self, day: Optional[date]) -> None:
        self._travel_day = day
        self._age_hint()

    def _age_hint(self) -> None:
        """How old on the day of the flight - so a wrong year is seen while typing, not after Book."""
        born = forms.parse_dob(self.dob.text())
        if self.kind == forms.ADULT or born is None or self._travel_day is None or born > self._travel_day:
            self.age.setText("")
            return
        self.age.setText(f"= {forms.age_words(born, self._travel_day)} old on the travel date")

    def set_index(self, i: int) -> None:
        self.num.setText(str(i + 1))

    def value(self) -> forms.Traveller:
        g = "M" if self.male.isChecked() else ("F" if self.female.isChecked() else "")
        return forms.Traveller(first=self.first.text(), last=self.last.text(), gender=g, kind=self.kind,
                               dob=self.dob.text() if self.kind != forms.ADULT else "")

    def set_value(self, t: forms.Traveller) -> None:
        self.set_kind(t.kind)
        self.first.setText(t.first)
        self.last.setText(t.last)
        if t.dob:
            self.dob.setText(t.dob.replace("-", "/"))
        if t.gender == "M":
            self.male.setChecked(True)
        elif t.gender == "F":
            self.female.setChecked(True)

    def show_errors(self, errs: dict[str, str], i: int) -> list[QWidget]:
        bad: list[QWidget] = []
        msgs = []
        for key, w in (("dob", self.dob), ("gender", None), ("first", self.first), ("last", self.last)):
            m = errs.get(f"t{i}.{key}")
            if key == "gender":
                for b in (self.male, self.female):
                    set_error(b, bool(m))
                if m:
                    bad.append(self.male)
            elif w is not None:
                set_error(w, bool(m))
                if m:
                    bad.append(w)
            if m:
                msgs.append(m)
        self.err.show_text("  ·  ".join(msgs))
        return bad


# ── offers: one at a time ────────────────────────────────────────────────────────────────────────────
class OfferTile(QPushButton):
    """'Offer 1' / 'Offer 2' - and nothing about what the offer is."""

    def __init__(self, key: str):
        super().__init__(forms.OFFER_TITLE[key])
        self.key = key
        self.setObjectName("offer")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(58)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_available(self, on: bool) -> None:
        self.setEnabled(on)
        self.setText(forms.OFFER_TITLE[self.key] + ("" if on else "\nnot available right now"))


# ── progress ─────────────────────────────────────────────────────────────────────────────────────────
class StepList(QWidget):
    """The booking stages with ✓ (done), ● (now) and ○ (next)."""

    def __init__(self, stages: list[tuple[str, str]]):
        super().__init__()
        self.setObjectName("clear")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(9)
        self.rows: dict[str, tuple[QLabel, QLabel]] = {}
        self.order = [k for k, _ in stages]
        for key, text in stages:
            h = QHBoxLayout()
            h.setSpacing(8)
            ic = label("○", "stepicon")
            tx = label(text, "steptext", wrap=True)
            h.addWidget(ic)
            h.addWidget(tx, 1)
            v.addLayout(h)
            self.rows[key] = (ic, tx)
        self._pulse = 0
        self.active: Optional[str] = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    def reset(self) -> None:
        self.active = None
        for key in self.order:
            self._paint(key, "todo")
        self.timer.stop()

    def set_active(self, key: str) -> None:
        if key not in self.rows:
            return
        idx = self.order.index(key)
        for i, k in enumerate(self.order):
            self._paint(k, "done" if i < idx else ("active" if i == idx else "todo"))
        self.active = key
        if not self.timer.isActive():
            self.timer.start(450)

    def finish(self, ok: bool) -> None:
        self.timer.stop()
        if ok:
            for k in self.order:
                self._paint(k, "done")
        elif self.active:
            ic, _ = self.rows[self.active]
            ic.setText("✕")
            ic.setStyleSheet(f"color: {C['bad']};")

    def _paint(self, key: str, state: str) -> None:
        ic, tx = self.rows[key]
        ic.setText({"done": "✓", "active": "●", "todo": "○"}[state])
        ic.setStyleSheet(f"color: {C['ok'] if state == 'done' else (C['primary'] if state == 'active' else '#C2C8DA')};")
        repolish(tx, state=state)

    def _tick(self) -> None:
        if not self.active:
            return
        self._pulse = (self._pulse + 1) % 2
        ic, _ = self.rows[self.active]
        ic.setText("●" if self._pulse else "◌")


class Toast(QLabel):
    """A short message that fades in at the bottom of the window ("PNR copied")."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("toast")
        self.hide()
        self._t = QTimer(self)
        self._t.setSingleShot(True)
        self._t.timeout.connect(self.hide)

    def say(self, text: str, ms: int = 2200) -> None:
        self.setText(text)
        self.adjustSize()
        p = self.parentWidget()
        if p is not None:
            self.move((p.width() - self.width()) // 2, p.height() - self.height() - 28)
        self.raise_()
        self.show()
        self._t.start(ms)


def qdate(d) -> QDate:
    return QDate(d.year, d.month, d.day)


__all__ = ["Card", "DateField", "DayCell", "ErrorLabel", "FareCalendar", "OfferTile", "StationBox", "StepList", "Toast",
           "TravellerRow", "UpperEdit", "button", "divider", "field", "label", "qdate", "repolish", "set_error", "shadow"]
