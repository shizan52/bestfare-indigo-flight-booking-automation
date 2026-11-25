"""The look of Best Fare's own windows that are NOT the app: the update window, the "update ready" card, and the base the setup wizard is built on.

Plain code in the shell (it needs nothing from the program that arrives from the server). One small design language:
a rounded white card on a soft shadow, an indigo header with the logo, big calm text, one primary button per window.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from PyQt6.QtCore import QEasingCurve, QObject, QPoint, QPointF, QPropertyAnimation, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLayout, QProgressBar, QPushButton, QVBoxLayout, QWidget)

C = {"bg": "#F3F5FB", "card": "#FFFFFF", "line": "#E3E7F1", "text": "#1B2140", "text2": "#525B7A", "muted": "#8A93AD", "primary": "#3E45D0",
     "primary_hover": "#343BBB", "primary_down": "#2C329E", "soft": "#EEF0FF", "top": "#1D2170", "bottom": "#3138A8", "accent": "#F28A2E",
     "ok": "#13875A", "ok_soft": "#E5F5EE", "bad": "#C93C3C", "bad_soft": "#FDECEC"}
FONT = "Segoe UI"
MARGIN = 18                                    # room around the card for its shadow


class Cancelled(Exception):
    """The user chose to skip a download that was in progress."""


# ── small helpers ────────────────────────────────────────────────────────────────────────────────────────────────────
def load_fonts() -> None:
    """Register the Windows fonts by file: some Qt builds cannot list system fonts and would show empty labels."""
    win = os.environ.get("WINDIR")
    if not win:
        return
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "segoeuisl.ttf", "seguisym.ttf", "seguiemj.ttf", "consola.ttf"):
        path = os.path.join(win, "Fonts", name)
        if os.path.isfile(path):
            QFontDatabase.addApplicationFont(path)


def megabytes(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(n, 0) / 1024:.0f} KB"


def logo(size: int) -> QPixmap:
    """The Best Fare mark: an indigo rounded square, a white plane, an orange dot."""
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    px = int(size * ratio)
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, px, px)
    g.setColorAt(0, QColor("#4B53E8"))
    g.setColorAt(1, QColor("#2A2FA8"))
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, px, px), px * 0.24, px * 0.24)
    p.fillPath(path, g)
    p.setPen(QColor("#FFFFFF"))
    f = QFont("Segoe UI Symbol")
    f.setPixelSize(int(px * 0.60))
    p.setFont(f)
    p.drawText(QRectF(0, -px * 0.03, px, px), Qt.AlignmentFlag.AlignCenter, "✈")
    p.setBrush(QColor(C["accent"]))
    p.setPen(Qt.PenStyle.NoPen)
    r = px * 0.12
    p.drawEllipse(QRectF(px * 0.71, px * 0.15, r, r))
    p.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def _qss(check: str) -> str:
    return f"""
* {{ font-family: "{FONT}"; color: {C['text']}; }}
QFrame#win {{ background: {C['card']}; border: 1px solid {C['line']}; border-radius: 16px; }}
QFrame#head {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {C['top']}, stop:1 {C['bottom']}); border: none;
  border-top-left-radius: 16px; border-top-right-radius: 16px; border-bottom-left-radius: 0; border-bottom-right-radius: 0; }}
QFrame#head QLabel {{ background: transparent; color: white; }}
QLabel#brand {{ font-size: 15pt; font-weight: 700; }}
QLabel#tag {{ font-size: 9pt; color: rgba(255,255,255,0.78); }}
QLabel#h1 {{ font-size: 17pt; font-weight: 700; }}
QLabel#h2 {{ font-size: 12pt; font-weight: 600; }}
QLabel#body {{ font-size: 10pt; color: {C['text2']}; }}
QLabel#small {{ font-size: 9pt; color: {C['muted']}; }}
QLabel#err {{ font-size: 10pt; color: {C['bad']}; font-weight: 600; }}
QLabel#chip {{ background: {C['soft']}; color: {C['primary']}; border-radius: 11px; padding: 3px 12px; font-weight: 700; font-size: 10pt; }}
QLabel#chipNew {{ background: {C['primary']}; color: white; border-radius: 11px; padding: 3px 12px; font-weight: 700; font-size: 10pt; }}
QLabel#arrow {{ color: {C['muted']}; font-size: 13pt; }}
QFrame#notes {{ background: #F7F8FD; border: 1px solid {C['line']}; border-radius: 12px; }}
QFrame#notes QLabel {{ background: transparent; }}
QProgressBar {{ background: #E6E9F5; border: none; border-radius: 5px; min-height: 10px; max-height: 10px; text-align: center; }}
QProgressBar::chunk {{ border-radius: 5px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4B53E8, stop:1 #7A5CF0); }}
QPushButton {{ font-size: 10pt; font-weight: 600; border-radius: 10px; padding: 10px 22px; min-height: 20px; }}
QPushButton#primary {{ background: {C['primary']}; color: white; border: none; }}
QPushButton#primary:hover {{ background: {C['primary_hover']}; }}
QPushButton#primary:pressed {{ background: {C['primary_down']}; }}
QPushButton#primary:disabled {{ background: #B8BCE6; color: white; }}
QPushButton#secondary {{ background: white; color: {C['primary']}; border: 1px solid #C9CDF3; }}
QPushButton#secondary:hover {{ background: {C['soft']}; }}
QPushButton#ghost {{ background: transparent; color: {C['text2']}; border: none; }}
QPushButton#ghost:hover {{ background: #EEF0F8; }}
QPushButton#close {{ background: transparent; color: rgba(255,255,255,0.8); border: none; border-radius: 8px; padding: 2px 9px; font-size: 12pt; min-height: 16px; }}
QPushButton#close:hover {{ background: rgba(255,255,255,0.18); color: white; }}
QLabel[dot="pending"] {{ background: #E6E9F5; color: {C['muted']}; border-radius: 13px; font-weight: 700; font-size: 9pt; }}
QLabel[dot="active"] {{ background: {C['primary']}; color: white; border-radius: 13px; font-weight: 700; font-size: 9pt; }}
QLabel[dot="done"] {{ background: {C['ok']}; color: white; border-radius: 13px; font-weight: 700; font-size: 9pt; }}
QLabel#stepname {{ font-size: 9pt; color: {C['muted']}; }}
QLabel#stepname[step="active"] {{ color: {C['primary']}; font-weight: 700; }}
QLabel#stepname[step="done"] {{ color: {C['ok']}; font-weight: 600; }}
QLabel#bigcheck {{ background: {C['ok_soft']}; color: {C['ok']}; border-radius: 34px; font-size: 30pt; font-weight: 700; }}
QLabel#bigbad {{ background: {C['bad_soft']}; color: {C['bad']}; border-radius: 34px; font-size: 30pt; font-weight: 700; }}
QLineEdit {{ background: #FBFBFE; border: 1px solid #D7DBEC; border-radius: 10px; padding: 9px 12px; font-size: 10pt; selection-background-color: {C['primary']}; }}
QLineEdit:focus {{ border: 1px solid {C['primary']}; background: white; }}
QCheckBox {{ font-size: 10pt; spacing: 10px; color: {C['text']}; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1.5px solid #B9BFDA; background: white; }}
QCheckBox::indicator:hover {{ border-color: {C['primary']}; }}
QCheckBox::indicator:checked {{ background: {C['primary']}; border-color: {C['primary']}; image: url({check}); }}
"""


def check_icon() -> str:
    """The white tick inside a ticked box (a style sheet can only show an image, so one is drawn once and kept in the temp folder). '' if it cannot be written."""
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "bestfare-ui", "tick.png")
    try:
        if not os.path.isfile(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            pm = QPixmap(36, 36)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor("#FFFFFF"))
            pen.setWidthF(4.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPolyline([QPointF(9, 18.5), QPointF(15.5, 25), QPointF(27.5, 11.5)])
            p.end()
            pm.save(path, "PNG")
        return path.replace("\\", "/")
    except Exception:  # noqa: BLE001
        return ""


_style_cache: dict = {}


def style() -> str:
    """The whole style sheet of Best Fare's own windows."""
    if "q" not in _style_cache:
        _style_cache["q"] = _qss(check_icon())
    return _style_cache["q"]


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


class Window(QDialog):
    """A frameless rounded window on a soft shadow with the indigo header. Subclasses fill `self.body`. The header drags the window."""

    def __init__(self, title: str, tagline: str = "", width: int = 540, closable: bool = True):
        super().__init__()
        self.setWindowTitle("Best Fare")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(style())
        self._drag: Optional[QPoint] = None
        self._closable = closable
        outer = QVBoxLayout(self)
        outer.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)          # (the window is always exactly as tall as what it shows)
        self.win = QFrame()
        self.win.setObjectName("win")
        self.win.setMinimumWidth(width)
        self.win.setMaximumWidth(width)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(20, 24, 70, 90))
        self.win.setGraphicsEffect(shadow)
        outer.addWidget(self.win)
        col = QVBoxLayout(self.win)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.head = QFrame()
        self.head.setObjectName("head")
        h = QHBoxLayout(self.head)
        h.setContentsMargins(22, 16, 14, 16)
        h.setSpacing(14)
        mark = QLabel()
        mark.setPixmap(logo(44))
        mark.setFixedSize(44, 44)
        h.addWidget(mark)
        names = QVBoxLayout()
        names.setSpacing(1)
        b = QLabel(title)
        b.setObjectName("brand")
        self.tagline = QLabel(tagline)
        self.tagline.setObjectName("tag")
        names.addWidget(b)
        names.addWidget(self.tagline)
        h.addLayout(names, 1)
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("close")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.reject)
        self.close_btn.setVisible(closable)
        h.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignTop)
        col.addWidget(self.head)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(26, 22, 26, 22)
        self.body.setSpacing(12)
        col.addLayout(self.body)

    def reject(self) -> None:                          # (Esc and the ✕ must not close a window that may not be closed)
        if self._closable:
            super().reject()

    def set_closable(self, yes: bool) -> None:
        self._closable = yes
        self.close_btn.setVisible(yes)

    def closeEvent(self, e) -> None:
        if not self._closable:
            e.ignore()
            return
        super().closeEvent(e)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.head.geometry().translated(self.win.pos()).contains(e.position().toPoint()):
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        self._drag = None
        super().mouseReleaseEvent(e)

    def center(self) -> None:
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            self.move(g.center().x() - self.width() // 2, g.center().y() - self.height() // 2)


def label(text: str, name: str = "body", wrap: bool = True) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


def button(text: str, kind: str = "primary") -> QPushButton:
    b = QPushButton(text)
    b.setObjectName(kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class Steps(QWidget):
    """Three (or more) numbered dots joined in a row: done / now / later."""

    def __init__(self, names: list[str]):
        super().__init__()
        self.dots: list[QLabel] = []
        self.names: list[QLabel] = []
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        for i, n in enumerate(names):
            cell = QHBoxLayout()
            cell.setSpacing(8)
            dot = QLabel(str(i + 1))
            dot.setFixedSize(26, 26)
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name = QLabel(n)
            name.setObjectName("stepname")
            self.dots.append(dot)
            self.names.append(name)
            cell.addWidget(dot)
            cell.addWidget(name)
            row.addLayout(cell)
            if i < len(names) - 1:
                line = QFrame()
                line.setFixedHeight(2)
                line.setStyleSheet(f"background: {C['line']}; border: none;")
                row.addWidget(line, 1)
                row.setContentsMargins(0, 0, 0, 0)
        self.set(0)

    def set(self, active: int) -> None:
        """Steps before `active` are done, `active` is in progress, the rest wait. active == len(names) means all done."""
        for i, (d, n) in enumerate(zip(self.dots, self.names)):
            state = "done" if i < active else "active" if i == active else "pending"
            d.setProperty("dot", state)
            n.setProperty("step", state)
            d.setText("✓" if state == "done" else str(i + 1))
            repolish(d)
            repolish(n)


class Meter(QWidget):
    """A progress bar with 'x MB of y MB · speed · time left' under it."""

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        v.addWidget(self.bar)
        row = QHBoxLayout()
        self.left = QLabel("")
        self.left.setObjectName("small")
        self.right = QLabel("")
        self.right.setObjectName("small")
        row.addWidget(self.left, 1)
        row.addWidget(self.right)
        v.addLayout(row)
        self._t0 = time.monotonic()
        self._speed = 0.0
        self._last = (self._t0, 0)

    def reset(self) -> None:
        self._t0 = time.monotonic()
        self._speed = 0.0
        self._last = (self._t0, 0)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.left.setText("")
        self.right.setText("")

    def indeterminate(self, text: str = "") -> None:
        self.bar.setRange(0, 0)
        self.left.setText(text)
        self.right.setText("")

    def set(self, done: int, total: int) -> None:
        now = time.monotonic()
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(min(1000, int(1000 * done / total)))
        else:
            self.bar.setRange(0, 0)
        t, d0 = self._last
        if now - t >= 0.4:
            inst = (done - d0) / max(now - t, 1e-3)
            self._speed = inst if self._speed == 0 else 0.7 * self._speed + 0.3 * inst
            self._last = (now, done)
        if total > 0:
            self.left.setText(f"{megabytes(done)} of {megabytes(total)}")
            if self._speed > 1024 and done < total:
                secs = int((total - done) / self._speed)
                self.right.setText(f"{megabytes(self._speed)}/s · " + (f"about {secs} s left" if secs < 90 else f"about {secs // 60 + 1} min left"))
            elif done >= total:
                self.right.setText("")
        else:
            self.left.setText(megabytes(done))


class Runner(QObject):
    """Runs one blocking job off the window's thread; its progress and its end come back as signals (queued onto the window's thread)."""
    progressed = pyqtSignal(int, int)
    phased = pyqtSignal(str)                            # a job may also say what it is doing: progress.phase("Copying files")
    finished = pyqtSignal(object, object)               # (result, error)

    def __init__(self):
        super().__init__()
        self.cancelled = False

    def run(self, job: Callable[[Callable[[int, int], None]], object]) -> None:
        self.cancelled = False

        def progress(done: int, total: int) -> None:
            if self.cancelled:
                raise Cancelled()
            self.progressed.emit(int(done), int(total))

        progress.phase = lambda text: self.phased.emit(str(text))          # type: ignore[attr-defined]

        def go() -> None:
            try:
                self.finished.emit(job(progress), None)
            except BaseException as e:  # noqa: BLE001
                self.finished.emit(None, e)
        threading.Thread(target=go, name="update-job", daemon=True).start()


# ── the update window ────────────────────────────────────────────────────────────────────────────────────────────────
def notes_lines(notes: str, most: int = 7) -> list[str]:
    out = []
    for raw in (notes or "").replace("\r", "").replace("•", "\n").replace(";", "\n").split("\n"):
        t = raw.strip(" -*·\t")
        if t:
            out.append(t[:140])
    return out[:most]


class UpdateDialog(Window):
    """'Best Fare 1.3.0 is available': what is new, then download → verify → install with a calm progress bar. One primary button at a time."""
    primary_clicked = pyqtSignal()
    secondary_clicked = pyqtSignal()

    def __init__(self, *, title: str = "Updating Best Fare", tagline: str = "", current: str = "", new: str = "", notes: str = "",
                 steps: tuple = ("Download", "Verify", "Install"), mandatory: bool = False):
        super().__init__("Best Fare", tagline or "Always up to date", closable=not mandatory)
        self.mandatory = mandatory
        self.title = label(title, "h1", wrap=True)
        self.body.addWidget(self.title)
        if new:
            row = QHBoxLayout()
            row.setSpacing(10)
            if current:
                c = QLabel(current)
                c.setObjectName("chip")
                a = QLabel("→")
                a.setObjectName("arrow")
                row.addWidget(c)
                row.addWidget(a)
            n = QLabel(new)
            n.setObjectName("chipNew")
            row.addWidget(n)
            row.addStretch(1)
            self.body.addLayout(row)
        self.lines = notes_lines(notes)
        if self.lines:
            box = QFrame()
            box.setObjectName("notes")
            v = QVBoxLayout(box)
            v.setContentsMargins(16, 12, 16, 12)
            v.setSpacing(5)
            v.addWidget(label("What's new", "h2"))
            for ln in self.lines:
                v.addWidget(label("•  " + ln, "body"))
            self.body.addWidget(box)
        self.text = label("", "body")
        self.body.addWidget(self.text)
        self.steps = Steps(list(steps))
        self.body.addWidget(self.steps)
        self.meter = Meter()
        self.body.addWidget(self.meter)
        self.problem = label("", "err")
        self.problem.hide()
        self.body.addWidget(self.problem)
        row = QHBoxLayout()
        self.secondary = button("Later", "secondary")
        self.primary = button("Update now", "primary")
        row.addStretch(1)
        row.addWidget(self.secondary)
        row.addWidget(self.primary)
        self.body.addSpacing(4)
        self.body.addLayout(row)
        self.primary.clicked.connect(self.primary_clicked)
        self.secondary.clicked.connect(self.secondary_clicked)
        self.adjustSize()
        self.center()

    # the states
    def ask(self, text: str, primary: str = "Update now", secondary: Optional[str] = "Later") -> None:
        self.text.setText(text)
        self.problem.hide()
        self.steps.hide()
        self.meter.hide()
        self.primary.setText(primary)
        self.primary.show()
        self.primary.setEnabled(True)
        self.secondary.setText(secondary or "")
        self.secondary.setVisible(bool(secondary) and not self.mandatory)
        self._resize()

    def working(self, text: str = "", cancel: Optional[str] = None) -> None:
        self.text.setText(text)
        self.problem.hide()
        self.steps.show()
        self.steps.set(0)
        self.meter.show()
        self.meter.reset()
        self.primary.hide()
        self.secondary.setText(cancel or "")
        self.secondary.setVisible(bool(cancel) and not self.mandatory)
        self._resize()

    def progress(self, done: int, total: int) -> None:
        self.meter.set(done, total)
        if total > 0 and done >= total:
            self.steps.set(1)

    def step(self, i: int) -> None:
        self.steps.set(i)

    def show_done(self, text: str, primary: Optional[str] = None) -> None:
        self.text.setText(text)
        self.problem.hide()
        self.steps.set(len(self.steps.dots))
        self.meter.bar.setRange(0, 1000)
        self.meter.bar.setValue(1000)
        self.meter.left.setText("")
        self.meter.right.setText("")
        self.secondary.hide()
        self.primary.setVisible(bool(primary))
        if primary:
            self.primary.setText(primary)
            self.primary.setEnabled(True)
        self._resize()

    def failed(self, text: str, primary: str = "Try again", secondary: Optional[str] = "Close") -> None:
        self.problem.setText(text)
        self.problem.show()
        self.text.setText("")
        self.steps.hide()
        self.meter.hide()
        self.primary.setText(primary)
        self.primary.show()
        self.secondary.setText(secondary or "")
        self.secondary.setVisible(bool(secondary))
        self._resize()

    def _resize(self) -> None:
        self.layout().activate()
        self.adjustSize()
        self.center()


def run_update(dlg: UpdateDialog, job: Callable, *, ask: Optional[str] = None, ask_button: str = "Update now", skip_button: Optional[str] = "Later",
               working_text: str = "", cancel_button: Optional[str] = None, done_text: str = "", done_button: Optional[str] = None,
               close_after_ms: int = 900, phases: Optional[dict] = None) -> tuple[str, object]:
    """Drive `dlg` through ask → work → done on the calling (GUI) thread. Returns ("ok", result) | ("later", None) | ("cancelled", None) |
    ("error", exception) | ("closed", None). `job(progress)` runs on a thread. With `done_button` the window waits for a click once the work is done."""
    runner = Runner()
    box: dict = {"outcome": ("closed", None)}

    def start() -> None:
        box["outcome"] = ("closed", None)
        dlg.working(working_text, cancel_button)
        runner.run(job)

    def finished(result, error) -> None:
        if error is None:
            box["outcome"] = ("ok", result)
            dlg.show_done(done_text, done_button)
            if not done_button:
                QTimer.singleShot(close_after_ms, dlg.accept)
            return
        if isinstance(error, Cancelled):
            box["outcome"] = ("cancelled", None)
            dlg.accept()
            return
        box["outcome"] = ("error", error)
        msg = getattr(error, "message", None) or str(error) or "Something went wrong."
        dlg.failed(msg if len(msg) < 220 else msg[:217] + "…", "Try again", "Quit" if dlg.mandatory else "Close")

    def primary() -> None:
        kind = box["outcome"][0]
        if kind == "ok":
            dlg.accept()                                  # the finished state's button ("Restart now")
        else:
            start()                                       # the first ask, or "Try again"

    def secondary() -> None:
        if box["outcome"][0] == "error":
            dlg.set_closable(True)                        # (a window that may not be closed can still be left from its error: nobody is trapped)
            dlg.reject()
        elif dlg.meter.isVisible() and cancel_button:
            runner.cancelled = True                       # (the next progress call raises Cancelled)
        else:
            box["outcome"] = ("later", None)
            dlg.reject()

    runner.progressed.connect(dlg.progress)
    if phases:
        runner.phased.connect(lambda name: dlg.step(phases.get(name, 0)))          # (a job says "Installing" -> the step dot moves)
    runner.finished.connect(finished)
    dlg.primary_clicked.connect(primary)
    dlg.secondary_clicked.connect(secondary)
    if ask is not None:
        box["outcome"] = ("later", None)
        dlg.ask(ask, ask_button, skip_button)
    else:
        QTimer.singleShot(0, start)
    dlg.exec()
    return box["outcome"]


# ── the "update ready" card ────────────────────────────────────────────────────────────────────────────────────────────
class ReadyCard(QDialog):
    """A small card in the corner of the screen: 'Best Fare 1.3.0 is ready - restart to use it'. Never takes the focus away from a booking."""
    restart_clicked = pyqtSignal()
    later_clicked = pyqtSignal()

    def __init__(self, version: str, notes: str = "", what: str = "Best Fare"):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(style())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        win = QFrame()
        win.setObjectName("win")
        win.setFixedWidth(400)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(20, 24, 70, 95))
        win.setGraphicsEffect(shadow)
        outer.addWidget(win)
        v = QVBoxLayout(win)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(12)
        mark = QLabel()
        mark.setPixmap(logo(40))
        mark.setFixedSize(40, 40)
        top.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        t = QVBoxLayout()
        t.setSpacing(2)
        t.addWidget(label("Update ready", "h2"))
        self.text = label(f"{what} {version} has been downloaded. Restart to start using it.", "body")
        t.addWidget(self.text)
        top.addLayout(t, 1)
        v.addLayout(top)
        lines = notes_lines(notes, 3)
        for ln in lines:
            v.addWidget(label("•  " + ln, "small"))
        row = QHBoxLayout()
        row.addStretch(1)
        later = button("Later", "ghost")
        self.go = button("Restart now", "primary")
        row.addWidget(later)
        row.addWidget(self.go)
        v.addLayout(row)
        later.clicked.connect(self.later_clicked)
        self.go.clicked.connect(self.restart_clicked)
        self.adjustSize()

    def show_in_corner(self) -> None:
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            self.move(g.right() - self.width() - 6, g.bottom() - self.height() - 6)
        self.setWindowOpacity(0.0)
        self.show()
        a = QPropertyAnimation(self, b"windowOpacity", self)
        a.setDuration(260)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.start()
        self._anim = a

    def waiting(self, text: str) -> None:
        self.text.setText(text)
        self.go.setEnabled(False)
        self.adjustSize()
