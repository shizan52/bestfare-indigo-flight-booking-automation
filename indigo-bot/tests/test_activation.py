"""Start-up: the bot's name/description/menus, the one-time 'what's new' for admins, and the browser watchdog."""
import asyncio
import re

import pytest

import ui
from tg import buttons

pytestmark = pytest.mark.asyncio


async def test_activation_publishes_the_best_fare_profile_and_menus(tgenv, monkeypatch):
    import config
    env = tgenv
    monkeypatch.setattr(config, "ADMIN_IDS", [1001, 1002])
    await env.bot.activate(env.app)
    p = env.wire.profile
    assert p["name"] == "Best Fare"
    assert 0 < len(p["short"]) <= 120 and 0 < len(p["description"]) <= 512             # Telegram's limits
    for public in (p["description"], p["short"]):                                       # coupon codes are never public
        assert "6ETST001" not in public and "PROMO2" not in public and "Defence" in p["description"]
    everyone, admin = p["commands"][None], p["commands"][1001]
    assert everyone == ["start", "book", "balance", "mybookings", "cancel", "help"]
    assert {"admin", "adduser", "addcredit", "removeuser", "users", "pending", "ledger", "bookings", "stats", "health"} <= set(admin)
    assert p["commands"][1002] == admin
    for cmd, desc in env.bot.ADMIN_COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", cmd) and 1 <= len(desc) <= 256
    assert sorted(u["telegram_id"] for u in await env.db.get_all_users() if u["is_admin"]) == [1001, 1002]


async def test_restarting_does_not_repeat_the_profile_changes_or_the_greeting(tgenv, monkeypatch):
    import config
    env = tgenv
    monkeypatch.setattr(config, "ADMIN_IDS", [1001, 1002])
    env.wire.blocked.add(1002)                                                           # has not pressed Start yet
    await env.bot.activate(env.app)
    greeted = [e for e in env.wire.sent_to(1001) if "even simpler" in e["text"]]
    assert len(greeted) == 1 and ui.MENU_ADMIN in [b[0] for b in buttons(greeted[0]["markup"])]
    assert not env.wire.sent_to(1002) and await env.db.get_setting("ui_notified_1002") is None
    await env.bot.activate(env.app)                                                      # restart
    assert len([e for e in env.wire.sent_to(1001) if "even simpler" in e["text"]]) == 1
    assert env.wire.profile["name_calls"] == 1                                           # unchanged name is not re-sent
    env.wire.blocked.discard(1002)                                                       # now they pressed Start
    await env.bot.activate(env.app)
    assert len([e for e in env.wire.sent_to(1002) if "even simpler" in e["text"]]) == 1


async def test_everything_published_is_english(tgenv):
    env = tgenv
    bengali = re.compile("[ঀ-৿]")
    for text in (env.bot.SHORT_DESCRIPTION, env.bot.DESCRIPTION, env.bot.WHATS_NEW, ui.BRAND, ui.HELP_TEXT,
                 *ui.MENU_TEXTS, *[d for _c, d in env.bot.ADMIN_COMMANDS]):
        assert not bengali.search(text) and text.strip()


async def test_the_watchdog_tells_admins_once_when_the_browser_goes_down_and_once_when_it_is_back(tgenv, monkeypatch):
    import automation
    env = tgenv
    answers = iter([True, True, False, False, False, False, True])

    async def health():
        return {"ok": next(answers, True)}

    async def cannot_fix(reason="", force=False):                       # the doctor tries, but nobody is logged in
        return {"ok": False, "actions": [], "detail": "Edge could not be started — is somebody logged in?"}
    monkeypatch.setattr(automation, "health", health)
    monkeypatch.setattr(automation, "heal", cannot_fix)
    task = asyncio.create_task(env.bot._health_watch(env.app, every=0.01))
    await asyncio.sleep(0.5)
    task.cancel()
    texts = env.wire.texts(1001)
    assert sum("offline" in t for t in texts) == 1 and sum("back online" in t for t in texts) == 1
    assert texts.index(next(t for t in texts if "offline" in t)) < texts.index(next(t for t in texts if "back online" in t))
    assert any("could not be repaired automatically" in t and "logged in" in t for t in texts)


async def test_one_failed_health_check_is_not_an_alarm(tgenv, monkeypatch):
    import automation
    env = tgenv
    answers = iter([True, False, True, True])

    async def health():
        return {"ok": next(answers, True)}

    async def never(reason="", force=False):
        raise AssertionError("one bad check must not start a repair")
    monkeypatch.setattr(automation, "health", health)
    monkeypatch.setattr(automation, "heal", never)
    task = asyncio.create_task(env.bot._health_watch(env.app, every=0.01))
    await asyncio.sleep(0.3)
    task.cancel()
    assert not env.wire.texts(1001)
