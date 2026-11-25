"""The admin panel: approvals, credits, users, pause - and the guarantee that only admins can use any of it."""
import pytest

import ui

pytestmark = pytest.mark.asyncio

ADMIN = 1001                                                    # from ADMIN_IDS in conftest


def boss(env):
    return env.person(ADMIN, "The Boss", "boss")


async def member(env, uid=55, credits=2, name="Maya"):
    await env.db.upsert_user(uid, name.lower(), name)
    if credits:
        await env.db.add_credits(uid, credits, "seed")
    return env.person(uid, name, name.lower())


async def open_user_card(a, label="Maya"):
    await a.say("/admin")
    await a.press("Users")
    await a.press(label)


async def test_a_stranger_who_presses_start_reaches_the_admins_with_one_tap_approval(tgenv):
    env = tgenv
    s = env.person(777, "Sam", "sam")
    await s.say("/start")
    assert "waiting for approval" in s.last() and "777" in s.last()
    alerts = [t for t in env.wire.texts(ADMIN) if "New access request" in t]
    assert len(alerts) == 1 and "Sam" in alerts[0] and "777" in alerts[0]
    assert "a:ok:777:5" in [d for _t, d in __import__("tg").buttons(env.wire.card(ADMIN)["markup"])]
    await s.say("/start")                                                         # pressing it again is not a new alert
    assert len([t for t in env.wire.texts(ADMIN) if "New access request" in t]) == 1

    a = boss(env)
    await a.press("+5")
    rec = await env.db.get_user(777)
    assert rec["is_active"] and rec["credits"] == 5 and rec["username"] == "sam"
    assert "Welcome to Best Fare" in env.wire.texts(777)[-1] and "<b>5 credits</b>" in env.wire.texts(777)[-1]
    assert "Approved" in a.card_text()                                            # the alert became a receipt
    await a.press("+5") if a.find("+5") else None                                 # a second tap cannot double-pay
    assert (await env.db.get_user(777))["credits"] == 5
    await s.say(ui.MENU_BOOK)
    assert "Where are you flying from?" in s.card_text()                          # and they can book straight away


async def test_the_approvals_screen_walks_through_everybody_waiting(tgenv):
    env = tgenv
    for uid, name in ((801, "Ann"), (802, "Ben")):
        await env.person(uid, name, name.lower()).say("/start")
    a = boss(env)
    await a.say("/admin")
    assert "<b>2</b> waiting for approval" in a.card_text() and any("Approvals (2)" in label for label in a.labels())
    await a.press("Approvals")
    assert "1 of 2" in a.card_text()
    await a.press("Dismiss")                                                      # not welcome: gone, no account created
    assert "Waiting for approval" in a.card_text() and "1 of 1" not in a.card_text() or "Ann" in a.card_text() or "Ben" in a.card_text()
    left = (await env.db.pending_users())
    assert len(left) == 1
    await a.press("+3")
    assert "Nobody is waiting" in a.card_text()
    assert (await env.db.get_user(left[0]["telegram_id"]))["credits"] == 3


async def test_credit_buttons_change_the_balance_and_tell_the_person(tgenv):
    env = tgenv
    m = await member(env, 55, credits=2)
    a = boss(env)
    await open_user_card(a)
    assert "Maya" in a.card_text() and "Credits: <b>2</b>" in a.card_text()
    await a.press("➕ 5")
    assert (await env.db.get_user(55))["credits"] == 7 and "balance <b>7</b>" in a.card_text()
    assert "Credits updated" in env.wire.texts(55)[-1] and "+5" in env.wire.texts(55)[-1]
    await a.press("➖ 5")
    await a.press("➖ 1")
    await a.press("➖ 1")
    assert (await env.db.get_user(55))["credits"] == 0
    await a.press("➖ 1")                                                          # cannot go below zero
    assert (await env.db.get_user(55))["credits"] == 0 and "cannot go below 0" in env.wire.answers[-1][0]
    ledger = await env.db.get_ledger(55)
    assert [r["delta"] for r in ledger][:4] == [-1, -1, -5, 5] and ledger[0]["admin_tg_id"] == ADMIN
    del m


async def test_custom_amount_is_typed_into_the_chat(tgenv):
    env = tgenv
    await member(env, 55, credits=1)
    a = boss(env)
    await open_user_card(a)
    await a.press("Custom amount")
    assert "Send a number" in a.card_text()
    await a.say("lots")
    assert "whole number" in a.last() and (await env.db.get_user(55))["credits"] == 1
    await a.say("12")
    assert (await env.db.get_user(55))["credits"] == 13 and "Credits: <b>13</b>" in a.card_text()
    await a.say("12")                                                              # the prompt is over: just a message now
    assert (await env.db.get_user(55))["credits"] == 13


async def test_deactivate_and_reactivate_but_never_an_admin(tgenv):
    env = tgenv
    await member(env, 55)
    await env.db.set_admins_exactly([ADMIN])
    a = boss(env)
    await open_user_card(a)
    await a.press("Deactivate")
    assert not (await env.db.get_user(55))["is_active"] and "Deactivated" in a.card_text()
    await env.person(55, "Maya").say(ui.MENU_BOOK)
    assert "switched off" in env.wire.texts(55)[-1]                               # a clear answer, and no new alert for the admins
    await a.press("Activate")
    assert (await env.db.get_user(55))["is_active"]
    await a.tap(f"a:t:{ADMIN}")                                                    # try to switch an admin off
    assert (await env.db.get_user(ADMIN))["is_active"] and "cannot be deactivated" in env.wire.answers[-1][0]


async def test_pausing_bookings_stops_new_forms_and_resuming_reopens(tgenv):
    env = tgenv
    m = await member(env, 55, credits=3)
    a = boss(env)
    await a.say("/admin")
    assert "Bookings are <b>open</b>" in a.card_text()
    await a.press("Pause bookings")
    assert "Bookings are <b>paused</b>" in a.card_text() and "Open bookings" in " ".join(a.labels())
    await m.say(ui.MENU_BOOK)
    assert "paused" in m.last()
    await a.press("Open bookings")
    await m.say(ui.MENU_BOOK)
    assert "Where are you flying from?" in m.card_text()


FORGED = ["a:home", "a:ap:0", "a:ok:55:100", "a:ok:999:100:v", "a:no:55", "a:us:0", "a:u:55", "a:c:55:100", "a:c:55:-1",
          "a:cx:55", "a:t:55", "a:led:55", "a:lg", "a:bk:all", "a:st", "a:hl", "a:fix", "a:bkp", "a:pz", "a:find", "a:add",
          "a:nop"]


@pytest.mark.parametrize("data", FORGED)
async def test_a_normal_user_cannot_use_any_admin_button_even_if_they_forge_it(tgenv, data):
    env = tgenv
    m = await member(env, 55, credits=2)
    before_users = len(await env.db.get_all_users())
    await m.tap(data, mid=4242)                                                    # a hand-made callback, not from a card
    assert env.wire.answers[-1] == ("This is for admins only.", True)
    assert (await env.db.get_user(55))["credits"] == 2 and (await env.db.get_user(55))["is_active"]
    assert len(await env.db.get_all_users()) == before_users
    assert await env.db.get_setting("bookings_paused") is None
    assert not env.wire.sent_to(55) or all("admin" not in t.lower() or "only" in t.lower() for t in env.wire.texts(55))


async def test_an_unregistered_person_cannot_use_admin_buttons_either(tgenv):
    env = tgenv
    s = env.person(999, "Eve")
    await s.tap("a:ok:999:100", mid=4243)                                          # try to approve themself
    assert await env.db.get_user(999) is None
    await s.say("/addcredit 999 100")
    assert await env.db.get_user(999) is None


async def test_the_pick_a_person_button_adds_somebody_with_starting_credits(tgenv):
    env = tgenv
    a = boss(env)
    await a.say("/admin")
    await a.press("Add a user")
    kb = env.wire.sent_to(ADMIN)[-1]["markup"]
    assert "Pick a person" in str(kb)
    await a.share(555, "Amit", "amit")
    assert "Add Amit (@amit)" in a.card_text() and "Starting credits" in a.card_text()
    await a.press("+5")
    rec = await env.db.get_user(555)
    assert rec["is_active"] and rec["credits"] == 5 and rec["full_name"] == "Amit" and rec["username"] == "amit"
    assert "Welcome to Best Fare" in env.wire.texts(555)[-1]


async def test_adding_by_typing_an_id_and_a_person_who_never_started_the_bot(tgenv):
    env = tgenv
    env.wire.blocked.add(50006)                                                    # has never pressed Start
    a = boss(env)
    await a.say("/admin")
    await a.press("Add a user")
    await a.say("abc")
    assert "does not look like a Telegram ID" in a.last()
    await a.say("50006")
    await a.press("+3")
    assert (await env.db.get_user(50006))["credits"] == 3
    assert "could not message them yet" in a.card_text() and "press <b>Start</b>" in a.card_text()


async def test_find_a_person_by_name_username_or_id(tgenv):
    env = tgenv
    await member(env, 55, name="Maya")
    await member(env, 56, name="Mayank")
    a = boss(env)
    await a.say("/admin")
    await a.press("Users")
    await a.press("Find")
    await a.say("may")
    assert "2 result(s)" in a.card_text() and "Mayank" in " ".join(a.labels())
    await a.press("Search again")
    await a.say("@mayank")
    assert "Mayank" in a.card_text() and "🆔 <code>56</code>" in a.card_text()    # one hit: straight to the card
    await a.press("Users"); await a.press("Find"); await a.say("55")
    assert "Maya" in a.card_text() and "🆔 <code>55</code>" in a.card_text() and "Mayank" not in a.card_text()
    await a.press("Users"); await a.press("Find"); await a.say("nobody here")
    assert "Nobody matches" in a.card_text()


async def test_the_users_list_pages_and_marks_admins_and_deactivated(tgenv):
    env = tgenv
    for i in range(11):
        await member(env, 2000 + i, credits=i, name=f"User{i:02d}")
    await env.db.set_active(2003, False)
    await env.db.set_admins_exactly([ADMIN])
    a = boss(env)
    await a.say("/admin")
    await a.press("Users")
    assert "page 1 of 2" in a.card_text() and any("🔑" in label for label in a.labels())
    await a.press("▶")
    assert "page 2 of 2" in a.card_text()
    first = await env.db.users_page(0, 8)
    assert first[0]["is_admin"] == 1 or len(first) == 8


async def test_the_dashboard_screens_all_render(tgenv, monkeypatch):
    import automation
    env = tgenv
    await member(env, 55, credits=4)
    await env.db.create_booking_charged(user_tg_id=55, bookings=[__import__("conftest").booking("B1")], total_cost=1)
    await env.db.mark_done("B1", "PNR123", "record_locator", None, 5132.0, True, "2999-01-01T00:00:00Z")
    await env.db.create_booking_charged(user_tg_id=55, bookings=[__import__("conftest").booking("B2")], total_cost=1)
    await env.db.mark_failed_and_refund("B2", "no flights available for this route/date")

    async def up():
        return {"ok": True, "browser": "Edg/157", "tabs": 1, "title": "IndiGo", "cooldown_s": 0}

    async def down():
        return {"ok": False, "error": "connection refused"}
    a = boss(env)
    monkeypatch.setattr(automation, "health", up)
    await a.say("/admin")
    assert "Booking browser: <b>online</b>" in a.card_text() and "1 done" in a.card_text() and "1 failed" in a.card_text()
    await a.press("Statistics")
    assert "success rate <b>50%</b>" in a.card_text()
    await a.press("Admin panel")
    await a.press("Bookings")
    assert "PNR123" in a.card_text() and "no flights available" in a.card_text()
    await a.press("Failed")
    assert "PNR123" not in a.card_text() and "no flights" in a.card_text()
    await a.press("Admin panel")
    await a.press("Credit history")
    assert "Booking" in a.card_text() and "Refund" in a.card_text() and "Maya" in a.card_text()
    await a.press("Admin panel")
    monkeypatch.setattr(automation, "health", down)
    await a.press("Health")
    assert "offline" in a.card_text() and "start_trusted_edge.ps1" in a.card_text()
    await a.press("Admin panel")
    assert "Booking browser: <b>offline</b>" in a.card_text()


async def test_typed_admin_commands_still_work_and_stay_admin_only(tgenv):
    env = tgenv
    await env.db.set_admins_exactly([ADMIN])
    m = env.person(55, "Maya")
    for cmd in ["/addcredit 55 100", "/adduser 777 50", "/removeuser 55", "/users", "/pending", "/stats", "/ledger 55",
                "/bookings", "/health", "/admin"]:
        await m.say(cmd)
        assert "admins only" in m.last(), cmd
    assert await env.db.get_user(777) is None
    a = boss(env)
    await a.say("/adduser 4242 3")
    assert (await env.db.get_user(4242))["credits"] == 3 and (await env.db.get_user(4242))["is_active"]
    await a.say("/addcredit 4242 +2")
    assert (await env.db.get_user(4242))["credits"] == 5
    await a.say("/addcredit 4242 -9")
    assert (await env.db.get_user(4242))["credits"] == 5 and "cannot go below 0" in a.last()
    await a.say("/removeuser 4242")
    assert not (await env.db.get_user(4242))["is_active"]
    await a.say(f"/removeuser {ADMIN}")
    assert (await env.db.get_user(ADMIN))["is_active"]                            # admins cannot be removed
    for cmd in ["/users", "/pending", "/stats", "/bookings", "/health", "/ledger 4242"]:
        await a.say(cmd)                                                           # all render without error
