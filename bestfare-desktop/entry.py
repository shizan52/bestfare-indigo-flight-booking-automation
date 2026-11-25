"""BestFare.exe - what runs when the installed program is started.

    BestFare.exe                     the app: sign in, fetch/verify/open the program, run it (updates itself)
    BestFare.exe --demo              practice mode (nothing is sent to IndiGo)
    BestFare.exe --wait-pid N        (used by a restart) wait until the old copy has ended, then start
    BestFare.exe --uninstall         "Remove Best Fare" (this is what Windows' Installed apps list runs); --silent for no window, --purge also deletes the data
    BestFare.exe --selfcheck --out F check that everything the program needs is inside this build (no window, no internet); writes JSON to F
    BestFare.exe --smoke --key K --out F --server URL   sign in with a key, get the program from the server, open its window, write what happened to F, quit
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional


def _arg(argv: list[str], name: str) -> Optional[str]:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _write(path: Optional[str], data: dict) -> None:
    text = json.dumps(data, indent=2, default=str)
    if path:
        Path(path).write_text(text, encoding="utf-8")
    elif sys.stdout:
        print(text)


def selfcheck(argv: list[str]) -> int:
    """Everything the shell AND the program (which arrives later) import must be inside this build; Qt must be able to draw."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import importlib
    report: dict = {"frozen": bool(getattr(sys, "frozen", False)), "python": sys.version.split()[0], "missing": [], "errors": []}
    try:
        import trust
        report["shell_version"] = trust.SHELL_VERSION
        report["server"] = trust.server_url()
        report["tls_pin"] = bool(trust.TLS_PIN)
    except Exception as e:  # noqa: BLE001
        report["errors"].append("trust: " + repr(e))
    try:
        import _bf_imports
        wanted = list(_bf_imports.MODULES)
    except ImportError:
        wanted = ["PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets", "aiohttp", "cryptography", "sqlite3", "ssl", "asyncio"]
    report["checked"] = len(wanted)
    for m in wanted:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001
            report["missing"].append(f"{m}: {type(e).__name__}")
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv[:1])
        import update_ui as ui
        ui.load_fonts()
        d = ui.UpdateDialog(title="Check", new="1.0.0", notes="one\ntwo")
        d.show()
        app.processEvents()
        pm = d.grab()
        report["window_pixels"] = [pm.width(), pm.height()]
        report["qt_platform"] = app.platformName()
        if pm.isNull() or pm.width() < 100:
            report["errors"].append("a window could not be drawn")
        d.close()
    except Exception as e:  # noqa: BLE001
        report["errors"].append("qt: " + repr(e))
    try:
        import cloud
        report["crypto"] = bool(cloud.verify("AAAA", b"x", "AAAA") is False)
    except Exception as e:  # noqa: BLE001
        report["errors"].append("crypto: " + repr(e))
    report["ok"] = not report["missing"] and not report["errors"]
    _write(_arg(argv, "--out"), report)
    return 0 if report["ok"] else 1


def uninstall(argv: list[str]) -> int:
    import setup_core as core
    target = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else (core.installed() or {"dir": core.default_dir()})["dir"]
    info = core.installed() or {"version": ""}
    if "--silent" in argv or "/S" in argv:
        try:
            core.uninstall(Path(target), purge_data="--purge" in argv, running_from_target=getattr(sys, "frozen", False))
            return 0
        except core.SetupError as e:
            core.log("silent uninstall failed: " + str(e))
            return 1
    from PyQt6.QtCore import QLockFile, Qt
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication, QMessageBox
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    import setup_ui
    import update_ui as ui
    ui.load_fonts()
    app.setWindowIcon(QIcon(ui.logo(64)))
    lock = QLockFile(str(core.data_dir() / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "Best Fare", "Best Fare is open. Please close it first, then remove it.")
        return 1
    lock.unlock()
    return setup_ui.run_uninstall(Path(target), info["version"])


def smoke(argv: list[str]) -> int:
    """A real start against a real server, without a person: sign in with `--key`, get the program, open the window, report, quit."""
    out = _arg(argv, "--out")
    server = _arg(argv, "--server")
    key = _arg(argv, "--key")
    if server:
        os.environ["BESTFARE_SERVER"] = server
    if "--visible" not in argv:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    report: dict = {"ok": False, "stage": "start"}
    try:
        import shell
        import trust
        report["shell_version"] = trust.SHELL_VERSION
        if key:
            L = shell.make_licence()
            L.activate(key)
            report["stage"] = "signed in"
        report["build_before"] = shell.current_build()

        def ready() -> None:
            report.update(ok=True, stage="window shown", build=shell.current_build())
            try:
                import buildinfo
                report["program_version"] = buildinfo.VERSION
                report["program_build"] = buildinfo.BUILD
            except ImportError:
                pass
            from PyQt6.QtWidgets import QApplication
            QApplication.quit()
        code = shell.run_packed(smoke=ready)
        report["exit_code"] = code
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001
        report["error"] = repr(e)
    _write(out, report)
    return 0 if report.get("ok") else 1


def main(argv: list[str]) -> int:
    pid = _arg(argv, "--wait-pid")
    if pid and pid.isdigit():
        import setup_core
        setup_core.wait_for_exit(int(pid), 40.0)
    if "--selfcheck" in argv:
        return selfcheck(argv)
    if "--uninstall" in argv:
        return uninstall(argv)
    if "--smoke" in argv:
        return smoke(argv)
    import shell
    return shell.run_packed(demo="--demo" in argv, debug="--debug" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
