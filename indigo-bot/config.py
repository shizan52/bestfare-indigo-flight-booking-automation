"""Central configuration. All secrets come from BASE_DIR/.env (never hard-coded)."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:                       # dotenv optional at import time
    def load_dotenv(*_a, **_k):         # type: ignore
        return False

# ── Paths ─────────────────────────────────────────────────
BASE_DIR = Path(os.getenv("INDIGO_BASE_DIR", r"D:\vps\apps\indigo-bot" if os.name == "nt" else "/opt/indigo-bot"))
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "indigo.db"
LOG_PATH = DATA_DIR / "bot.log"

load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# ── Telegram ──────────────────────────────────────────────
BOT_TOKEN = (os.getenv("BOT_TOKEN", "") or "").strip()
ADMIN_IDS: list[int] = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()]

# ── Coupons (identical to the coupon extension: promo code only, passenger type stays ADT) ──
COUPONS = {"offer1": "6ETST001", "offer2": "PROMO2"}
OFFER_LABEL = {"offer1": "Offer 1 - 6ETST001 (promo A)", "offer2": "Offer 2 - PROMO2 (promo B)"}

# ── Booking engine ────────────────────────────────────────
# IndiGo allows one booking session per trusted browser, so the engine runs ONE booking at a
# time (the queue absorbs any burst). Add more trusted browsers (CDP_ENDPOINTS) to scale.
CDP_ENDPOINTS = [e.strip() for e in os.getenv("CDP_ENDPOINTS", "http://127.0.0.1:9222").split(",") if e.strip()]
MAX_PAX = _int("MAX_PAX", 6)
MAX_OPEN_JOBS_PER_USER = _int("MAX_OPEN_JOBS_PER_USER", 3)
MAX_QUEUE = _int("MAX_QUEUE", 200)
BOOKING_CLEANUP_HOURS = _int("BOOKING_CLEANUP_HOURS", 2)
BLOCK_COOLDOWN_SECONDS = _int("BLOCK_COOLDOWN_SECONDS", 900)
PNR_HOLD_MINUTES = 30
DEBUG_BOOKING = os.getenv("DEBUG_BOOKING", "false").lower() in ("1", "true", "yes")

# ── Backups: a daily copy of the money records WITHOUT any personal booking data ───────
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_KEEP = _int("BACKUP_KEEP", 14)

# ── Self-healing of the trusted browser (doctor.py) ───────
# The Windows task that starts Edge in the logged-in desktop (created by deploy_box.py), the profile it uses, and
# whether the bot may start / restart Edge by itself when it is down.
EDGE_TASK = os.getenv("EDGE_TASK", "BestFare-Edge")
EDGE_PROFILE = os.getenv("EDGE_PROFILE", str(BASE_DIR / "cdpprofile"))
EDGE_AUTOSTART = os.getenv("EDGE_AUTOSTART", "true" if os.name == "nt" else "false").lower() in ("1", "true", "yes")

