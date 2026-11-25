"""Best Fare — IndiGo bookings on the user's own computer.

Run (development):   .venv\\Scripts\\python.exe app.py
Data lives in %LOCALAPPDATA%\\BestFare (history, settings, logs, the private Edge profile).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

import paths

paths.use_engine()

from PyQt6.QtCore import QLockFile, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import theme  # noqa: E402
from store import Store  # noqa: E402


OWN_LOGGERS = ("bestfare", "app")


class _OnlyOwn(logging.Filter):
    """Normal mode: the log file holds only the customer-facing story (the same lines as the Activity page). Everything technical
    - what is called, how, in what order - is not written anywhere a curious person could read it."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.split(".")[0] in OWN_LOGGERS


def debug_wanted(flag: bool = False) -> bool:
    """Debug mode (the developer's): run.py --debug, BESTFARE_DEBUG=1, or a file named debug.flag in the data folder."""
    import os
    return bool(flag or os.environ.get("BESTFARE_DEBUG") or (paths.data_dir() / "debug.flag").exists())


def setup_logging(debug: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(paths.log_dir() / "bestfare.log", maxBytes=1_000_000, backupCount=3,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    if not debug:
        fh.addFilter(_OnlyOwn())
    root.addHandler(fh)
    if debug and not getattr(sys, "frozen", False) and sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s"))
        root.addHandler(sh)
    for noisy in ("aiohttp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(demo: bool = False, debug: bool = False, licence=None, on_ready=None) -> int:
    debug = debug_wanted(debug)
    setup_logging(debug)
    if os.name == "nt":
        try:                                          # own taskbar icon instead of python.exe's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BestFare.Desktop")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication.instance()
    if app is None:
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication(sys.argv)
    app.setApplicationName(paths.APP_NAME)
    theme.load_fonts()
    app.setFont(theme.base_font())
    app.setWindowIcon(theme.app_icon())
    app.setStyleSheet(theme.qss())

    lock = QLockFile(str(paths.data_dir() / ("app-demo.lock" if demo else "app.lock")))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, paths.APP_NAME, "Best Fare is already open" + (" in practice mode." if demo else "."))
        return 0

    from main_window import MainWindow
    if not demo and licence is None:
        import shell
        if not shell.dev_mode():                      # the real app: the key is checked with the server before anything opens
            licence = shell.sign_in(app)
            if licence is None:
                lock.unlock()
                return 0
    store = Store(paths.db_path(demo))
    if demo:
        from demo import DemoEngine
        win = MainWindow(store, lambda emit: DemoEngine(store, emit, debug), demo=True, debug=debug)
    elif licence is not None:
        import engine as eng
        from gate import LicenceGate
        win = MainWindow(store, lambda emit: eng.Engine(store, emit, debug=debug, gate=LicenceGate(licence)), debug=debug, licence=licence)
    else:
        win = MainWindow(store, debug=debug)
    win.show()
    win.start()
    if on_ready is not None:                          # the window is up and the first seconds went well: this version works here
        QTimer.singleShot(4000, on_ready)
    logging.getLogger("app").info("Best Fare started%s", " in practice mode" if demo else "")
    code = app.exec()
    store.close()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
