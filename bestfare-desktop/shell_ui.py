"""The windows that appear BEFORE the app itself: connecting, typing the key, "your access has been stopped".

They are part of the small plain-code shell (everything else of the app arrives from the server) and carry their own small style, so
they need nothing from the rest of the program.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QVBoxLayout

import licence as lic
from cloud import CloudError

QSS = """
QDialog { background: #F3F4FA; }
QLabel { color: #1B2145; font-size: 10.5pt; }
QLabel#brand { font-size: 20pt; font-weight: 700; color: #3E45D0; }
QLabel#h { font-size: 14pt; font-weight: 700; }
QLabel#muted { color: #6C74A0; font-size: 9pt; }
QLabel#err { color: #D03A3A; font-weight: 600; }
QLabel#ok { color: #0F8A5F; font-weight: 600; }
QFrame#card { background: white; border: 1px solid #E6E8F2; border-radius: 16px; }
QLineEdit { background: #FBFBFE; border: 1px solid #D7DBEC; border-radius: 10px; padding: 11px 13px; font-size: 14pt; font-family: Consolas, "Segoe UI";
  letter-spacing: 2px; selection-background-color: #3E45D0; }
QLineEdit:focus { border: 1px solid #3E45D0; background: white; }
QPushButton { background: white; color: #3E45D0; border: 1px solid #C9CDF3; border-radius: 10px; padding: 10px 18px; font-weight: 600; }
QPushButton:hover { background: #EEF0FF; }
QPushButton#primary { background: #3E45D0; color: white; border: none; }
QPushButton#primary:hover { background: #343BBB; }
QPushButton#primary:disabled { background: #B8BCE6; }
QProgressBar { background: #E6E8F2; border: none; border-radius: 4px; max-height: 6px; }
QProgressBar::chunk { background: #3E45D0; border-radius: 4px; }
"""


class _Worker(QObject):
    """Runs one blocking call (the network) off the window's thread and hands the result back through a signal."""
    done = pyqtSignal(object, object)                         # (result, error)

    def run(self, fn: Callable) -> None:
        def go() -> None:
            try:
                self.done.emit(fn(), None)
            except Exception as e:  # noqa: BLE001
                self.done.emit(None, e)
        threading.Thread(target=go, name="shell-worker", daemon=True).start()


def _card(dialog: QDialog) -> QVBoxLayout:
    dialog.setStyleSheet(QSS)
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(28, 26, 28, 22)
    brand = QLabel("✈  Best Fare")
    brand.setObjectName("brand")
    outer.addWidget(brand)
    card = QFrame()
    card.setObjectName("card")
    v = QVBoxLayout(card)
    v.setContentsMargins(24, 22, 24, 22)
    v.setSpacing(10)
    outer.addWidget(card, 1)
    return v


class LoginDialog(QDialog):
    """Type the key your admin gave you."""

    def __init__(self, licence: "lic.Licence", computer: str = "", note: str = ""):
        super().__init__()
        self.licence = licence
        self.setWindowTitle("Best Fare — Sign in")
        self.setMinimumWidth(500)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        v = _card(self)
        title = QLabel("Sign in")
        title.setObjectName("h")
        v.addWidget(title)
        intro = QLabel("Type the key your admin gave you. It works on this computer only.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.key = QLineEdit()
        self.key.setPlaceholderText("BF-xxxx-xxxx-xxxx-xxxx")
        self.key.setMaxLength(40)
        self.key.textEdited.connect(self._upper)
        self.key.returnPressed.connect(self._go)
        v.addWidget(self.key)
        self.msg = QLabel(note)
        self.msg.setObjectName("err")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.hide()
        v.addWidget(self.bar)
        row = QHBoxLayout()
        quit_ = QPushButton("Quit")
        quit_.clicked.connect(self.reject)
        self.go = QPushButton("Sign in")
        self.go.setObjectName("primary")
        self.go.clicked.connect(self._go)
        self.go.setDefault(True)
        row.addWidget(quit_)
        row.addStretch(1)
        row.addWidget(self.go)
        v.addLayout(row)
        if computer:
            c = QLabel("This computer: " + computer)
            c.setObjectName("muted")
            c.setWordWrap(True)
            v.addWidget(c)
        self.worker = _Worker()
        self.worker.done.connect(self._finished)
        self.key.setFocus()

    def _upper(self, text: str) -> None:
        self.key.setText(text.upper())

    def _go(self) -> None:
        if not self.go.isEnabled():
            return
        self.msg.setText("")
        self.go.setEnabled(False)
        self.key.setEnabled(False)
        self.bar.show()
        text = self.key.text()
        self.worker.run(lambda: self.licence.activate(text))

    def _finished(self, _result, error) -> None:
        self.bar.hide()
        self.key.setEnabled(True)
        self.go.setEnabled(True)
        if error is None:
            self.accept()
            return
        self.msg.setText(error.message if isinstance(error, CloudError) else "Something went wrong. Please try again.")
        self.key.setFocus()
        self.key.selectAll()


class StatusDialog(QDialog):
    """'Connecting…', with Retry and Quit when there is no connection (a slow connection is waited for, not given up on)."""

    def __init__(self, text: str = "Connecting to Best Fare…"):
        super().__init__()
        self.setWindowTitle("Best Fare")
        self.setMinimumWidth(440)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        v = _card(self)
        self.title = QLabel(text)
        self.title.setObjectName("h")
        self.title.setWordWrap(True)
        v.addWidget(self.title)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        v.addWidget(self.detail)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        v.addWidget(self.bar)
        row = QHBoxLayout()
        self.quit = QPushButton("Quit")
        self.quit.clicked.connect(self.reject)
        self.retry = QPushButton("Try again")
        self.retry.setObjectName("primary")
        self.retry.hide()
        row.addWidget(self.quit)
        row.addStretch(1)
        row.addWidget(self.retry)
        v.addLayout(row)

    def busy(self, title: str, detail: str = "") -> None:
        self.title.setText(title)
        self.detail.setText(detail)
        self.bar.show()
        self.retry.hide()

    def problem(self, title: str, detail: str, retry: bool = True) -> None:
        self.title.setText(title)
        self.detail.setText(detail)
        self.bar.hide()
        self.retry.setVisible(retry)


def _connect(L: "lic.Licence", say: Optional[Callable[[str], None]] = None) -> str:
    """Ask the server whether the saved sign-in is still good, with a window that stays calm on a slow or missing connection."""
    dlg = StatusDialog()
    worker = _Worker()
    out = {"state": None}

    def start() -> None:
        dlg.busy("Connecting to Best Fare…", "This can take a little longer on a slow internet connection.")
        worker.run(L.resume)

    def finished(state, error) -> None:
        if error is not None:
            dlg.problem("Something went wrong", "Please try again.")
            return
        out["state"] = state
        if state == lic.OFFLINE:
            dlg.problem("Cannot reach Best Fare", (L.message or "Please check the internet connection.") + "\n\nBest Fare needs the internet to check your credits.")
        else:
            dlg.accept()

    worker.done.connect(finished)
    dlg.retry.clicked.connect(start)
    QTimer.singleShot(50, start)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return "quit"
    return out["state"] or lic.OFFLINE


def blocked_dialog(L: "lic.Licence") -> bool:
    """Access was stopped (or ended). Returns True when the user wants to try another key."""
    dlg = StatusDialog("Your access has been stopped" if L.state == lic.BLOCKED else "Your licence has ended")
    dlg.problem(dlg.title.text(), L.message or "Please contact your admin.", retry=False)
    dlg.retry.setText("Use another key")
    dlg.retry.show()
    dlg.retry.clicked.connect(dlg.accept)
    dlg.quit.setText("Close")
    return dlg.exec() == QDialog.DialogCode.Accepted


def sign_in(L: "lic.Licence", computer: str = "") -> bool:
    """The whole start-up conversation. True = signed in and allowed; False = the user quit."""
    app = QApplication.instance()
    assert app is not None
    note = ""
    for _ in range(50):
        state = _connect(L)
        if state == "quit":
            return False
        if state == lic.OK:
            return True
        if state == lic.OFFLINE:
            continue
        if state in (lic.BLOCKED, lic.EXPIRED):
            if not blocked_dialog(L):
                return False
            L.sign_out()
            note = ""
        else:
            note = L.message if state == lic.NEW and L.message and L.message not in ("",) else ""
        dlg = LoginDialog(L, computer, note)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        if L.state == lic.OK:
            return True
    return False


def run_with_progress(title: str, detail: str, fn: Callable):
    """Run one long job (a download) off the window's thread while a calm 'please wait' window is shown. Returns its result or raises its error."""
    dlg = StatusDialog(title)
    dlg.busy(title, detail)
    dlg.quit.hide()
    worker = _Worker()
    out: dict = {}

    def finished(result, error) -> None:
        out["result"], out["error"] = result, error
        dlg.accept()

    worker.done.connect(finished)
    QTimer.singleShot(50, lambda: worker.run(fn))
    dlg.exec()
    if out.get("error") is not None:
        raise out["error"]
    return out.get("result")
