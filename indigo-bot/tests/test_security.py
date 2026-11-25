"""Security: only admins manage credits/users; secrets never in code; hostile input is inert."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


async def test_unregistered_and_normal_users_are_not_admin(db):
    await db.upsert_user(55)
    assert not await db.is_admin(55)
    assert not await db.is_admin(99999)
    assert await db.is_admin(1001)                       # from ADMIN_IDS env


async def test_deactivated_admin_loses_power(db):
    await db.upsert_user(66); await db.set_admin(66, True)
    assert await db.is_admin(66)
    await db.set_active(66, False)
    assert not await db.is_admin(66)


async def test_db_admin_flags_always_mirror_env_exactly(db):
    """Only the people listed in .env ADMIN_IDS are admins; everyone else loses the flag at start-up."""
    await db.upsert_user(70); await db.set_admin(70, True)           # a stray admin flag from the past
    await db.set_admins_exactly([1001, 1002])
    assert not await db.is_admin(70)
    admins = sorted(u["telegram_id"] for u in await db.get_all_users() if u["is_admin"])
    assert admins == [1001, 1002]


async def test_admin_rows_keep_the_name_they_messaged_the_bot_with(db):
    await db.note_seen(1001, "boss", "The Boss")
    await db.set_admins_exactly([1001, 1002])                        # 1002 never messaged: no name, still an admin
    boss, other = await db.get_user(1001), await db.get_user(1002)
    assert boss["is_admin"] and boss["username"] == "boss" and boss["full_name"] == "The Boss"
    assert other["is_admin"] and other["username"] is None


UI_MODULES = ("bot.py", "admin.py", "wizard.py", "common.py", "ui.py")


def test_there_is_no_way_to_create_an_admin_through_the_bot():
    for name in UI_MODULES:
        src = (ROOT / name).read_text(encoding="utf-8")
        assert "setadmin" not in src.lower() and "claimadmin" not in src.lower(), name
        # set_admin( appears once, in cmd_start, and only for people already listed in the server's ADMIN_IDS
        assert src.count("set_admin(") == (1 if name == "bot.py" else 0), name
    start = (ROOT / "bot.py").read_text(encoding="utf-8").split("async def cmd_start(")[1].split("\nasync def ")[0]
    assert "u.id in config.ADMIN_IDS" in start and start.index("config.ADMIN_IDS") < start.index("set_admin(")
    assert not hasattr(__import__("db"), "claim_admin")


async def test_seen_users_lists_unregistered_contacts_for_approval(db):
    await db.note_seen(501, "alice", "Alice A")
    await db.note_seen(501, "alice", "Alice A")                      # counted, not duplicated
    await db.note_seen(502, None, "Bob")
    await db.upsert_user(502)                                        # Bob gets approved -> no longer pending
    pend = await db.pending_users()
    assert [p["telegram_id"] for p in pend] == [501] and pend[0]["msg_count"] == 2

async def test_sql_injection_in_names_is_stored_inertly(db):
    evil = "x'); DROP TABLE users;--"
    await db.upsert_user(90, evil, evil)
    assert (await db.get_user(90))["username"] == evil
    assert len(await db.get_all_users()) >= 1            # table still exists


def test_no_secrets_hardcoded_in_source():
    tok = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}")
    for f in ROOT.glob("*.py"):
        assert not tok.search(f.read_text(encoding="utf-8")), f"telegram token found in {f.name}"
    for f in ROOT.glob("*.example"):
        assert not tok.search(f.read_text(encoding="utf-8"))


def test_env_file_is_gitignored():
    gi = (ROOT / ".gitignore").read_text()
    assert ".env" in gi and "data/" in gi


def test_bot_gates_every_admin_command():
    """Every command handler that is not on the public list MUST start with the admin gate."""
    sources = {n: (ROOT / n).read_text(encoding="utf-8") for n in UI_MODULES}
    public = {"cmd_start", "cmd_help", "cmd_balance", "cmd_mybookings", "cmd_book", "cmd_cancel"}
    found = {}
    for name, src in sources.items():
        for fn in re.findall(r"async def (cmd_\w+)\(", src):
            found[fn] = (name, src.split(f"async def {fn}(")[1].split("\nasync def ")[0])
    admin = {fn for fn in found if fn not in public}
    assert admin == {"cmd_admin", "cmd_adduser", "cmd_removeuser", "cmd_addcredit", "cmd_users", "cmd_pending",
                     "cmd_ledger", "cmd_bookings", "cmd_stats", "cmd_health", "cmd_backup"}, admin
    for fn in admin:
        name, body = found[fn]
        assert name == "admin.py", f"{fn} should live in admin.py"
        first_lines = "\n".join(body.splitlines()[1:3])
        assert "if not await common.admin_only(update, ctx):" in first_lines, f"{fn} is missing the admin gate"


def test_every_other_admin_entry_point_checks_the_admin_flag_first():
    """Buttons (callback data can be forged), shared-contact messages and typed prompts are all admin entry points."""
    src = (ROOT / "admin.py").read_text(encoding="utf-8")
    for fn, marker in (("on_callback", "common.is_admin(update)"), ("on_users_shared", "common.is_admin(update)"),
                       ("handle_text", "common.is_admin(update)")):
        body = src.split(f"async def {fn}(")[1].split("\nasync def ")[0]
        assert marker in body, f"admin.{fn} does not check the admin flag"
        assert body.index(marker) < body.index("await ") + 400 or fn == "handle_text", fn
    router = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert 'data.startswith("a:")' in router and "admin.on_callback" in router        # 'a:' buttons only reach the gate
