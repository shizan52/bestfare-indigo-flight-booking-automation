"""While Best Fare is open: notice a newer release or a newer setup, fetch it quietly, and offer the restart (the shell's part - it needs the network)."""
from __future__ import annotations

import threading
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

import selfupdate
from update_ui import ReadyCard, UpdateDialog


def booking_running() -> bool:
    """Is a booking in progress right now? (The app's window has an engine that says so.) An update must never cut one short."""
    for w in QApplication.topLevelWidgets():
        e = getattr(w, "engine", None)
        if e is not None and getattr(e, "busy", False):
            return True
    return False


class AutoUpdate(QObject):
    """While Best Fare is open: notice a newer release or setup (the licence refresh brings the news), fetch it quietly, then offer the restart.

    * a newer program release → downloaded and adopted in the background; "Restart now" starts the app again (it opens the new one)
    * a newer setup (the app itself) → downloaded and checked; "Restart now" runs it in update mode
    * a REQUIRED update (the admin said so) does not wait to be asked: a window that cannot be dismissed asks for the restart - once no booking is running
    * a restart never cuts a booking short: it waits for the booking to finish
    """
    _poke = pyqtSignal()
    _ready = pyqtSignal(str, object, object)               # (kind, info, path-or-None)

    def __init__(self, licence, updater, running_build: int, *, quit_app: Optional[Callable[[], None]] = None, busy: Callable[[], bool] = booking_running,
                 every_s: int = 600, first_s: int = 25):
        super().__init__()
        self.selfupdate = selfupdate
        self.licence, self.updater, self.running = licence, updater, running_build
        self.quit_app = quit_app or QApplication.quit
        self.busy = busy
        self.every_s, self.first_s = every_s, first_s
        self.working = False
        self.shown: set = set()
        self.card: Optional[ReadyCard] = None
        self.modal: Optional[UpdateDialog] = None
        self._poke.connect(self.check)
        self._ready.connect(self._on_ready)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check)
        self.last_error = ""

    def start(self) -> None:
        self.licence.on_change(lambda _l: self._poke.emit())          # (the licence's refresh runs on other threads: the signal brings it home)
        self._timer.start(self.every_s * 1000)
        QTimer.singleShot(self.first_s * 1000, self.check)

    # ── noticing ──
    def check(self) -> None:
        if self.working:
            return
        L = self.licence
        offer = self.selfupdate.check(getattr(L, "installer", None))
        update = L.update if isinstance(L.update, dict) else {}
        release = None
        st = self.updater.releases.state()
        b = update.get("build")
        if update.get("available") and isinstance(b, int) and not isinstance(b, bool) and b > max(st["current"] or 0, self.running) and b not in st["bad"]:
            release = update
        if offer is not None and ("setup", offer["version"]) not in self.shown:
            self.working = True
            threading.Thread(target=self._get_setup, args=(offer,), name="update-setup", daemon=True).start()
        elif release is not None and ("release", b) not in self.shown:
            self.working = True
            threading.Thread(target=self._get_release, args=(release,), name="update-release", daemon=True).start()

    def _get_setup(self, offer: dict) -> None:
        try:
            path = self.selfupdate.fetch(self.licence.client, offer)
            self._ready.emit("setup", offer, path)
        except Exception as e:  # noqa: BLE001 - a failed quiet download is retried at the next check
            self.last_error = getattr(e, "message", None) or str(e)
            self._ready.emit("failed", None, None)

    def _get_release(self, update: dict) -> None:
        try:
            build = self.updater.background(update)
            self._ready.emit("release" if build else "failed", update if build else None, None)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self._ready.emit("failed", None, None)

    # ── offering ──
    def _on_ready(self, kind: str, info, path) -> None:
        self.working = False
        if kind == "failed" or info is None:
            return
        key = (kind, info.get("version") if kind == "setup" else info.get("build"))
        if key in self.shown:
            return
        self.shown.add(key)
        self.pending = (kind, path)
        version = str(info.get("version") or "")
        notes = str(info.get("notes") or "")
        if info.get("mandatory"):
            self._required(version, notes)
            return
        self.card = ReadyCard(version, notes)
        self.card.restart_clicked.connect(self.restart_when_idle)
        self.card.later_clicked.connect(self.card.close)
        self.card.show_in_corner()

    def _required(self, version: str, notes: str) -> None:
        dlg = UpdateDialog(title="An update is required", tagline="Please restart Best Fare", new=version, notes=notes, mandatory=True)
        dlg.show_done("Best Fare " + version + " is ready. Restart to continue booking.", "Restart now")
        dlg.primary_clicked.connect(lambda: (dlg.accept(), self.restart_when_idle()))
        self.modal = dlg
        dlg.open()

    def restart_when_idle(self) -> None:
        if self.busy():
            if self.card is not None:
                self.card.waiting("A booking is running. Best Fare will restart as soon as it is finished.")
            QTimer.singleShot(3000, self.restart_when_idle)
            return
        self.restart_now()

    def restart_now(self) -> None:
        kind, path = self.pending
        try:
            if kind == "setup" and path is not None:
                self.selfupdate.launch(path)
            else:
                self.selfupdate.start_again()
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            if self.card is not None:
                self.card.text.setText("The restart did not work. Please close Best Fare and open it again.")
            return
        self.quit_app()
