"""Where Best Fare keeps things on the user's computer, and where the shared IndiGo engine lives.

* The IndiGo engine (raw CDP client, human-like input, coupon rules, the booking flow and the PNR step) is the SAME code the
  Telegram bot runs on the server - it lives in ../indigo-bot and is imported from there, so a fix for IndiGo is made once.
  A packaged build (later) bundles those modules, so nothing has to be found on disk then.
* Everything the app writes (history, settings, logs, its private Edge profile) goes to one folder per Windows user:
  %LOCALAPPDATA%\\BestFare  (BESTFARE_HOME overrides it - the tests use that).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Best Fare"
APP_DIR = Path(__file__).resolve().parent
ENGINE_DIR = APP_DIR.parent / "indigo-bot"
ENGINE_MODULES = ("cdp", "human", "iframe_tools", "coupons", "indigo_client", "psp_flow", "airports")


def use_engine() -> None:
    """Make the shared engine importable. Appended (not prepended) so the app's own modules always win a name clash."""
    if getattr(sys, "frozen", False):
        return                                   # a packaged build already contains the engine modules
    try:
        import packed
        if packed.ACTIVE:
            return                               # a release opened in memory carries the engine modules itself
    except ImportError:
        pass
    p = str(ENGINE_DIR)
    if ENGINE_DIR.is_dir() and p not in sys.path:
        sys.path.append(p)


def data_dir() -> Path:
    base = os.getenv("BESTFARE_HOME") or os.path.join(os.getenv("LOCALAPPDATA") or str(Path.home()), "BestFare")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path(demo: bool = False) -> Path:
    """Practice mode keeps its own history file, so it never mixes with real bookings."""
    return data_dir() / ("bestfare-demo.db" if demo else "bestfare.db")


def log_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def edge_profile() -> Path:
    """The app's own Edge profile. It keeps IndiGo's trust cookies between runs - it is never the user's normal Edge."""
    p = data_dir() / "edge-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p
