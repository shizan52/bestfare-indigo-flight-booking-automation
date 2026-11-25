"""Look and feel: one calm, light theme with IndiGo-like indigo, big readable text and clear states."""
from __future__ import annotations

import os

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap

C = {
    "bg": "#F3F5FB", "card": "#FFFFFF", "line": "#E3E7F1", "line2": "#D3D9E8",
    "text": "#1B2140", "text2": "#525B7A", "muted": "#8A93AD",
    "primary": "#3E45D0", "primary_hover": "#343BBB", "primary_down": "#2C329E", "primary_soft": "#EEF0FF",
    "side_top": "#1D2170", "side_bottom": "#3138A8",
    "accent": "#F28A2E",
    "ok": "#13875A", "ok_soft": "#E5F5EE", "warn": "#A86A12", "warn_soft": "#FFF4DF",
    "bad": "#C93C3C", "bad_soft": "#FDECEC",
}

UI_FONTS = ("segoeui.ttf", "segoeuib.ttf", "segoeuisl.ttf", "seguisb.ttf", "seguiemj.ttf", "seguisym.ttf", "consola.ttf",
            "consolab.ttf")


def load_fonts() -> None:
    """Register the Windows fonts by file: some Qt6 builds cannot enumerate system fonts and would show empty labels."""
    win = os.environ.get("WINDIR")
    if not win:
        return
    for name in UI_FONTS:
        path = os.path.join(win, "Fonts", name)
        if os.path.isfile(path):
            QFontDatabase.addApplicationFont(path)


def base_font() -> QFont:
    f = QFont("Segoe UI", 10)
    f.setStyleHint(QFont.StyleHint.SansSerif)
    return f


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QLinearGradient(0, 0, size, size)
        g.setColorAt(0, QColor(C["side_top"]))
        g.setColorAt(1, QColor(C["primary"]))
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
        p.fillPath(path, g)
        p.setPen(QColor("#FFFFFF"))
        f = QFont("Segoe UI Symbol")
        f.setPixelSize(int(size * 0.62))
        p.setFont(f)
        p.drawText(QRectF(0, -size * 0.04, size, size), Qt.AlignmentFlag.AlignCenter, "✈")
        p.setBrush(QColor(C["accent"]))
        p.setPen(Qt.PenStyle.NoPen)
        r = size * 0.11
        p.drawEllipse(QRectF(size * 0.72, size * 0.16, r, r))
        p.end()
        icon.addPixmap(pm)
    return icon


def qss() -> str:
    c = C
    return f"""
* {{ font-family: "Segoe UI"; color: {c['text']}; }}
QMainWindow, QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget#pagebody {{ background: {c['bg']}; }}
QWidget#clear, QFrame#clear {{ background: transparent; }}
QToolTip {{ background: {c['text']}; color: white; border: none; padding: 6px 8px; border-radius: 6px; }}

/* ── sidebar ─────────────────────────────── */
QFrame#sidebar {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {c['side_top']}, stop:1 {c['side_bottom']}); }}
QFrame#sidebar QLabel {{ color: white; background: transparent; }}
QLabel#brand {{ font-size: 20pt; font-weight: 700; }}
QLabel#tagline {{ color: rgba(255,255,255,0.72); font-size: 9pt; }}
QPushButton#nav {{ color: rgba(255,255,255,0.86); background: transparent; border: none; border-radius: 10px;
    text-align: left; padding: 11px 14px; font-size: 11pt; }}
QPushButton#nav[compact="true"] {{ text-align: center; padding: 11px 0px; font-size: 14pt; }}
QPushButton#nav:hover {{ background: rgba(255,255,255,0.10); }}
QPushButton#nav:checked {{ background: rgba(255,255,255,0.18); color: white; font-weight: 600; }}
QFrame#statusbox {{ background: rgba(255,255,255,0.10); border-radius: 12px; }}
QLabel#statustitle {{ font-weight: 600; font-size: 9.5pt; }}
QLabel#statustext {{ color: rgba(255,255,255,0.82); font-size: 9pt; }}
QPushButton#sidebtn {{ color: white; background: rgba(255,255,255,0.16); border: none; border-radius: 8px;
    padding: 6px 10px; font-size: 9pt; font-weight: 600; }}
QPushButton#sidebtn:hover {{ background: rgba(255,255,255,0.26); }}

/* ── page text ───────────────────────────── */
QLabel#h1 {{ font-size: 21pt; font-weight: 700; }}
QLabel#sub {{ color: {c['text2']}; font-size: 10.5pt; }}
QLabel#cardtitle {{ font-size: 12.5pt; font-weight: 700; }}
QLabel#cardhint {{ color: {c['muted']}; font-size: 9pt; }}
QLabel#label {{ color: {c['text2']}; font-size: 9pt; font-weight: 600; }}
QLabel#small {{ color: {c['text2']}; font-size: 9pt; }}
QLabel#muted {{ color: {c['muted']}; font-size: 9pt; }}
QLabel#err {{ color: {c['bad']}; font-size: 9pt; }}
QLabel#stepnum {{ background: {c['primary_soft']}; color: {c['primary']}; border-radius: 13px; font-weight: 700;
    min-width: 26px; max-width: 26px; min-height: 26px; max-height: 26px; qproperty-alignment: AlignCenter; }}

/* ── cards ───────────────────────────────── */
QFrame#card {{ background: {c['card']}; border: 1px solid {c['line']}; border-radius: 16px; }}
QFrame#card QLabel {{ background: transparent; }}
QFrame#panel {{ background: {c['card']}; border: 1px solid {c['line']}; border-radius: 18px; }}
QFrame#panel QLabel {{ background: transparent; }}
QFrame#divider {{ background: {c['line']}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#notebox {{ background: {c['primary_soft']}; border-radius: 10px; }}
QFrame#okbox {{ background: {c['ok_soft']}; border: 1px solid #BFE6D3; border-radius: 14px; }}
QFrame#badbox {{ background: {c['bad_soft']}; border: 1px solid #F3C4C4; border-radius: 14px; }}
QFrame#warnbox {{ background: {c['warn_soft']}; border-radius: 10px; }}

/* ── inputs ──────────────────────────────── */
QLineEdit, QComboBox, QDateEdit {{ background: white; border: 1px solid {c['line2']}; border-radius: 10px;
    padding: 8px 11px; font-size: 10.5pt; min-height: 22px; selection-background-color: {c['primary']}; }}
QLineEdit:hover, QComboBox:hover, QDateEdit:hover {{ border-color: #B9C1D8; }}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus {{ border: 1px solid {c['primary']}; background: #FCFCFF; }}
QLineEdit[error="true"], QComboBox[error="true"], QDateEdit[error="true"] {{ border: 1px solid {c['bad']}; background: #FFF9F9; }}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled {{ background: #F6F7FB; color: {c['muted']}; }}
QComboBox::drop-down, QDateEdit::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow, QDateEdit::down-arrow {{ width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: white; border: 1px solid {c['line2']}; border-radius: 8px; padding: 4px;
    selection-background-color: {c['primary_soft']}; selection-color: {c['text']}; outline: none; }}
QLabel#prefix {{ background: #F6F7FB; border: 1px solid {c['line2']}; border-right: none; border-top-left-radius: 10px;
    border-bottom-left-radius: 10px; padding: 8px 10px; color: {c['text2']}; font-size: 10.5pt; }}
QLineEdit#withprefix {{ border-top-left-radius: 0; border-bottom-left-radius: 0; }}
QCheckBox {{ spacing: 8px; font-size: 10pt; background: transparent; }}
QCheckBox[error="true"] {{ color: {c['bad']}; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid #AEB6CC; background: white; }}
QCheckBox::indicator:checked {{ background: {c['primary']}; border-color: {c['primary']}; image: none; }}

/* calendar popup */
QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {c['primary']}; }}
QCalendarWidget QToolButton {{ color: white; background: transparent; font-weight: 600; padding: 4px 8px; }}
QCalendarWidget QAbstractItemView:enabled {{ selection-background-color: {c['primary']}; selection-color: white; }}

/* ── buttons ─────────────────────────────── */
QPushButton {{ font-size: 10pt; }}
QPushButton#primary {{ background: {c['primary']}; color: white; border: none; border-radius: 12px; padding: 13px 20px;
    font-size: 12pt; font-weight: 700; }}
QPushButton#primary:hover {{ background: {c['primary_hover']}; }}
QPushButton#primary:pressed {{ background: {c['primary_down']}; }}
QPushButton#primary:disabled {{ background: #B8BCE6; }}
QPushButton#secondary {{ background: white; color: {c['primary']}; border: 1px solid #C9CDF3; border-radius: 10px;
    padding: 8px 14px; font-weight: 600; }}
QPushButton#secondary:hover {{ background: {c['primary_soft']}; }}
QPushButton#ghost {{ background: transparent; color: {c['primary']}; border: none; padding: 6px 4px; font-weight: 600; }}
QPushButton#ghost:hover {{ color: {c['primary_down']}; text-decoration: underline; }}
QPushButton#ghost:disabled {{ color: {c['muted']}; }}
QPushButton#danger {{ background: white; color: {c['bad']}; border: 1px solid #F0C2C2; border-radius: 10px;
    padding: 8px 14px; font-weight: 600; }}
QPushButton#danger:hover {{ background: {c['bad_soft']}; }}
QPushButton#chip {{ background: #F1F3FA; color: {c['text2']}; border: 1px solid transparent; border-radius: 14px;
    padding: 5px 12px; font-size: 9.5pt; }}
QPushButton#chip:hover {{ background: {c['primary_soft']}; color: {c['primary']}; border-color: #D3D7FA; }}
QPushButton#swap {{ background: white; border: 1px solid {c['line2']}; border-radius: 17px; min-width: 34px; max-width: 34px;
    min-height: 34px; max-height: 34px; font-size: 13pt; color: {c['primary']}; }}
QPushButton#swap:hover {{ background: {c['primary_soft']}; }}
QPushButton#seg {{ background: white; color: {c['text2']}; border: 1px solid {c['line2']}; padding: 8px 12px; font-weight: 600; }}
QPushButton#seg[compact="true"] {{ padding: 8px 8px; }}
QPushButton#seg[pos="left"] {{ border-top-left-radius: 10px; border-bottom-left-radius: 10px; }}
QPushButton#seg[pos="right"] {{ border-top-right-radius: 10px; border-bottom-right-radius: 10px; border-left: none; }}
QPushButton#seg[pos="mid"] {{ border-left: none; }}
QPushButton#seg:checked {{ background: {c['primary']}; color: white; border-color: {c['primary']}; }}
QPushButton#seg[error="true"] {{ border-color: {c['bad']}; }}
QPushButton#remove {{ background: transparent; border: none; color: {c['muted']}; font-size: 13pt; padding: 2px 6px; }}
QPushButton#remove:hover {{ color: {c['bad']}; }}
QPushButton#offer {{ background: white; border: 1px solid {c['line2']}; border-radius: 14px; padding: 14px 12px;
    text-align: center; font-size: 12pt; font-weight: 700; color: {c['text']}; }}
QPushButton#offer:disabled {{ background: #F6F7FB; color: {c['muted']}; font-size: 10pt; font-weight: 600; }}
QFrame#tabstrip {{ background: #E9ECF7; border-bottom: 1px solid {c['line2']}; }}
QTabBar#booktabs {{ background: transparent; }}
QTabBar#booktabs::tab {{ background: #DDE1F1; color: {c['text2']}; border: 1px solid {c['line2']}; border-bottom: none; border-top-left-radius: 11px;
    border-top-right-radius: 11px; padding: 9px 12px 9px 16px; margin: 0 4px 0 0; min-width: 130px; max-width: 230px; font-weight: 600; }}
QTabBar#booktabs::tab:selected {{ background: {c['bg']}; color: {c['text']}; border-color: {c['line2']}; }}
QTabBar#booktabs::tab:hover:!selected {{ background: #EDEFFA; }}
QToolButton#tabclose {{ background: transparent; color: {c['muted']}; border: none; border-radius: 9px; padding: 2px 6px; font-weight: 700; }}
QToolButton#tabclose:hover {{ background: {c['bad_soft']}; color: {c['bad']}; }}
QToolButton#newtab {{ background: white; color: {c['primary']}; border: 1px solid {c['line2']}; border-radius: 12px; padding: 7px 14px; margin-bottom: 4px;
    font-weight: 700; }}
QToolButton#newtab:hover {{ background: {c['primary_soft']}; border-color: #C9CDF3; }}
QToolButton#savedbtn {{ background: white; color: {c['primary']}; border: 1px solid #C9CDF3; border-radius: 10px; padding: 6px 12px; font-weight: 700; }}
QToolButton#savedbtn:hover {{ background: {c['primary_soft']}; }}
QToolButton#savedbtn::menu-indicator {{ image: none; width: 0; }}
QMenu {{ background: white; border: 1px solid {c['line2']}; border-radius: 8px; padding: 6px; }}
QMenu::item {{ padding: 8px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c['primary_soft']}; color: {c['primary']}; }}
QMenu::item:disabled {{ color: {c['muted']}; }}
QMenu::separator {{ height: 1px; background: {c['line']}; margin: 5px 8px; }}
QPushButton#offer:hover {{ border-color: #A9AFEF; background: #FAFAFF; }}
QPushButton#offer:checked {{ border: 2px solid {c['primary']}; background: {c['primary_soft']}; padding: 13px 11px; }}
QPushButton#offer[error="true"] {{ border-color: {c['bad']}; }}
QPushButton#copy {{ background: {c['primary']}; color: white; border: none; border-radius: 9px; padding: 8px 14px; font-weight: 700; }}
QPushButton#copy:hover {{ background: {c['primary_hover']}; }}

/* ── result / progress ───────────────────── */
QLabel#pnr {{ font-family: Consolas, "Segoe UI"; font-size: 24pt; font-weight: 700; letter-spacing: 3px; color: {c['text']}; }}
QLabel#route {{ font-size: 19pt; font-weight: 700; }}
QLabel#routecities {{ color: {c['text2']}; font-size: 10pt; }}
QLabel#kicker {{ color: {c['muted']}; font-size: 8.5pt; font-weight: 700; letter-spacing: 1px; }}
QLabel#bigok {{ color: {c['ok']}; font-size: 15pt; font-weight: 700; }}
QLabel#bigbad {{ color: {c['bad']}; font-size: 13pt; font-weight: 700; }}
QLabel#save {{ color: {c['ok']}; font-weight: 700; }}
QLabel#timer {{ color: {c['warn']}; font-weight: 700; font-size: 10.5pt; }}
QLabel#timer[urgent="true"] {{ color: {c['bad']}; }}
QLabel#timer[over="true"] {{ color: {c['muted']}; }}
QLabel#elapsed {{ color: {c['muted']}; font-size: 9pt; }}
QLabel#stepicon {{ font-size: 12pt; min-width: 24px; }}
QLabel#steptext {{ font-size: 10.5pt; color: {c['muted']}; }}
QLabel#steptext[state="active"] {{ color: {c['primary']}; font-weight: 700; }}
QLabel#steptext[state="done"] {{ color: {c['text']}; }}
QLabel#toast {{ background: {c['text']}; color: white; border-radius: 10px; padding: 9px 16px; font-weight: 600; }}

QTextEdit#activity {{ background: white; border: 1px solid {c['line']}; border-radius: 14px; padding: 12px;
    font-family: Consolas, "Segoe UI"; font-size: 10pt; selection-background-color: {c['primary_soft']}; }}
QFrame#demobar {{ background: {c['warn_soft']}; border-bottom: 1px solid #F0D9A8; }}
QFrame#demobar QLabel {{ background: transparent; color: {c['warn']}; }}

QFrame#calpopup {{ background: white; border: 1px solid {c['line2']}; border-radius: 16px; }}
QFrame#calpopup QLabel {{ background: transparent; }}
QPushButton#datefield {{ background: white; border: 1px solid {c['line2']}; border-radius: 10px; padding: 9px 12px; font-size: 10.5pt;
    text-align: left; min-height: 22px; }}
QPushButton#datefield:hover {{ border-color: #B9C1D8; }}
QPushButton#datefield:focus {{ border: 1px solid {c['primary']}; }}
QPushButton#datefield[error="true"] {{ border: 1px solid {c['bad']}; background: #FFF9F9; }}
QListView#stationpopup {{ background: white; border: 1px solid {c['line2']}; border-radius: 12px; padding: 4px; outline: none; }}

/* ── the flight list ("Choose your flight") ───────────────────────── */
QFrame#flightcard {{ background: white; border: 1px solid {c['line']}; border-radius: 12px; }}
QFrame#flightcard:hover {{ border: 1px solid #A9AFEF; background: #FAFAFF; }}
QFrame#flightcard[sel="true"] {{ border: 2px solid {c['primary']}; background: {c['primary_soft']}; }}
QFrame#flightcard QLabel {{ background: transparent; }}
QLabel#flighttime {{ font-size: 12.5pt; font-weight: 700; color: {c['text']}; }}
QLabel#flightprice {{ font-size: 12.5pt; font-weight: 700; color: {c['primary']}; }}
QLabel#flightmeta {{ font-size: 9pt; color: {c['text2']}; }}
QLabel#flightsave {{ font-size: 9pt; font-weight: 700; color: {c['ok']}; }}
QLabel#pill {{ background: {c['ok_soft']}; color: {c['ok']}; border-radius: 9px; padding: 2px 9px; font-size: 8.5pt; font-weight: 700; }}
QFrame#flightlist {{ background: transparent; border: none; }}

/* ── history table ───────────────────────── */
QTableWidget {{ background: white; border: 1px solid {c['line']}; border-radius: 14px; gridline-color: transparent;
    font-size: 10pt; selection-background-color: {c['primary_soft']}; selection-color: {c['text']}; alternate-background-color: #FAFBFE; }}
QTableWidget::item {{ padding: 8px 6px; border-bottom: 1px solid #F0F2F8; }}
QHeaderView::section {{ background: white; color: {c['muted']}; border: none; border-bottom: 1px solid {c['line']};
    padding: 10px 6px; font-size: 8.5pt; font-weight: 700; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #CCD2E3; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}
QMessageBox QLabel {{ font-size: 10pt; }}
"""
