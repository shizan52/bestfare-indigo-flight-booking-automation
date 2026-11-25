"""BestFare-Setup.exe - what runs when the setup program is started.

    BestFare-Setup.exe                         the install wizard (it notices an existing install and offers Update / Reinstall)
    BestFare-Setup.exe /S  (or --silent)       no windows: install for this Windows user (--dir FOLDER, --no-desktop, --no-startmenu, --no-launch)
    BestFare-Setup.exe --update --wait-pid N   started by an installed Best Fare that found a newer setup: waits for it to close, replaces the
                                               program, starts it again (a small update window is shown)
Everything is written to %LOCALAPPDATA%\\BestFare\\logs\\setup.log as well.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import setup_core as core


def _arg(argv: list[str], name: str) -> Optional[str]:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _only_one() -> bool:
    """One setup at a time (a named mutex). False = another setup is already running."""
    if os.name != "nt":
        return True
    import ctypes
    k = ctypes.windll.kernel32
    k.CreateMutexW.restype = ctypes.c_void_p
    h = k.CreateMutexW(None, False, "Local\\BestFareSetup")
    _only_one.handle = h                                            # type: ignore[attr-defined]  (kept for the life of the process)
    return k.GetLastError() != 183                                  # 183 = ERROR_ALREADY_EXISTS


def _say(title: str, text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:  # noqa: BLE001
        pass


def silent(argv: list[str]) -> int:
    try:
        payload = core.payload_path()
        manifest = core.read_manifest(payload)
        existing = core.installed()
        if existing and core.version_number(existing["version"]) > core.version_number(manifest["version"]):
            core.log("silent: a newer version is already installed; nothing to do")
            return 3
        target = Path(_arg(argv, "--dir") or (existing["dir"] if existing else core.default_dir()))
        pid = _arg(argv, "--wait-pid")
        if pid and pid.isdigit():
            core.wait_for_exit(int(pid), 60.0)
        out = core.install(payload, target, desktop="--no-desktop" not in argv, startmenu="--no-startmenu" not in argv, update=existing is not None)
        if "--no-launch" not in argv and ("--update" in argv or "--launch" in argv):
            core.launch(out["dir"])
        return 0
    except core.SetupError as e:
        core.log("silent install failed: " + str(e))
        return 1
    except Exception as e:  # noqa: BLE001
        core.log("silent install crashed: " + repr(e))
        return 2


def main(argv: list[str]) -> int:
    if any(a in argv for a in ("/S", "/s", "--silent")):
        return silent(argv)
    if not _only_one():
        _say("Best Fare Setup", "Best Fare Setup is already open.")
        return 0
    try:
        payload = core.payload_path()
        manifest = core.read_manifest(payload)
    except core.SetupError as e:
        _say("Best Fare Setup", str(e))
        return 2
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    import update_ui as ui
    ui.load_fonts()
    from PyQt6.QtGui import QIcon
    app.setWindowIcon(QIcon(ui.logo(64)))
    app.setApplicationName("Best Fare Setup")
    import setup_ui
    existing = core.installed()
    if "--update" in argv:
        pid = _arg(argv, "--wait-pid")
        d = _arg(argv, "--dir")
        return setup_ui.run_update(payload, manifest, existing, int(pid) if pid and pid.isdigit() else None, Path(d) if d else None)
    return setup_ui.run_install(payload, manifest, existing)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
