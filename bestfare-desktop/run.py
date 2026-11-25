"""Best Fare — start here.

    python run.py                 open Best Fare (real bookings on IndiGo; it asks for your key the first time)
    python run.py --demo          PRACTICE MODE: every screen works, NOTHING is sent to IndiGo (safe to click around)
    python run.py --debug         the developer's view: the technical story in Activity, a visible browser window, full logs
    python run.py --check         quick check of this computer (Python, packages, Edge, folders) — no window
    python run.py --check --browser   … and also start the booking browser and look at what IndiGo sees
    python run.py --tests         run all automated tests
    python run.py --setup         (re)install the packages into the app's own folder (.venv)
    python run.py --dev           DEVELOPER ONLY (this workspace): real bookings WITHOUT a key or server - the offer codes are read from
                                  the bot's config. A distribution built for users does not contain what this needs.

The first run creates a private Python environment (the .venv folder) and installs what the app needs. Nothing is
installed into your normal Python. Data (history, settings, logs, the booking browser's profile) lives in
%LOCALAPPDATA%\\BestFare; the log file is there too (logs\\bestfare.log).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
VPY = VENV / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
REQ = HERE / "requirements.txt"


def say(text: str = "") -> None:
    print(text, flush=True)


def in_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def packages_ok() -> bool:
    if not VPY.exists():
        return False
    return subprocess.run([str(VPY), "-c", "import PyQt6.QtWidgets, aiohttp, cryptography"], capture_output=True).returncode == 0


def setup_env(force: bool = False) -> None:
    if sys.version_info < (3, 10):
        say(f"Best Fare needs Python 3.10 or newer (this is {sys.version.split()[0]}).")
        sys.exit(2)
    if not VPY.exists():
        say("First start: creating the app's own Python environment (about a minute)…")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    if force or not packages_ok():
        say("Installing what Best Fare needs (PyQt6, aiohttp, cryptography)… please wait.")
        subprocess.check_call([str(VPY), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(REQ)])
        say("Done.")


def utf8_output() -> None:
    """The ✓ / ✗ marks must not crash a redirected or old-code-page console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _dev_offers() -> bool:
    """Developer mode: the offers' codes from the bot's config (only the workspace has that file)."""
    cfg = HERE.parent / "indigo-bot" / "config.py"
    if not cfg.exists():
        return False
    sys.path.insert(0, str(cfg.parent))
    try:
        import json
        import config as botcfg
        os.environ["BESTFARE_DEV_OFFERS"] = json.dumps(dict(botcfg.COUPONS))
        return True
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str]) -> int:
    utf8_output()
    if "-h" in argv or "--help" in argv:
        say(__doc__)
        return 0
    if "--setup" in argv:
        setup_env(force=True)
        return 0
    if not in_venv():                                   # run inside the app's own environment
        setup_env()
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        return subprocess.call([str(VPY), str(Path(__file__).resolve()), *argv], env=env, cwd=str(HERE))

    sys.path.insert(0, str(HERE))
    if "--check" in argv:
        try:
            import selfcheck
        except ImportError:
            say("The check is part of the program, which is fetched when Best Fare starts. Start Best Fare once, then run it again.")
            return 0
        return selfcheck.run(browser="--browser" in argv)
    if "--tests" in argv:
        say("Running the automated tests…\n")
        return subprocess.call([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=str(HERE))
    demo = "--demo" in argv
    debug = "--debug" in argv
    if "--dev" in argv:
        if not _dev_offers():
            say("--dev needs ../indigo-bot/config.py (the developer's workspace). Without it, sign in with a key instead.")
            return 2
        say("DEVELOPER MODE — no sign-in, no credits. Real bookings on IndiGo.")
    if debug:
        say("DEBUG MODE — technical details are shown and logged.")
    if demo:
        say("PRACTICE MODE — nothing is sent to IndiGo. Close the window to stop.")
    else:
        say("Starting Best Fare… (the window opens in a few seconds)")
    if not (HERE / "app.py").exists():                   # a distribution: only the shell is here, the program arrives from the server
        import shell
        return shell.run_packed(demo=demo, debug=debug)
    import app
    return app.main(demo=demo, debug=debug)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
