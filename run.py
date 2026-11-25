"""Best Fare — start it from here.

This small file only passes everything on to the real one in bestfare-desktop\run.py (which keeps working on its own,
so that folder can be copied to another computer by itself).

    python run.py                 open Best Fare (real bookings on IndiGo)
    python run.py --demo          PRACTICE MODE: every screen works, NOTHING is sent to IndiGo (safe to click around)
    python run.py --check         quick check of this computer (Python, packages, Edge, folders) — no window
    python run.py --check --browser   … and also start the booking browser and look at what IndiGo sees
    python run.py --tests         run all automated tests of the desktop app
    python run.py --setup         (re)install the packages into the app's own folder (.venv)

Works from any folder:  python f:\\indigo\\run.py --demo
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent / "bestfare-desktop"
TARGET = APP / "run.py"


def main(argv: list[str]) -> int:
    if not TARGET.is_file():
        print(f"Cannot find {TARGET}\nThe bestfare-desktop folder must be next to this file.", flush=True)
        return 2
    try:
        return subprocess.call([sys.executable, str(TARGET), *argv], cwd=str(APP))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
