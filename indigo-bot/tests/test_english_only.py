"""The bot always speaks English: no Bengali characters in anything it can send (the source of every user-facing module,
the command menus, and every message captured while the conversation tests run)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENGALI = re.compile("[ঀ-৿]")
MODULES = ("bot.py", "admin.py", "wizard.py", "common.py", "ui.py", "airports.py", "worker.py", "indigo_client.py",
           "automation.py", "coupons.py")


def test_user_facing_sources_contain_no_bengali():
    for name in MODULES:
        text = (ROOT / name).read_text(encoding="utf-8")
        hit = BENGALI.search(text)
        assert not hit, f"{name} contains Bengali text near: {text[max(0, hit.start() - 30):hit.start() + 30]!r}"


def test_command_menu_and_descriptions_are_english():
    import bot
    for _cmd, desc in bot.PUBLIC_COMMANDS + bot.ADMIN_COMMANDS:
        assert not BENGALI.search(desc) and desc.strip()
