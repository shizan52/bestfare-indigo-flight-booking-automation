"""The booking wizard through the real handlers and a fake Telegram: few questions, plain words, buttons first,
typing always works, mistakes are explained, Back / Keep / Change fix things, and nothing stale can interfere."""
import re

import pytest

import ui

pytestmark = pytest.mark.asyncio
CODES = re.compile("6ETST001|PROMO2")


async def make_user(env, uid=300, credits=5, name="Rahul"):
    await env.db.upsert_user(uid, "rahul", name)
    if credits:
        await env.db.add_credits(uid, credits, "seed")
    return env.person(uid, name, "rahul")


async def trip(u):
    await u.say(ui.MENU_BOOK)
    await u.press("Delhi")
    await u.press("Mumbai")
    await u.press("Tomorrow")


async def passenger(u, name="RAHUL SHARMA", g="M"):
    await u.say(name)
    await u.cmd("g", g)


async def to_review(u, pax=1, offer="offer1", with_count=True):
    await trip(u)
    if with_count:
        await u.cmd("n", str(pax))
    for i in range(pax):
        await passenger(u, f"PAX{chr(65 + i)} SHARMA", "M" if i % 2 == 0 else "F")
    await u.say("9876543210")
    await u.say("me@example.com")
    await u.cmd("o", offer)


def user_saw(env, uid):
    """Every text and every button label the person was shown."""
    out = []
    for e in env.wire.events:
        if e["chat"] == uid and e["text"]:
            out.append(e["text"])
            from tg import buttons
            out += [b[0] for b in buttons(e["markup"])]
    return out


async def test_a_whole_booking_in_a_few_plain_steps(tgenv):
    env = tgenv
    u = await make_user(env)
    await u.say(ui.MENU_BOOK)
    assert "Where are you flying from?" in u.card_text() and "Step 1 of 5" in u.card_text() and "Delhi" in u.labels()
    await u.press("Delhi")
    assert "Where are you flying to?" in u.card_text() and "Delhi" not in u.labels()      # not the same city again
    await u.press("Mumbai")
    assert "When do you want to fly?" in u.card_text() and any(lab.startswith("Tomorrow · ") for lab in u.labels())
    await u.press("Tomorrow")
    assert "How many people are travelling?" in u.card_text() and "you have <b>5</b>" in u.card_text()
    await u.cmd("n", "2")
    assert "Passenger 1 of 2 — full name" in u.card_text() and "Step 2 of 5" in u.card_text()
    await u.say("rahul sharma")
    assert "RAHUL SHARMA — Male or Female?" in u.card_text()
    await u.press("Male")
    assert "Passenger 2 of 2" in u.card_text() and "SHARMA</code>" in u.card_text()        # the example re-uses the surname
    await u.say("Priya Sharma")
    await u.press("Female")
    assert "Your mobile number" in u.card_text() and "👤 PRIYA SHARMA · Female" in u.card_text()
    await u.say("+91 98765 43210")
    assert "Your email address" in u.card_text()
    await u.say("Me@Example.com")
    assert "Which offer do you want?" in u.card_text() and "Step 4 of 5" in u.card_text()
    await u.press("Both offers")
    text = u.card_text()
    assert "Check your booking" in text and "Delhi" in text and "Mumbai" in text and "<b>4 credits</b>" in text
    assert "👤 RAHUL SHARMA · Male" in text and "👤 PRIYA SHARMA · Female" in text
    assert "9876543210" in text and "me@example.com" in text and "confirm that every passenger is eligible" in text
    assert u.labels() == ["✅ Book now", "✏️ Change", "❌ Cancel"]
    await u.press("Book now")
    assert "Request received!" in u.card_text() and "#1" in u.card_text() and "Credits left: <b>1</b>" in u.card_text()
    assert (await env.db.get_user(300))["credits"] == 1                                    # 5 - (2 people x 2 offers)
    rows = await env.db.get_user_bookings(300)
    assert sorted(r["offer"] for r in rows) == ["offer1", "offer2"] and all(r["credit_cost"] == 2 for r in rows)
    assert rows[0]["contact_phone"] == "9876543210" and env.worker.queue_size() == 2
    assert '"first_name": "PRIYA", "last_name": "SHARMA", "gender": "F"' in rows[0]["passengers_json"]
    # one single card for the whole booking; the 4 typed answers were removed from the chat
    assert len([e for e in env.wire.events if e["chat"] == 300 and e["kind"] == "send"]) == 1
    assert len([e for e in env.wire.events if e["chat"] == 300 and e["kind"] == "delete"]) == 4
    assert not any(CODES.search(t) for t in user_saw(env, 300))                          # customers never see codes


async def test_with_one_credit_the_how_many_question_is_skipped(tgenv):
    u = await make_user(tgenv, credits=1)
    await trip(u)
    assert "Passenger — full name" in u.card_text() and "How many" not in u.card_text()
    await passenger(u)
    await u.press("Back"); await u.press("Back"); await u.press("Back")                     # phone -> gender -> name -> date
    assert "When do you want to fly?" in u.card_text()


async def test_typed_answers_work_just_like_buttons(tgenv):
    env = tgenv
    u = await make_user(env)
    await u.say("/book")
    await u.say("bangalore")
    await u.say("dll")                                                                    # unknown 3-letter code: asked first
    assert "Yes, use DLL" in u.labels()
    await u.press("Yes, use DLL")
    await u.say("tomorrow")
    await u.say("1")
    await u.say("amit kumar singh")
    await u.say("male")
    await u.say("9000000001")
    await u.say("a@b.co")
    await u.say("2")
    text = u.card_text()
    assert "Bengaluru" in text and "DLL" in text and "Offer 2 — Senior Citizen" in text and "<b>1 credit</b>" in text
    assert "👤 AMIT KUMAR SINGH · Male" in text
    wz = env.app.user_data[300]["wz"]
    assert wz["pax"][0]["fn"] == "AMIT KUMAR" and wz["pax"][0]["ln"] == "SINGH"            # the last word is the surname


async def test_an_ambiguous_city_asks_which_airport(tgenv):
    u = await make_user(tgenv)
    await u.say(ui.MENU_BOOK)
    await u.say("goa")
    assert "Which one do you mean?" in u.card_text() and "Goa Dabolim (GOI)" in u.labels() and "Goa Mopa (GOX)" in u.labels()
    await u.press("Mopa")
    assert "flying to" in u.card_text() and "Goa Mopa" in u.card_text()


async def test_mistakes_get_a_friendly_message_and_the_screen_stays(tgenv):
    u = await make_user(tgenv)
    await u.say(ui.MENU_BOOK)
    await u.say("qwertyuiop")
    assert "could not find that city" in u.card_text() and "flying from" in u.card_text()
    await u.say("DEL")
    await u.say("delhi")
    assert "already flying from there" in u.card_text() and "flying to" in u.card_text()
    await u.say("BOM")
    await u.say("01-01-2020")
    assert "already passed" in u.card_text()
    await u.say("31-02-2027")
    assert "does not look like a date" in u.card_text()
    await u.say("01-01-2099")
    assert "330 days" in u.card_text()
    await u.say("+5")
    await u.say("9")
    assert "Choose a number from 1 to" in u.card_text()
    await u.say("1")
    await u.say("RAHUL")
    assert "first and last name together" in u.card_text()
    await u.say("R4HUL SHARMA")
    assert "English letters only" in u.card_text()
    await u.say("RAHUL SHARMA")
    await u.say("robot")
    assert "tap Male or Female" in u.card_text()
    await u.say("m")
    await u.say("12345")
    assert "10-digit mobile number" in u.card_text()
    await u.say("9876543210")
    await u.say("not-an-email")
    assert "email address does not look right" in u.card_text()


async def test_quick_days_and_the_calendar(tgenv):
    env = tgenv
    u = await make_user(env)
    await u.say(ui.MENU_BOOK)
    await u.press("Delhi"); await u.press("Mumbai")
    days = [lab for lab in u.labels() if "·" in lab or re.match(r"[A-Z][a-z]{2} \d\d [A-Z][a-z]{2}$", lab)]
    assert len(days) == 8 and days[0].startswith("Today · ") and days[1].startswith("Tomorrow · ")
    await u.press("Pick another date")
    assert "Pick a day" in u.card_text() and "⬅️ Quick dates" in u.labels()
    await u.press("▶")                                                                    # next month
    nxt = env.app.user_data[300]["wz"]["cal"]
    await u.press("Quick dates")
    assert "When do you want to fly?" in u.card_text()
    await u.press("Pick another date")
    assert env.app.user_data[300]["wz"]["cal"] == nxt                                     # the calendar remembers the month
    await u.cmd("d", f"{nxt[0]}-{nxt[1]:02d}-15")
    assert "How many people" in u.card_text() and f"15 " in u.card_text()


async def test_back_and_keep_fix_an_earlier_answer(tgenv):
    u = await make_user(tgenv)
    await trip(u)
    await u.cmd("n", "1")
    await u.say("RAHUL SHARMA")
    assert "Male or Female" in u.card_text()
    await u.press("Back")
    assert "full name" in u.card_text() and "Keep: RAHUL SHARMA" in "".join(u.labels())
    await u.say("RAHIM SHAIKH")                                                           # a corrected answer replaces the old one
    assert "RAHIM SHAIKH — Male or Female?" in u.card_text()
    await u.press("Back")
    await u.press("Keep")                                                                 # keep it, move on
    assert "Male or Female" in u.card_text()
    for _ in range(6):
        if "Where are you flying from?" in u.card_text():
            break
        await u.press("Back")
    assert "Where are you flying from?" in u.card_text() and "Back" not in "".join(u.labels())   # nowhere further back
    assert "Keep: Delhi" in "".join(u.labels())


async def test_change_on_the_review_jumps_to_one_thing_and_comes_straight_back(tgenv):
    env = tgenv
    u = await make_user(env)
    await to_review(u)
    await u.press("Change")
    assert "What would you like to change?" in u.card_text()
    await u.press("Back to review")
    assert "Check your booking" in u.card_text()

    await u.press("Change"); await u.press("Date")
    assert "When do you want to fly?" in u.card_text() and "Back to review" in "".join(u.labels())
    day = [lab for lab in u.labels() if re.match(r"[A-Z][a-z]{2} \d\d [A-Z][a-z]{2}$", lab)][2]
    await u.press(day)
    assert "Check your booking" in u.card_text() and day[4:] in u.card_text()            # straight back, new day shown

    await u.press("Change"); await u.press("Contact")
    await u.say("9123456789")
    await u.press("Keep")                                                                 # same email
    assert "Check your booking" in u.card_text() and "9123456789" in u.card_text()

    await u.press("Change"); await u.press("Offer")
    await u.press("Offer 2")
    assert "Check your booking" in u.card_text() and "Offer 2 — Senior Citizen" in u.card_text()

    await u.press("Change"); await u.press("Passengers")
    await u.cmd("n", "2")                                                                 # add a second traveller
    await u.press("Keep"); await u.press("Keep")                                          # passenger 1 unchanged
    await passenger(u, "NEW PERSON", "F")
    assert "Check your booking" in u.card_text() and "👤 NEW PERSON · Female" in u.card_text() and "<b>2 credits</b>" in u.card_text()

    await u.press("Change"); await u.press("Cities")
    await u.press("Kolkata")
    await u.press("Keep")
    assert "Check your booking" in u.card_text() and "Kolkata" in u.card_text()
    await u.press("Book now")
    rows = await env.db.get_user_bookings(300)
    assert rows[0]["origin"] == "CCU" and rows[0]["pax_count"] == 2 and rows[0]["contact_phone"] == "9123456789"
    assert rows[0]["offer"] == "offer2"


async def test_a_button_on_an_old_card_cannot_change_the_new_booking(tgenv):
    u = await make_user(tgenv)
    await u.say(ui.MENU_BOOK)
    old = u.wire.card(300)["mid"]
    old_data = u.find("Delhi")
    await u.say(ui.MENU_BOOK)                                                             # a second booking replaces the first
    assert "this one is closed" in u.wire.messages[(300, old)]["text"]
    await u.tap(old_data, mid=old)
    assert u.wire.answers[-1][0].startswith("This screen is out of date") and u.wire.answers[-1][1] is True
    assert u.app.user_data[300]["wz"]["origin"] is None                                   # the stale tap changed nothing


async def test_an_abandoned_booking_expires(tgenv):
    u = await make_user(tgenv)
    await u.say(ui.MENU_BOOK)
    u.app.user_data[300]["wz"]["ts"] = 0                                                  # untouched since 1970
    await u.press("Delhi")
    assert "out of date" in u.wire.answers[-1][0]
    await u.say(ui.MENU_BOOK)
    u.app.user_data[300]["wz"]["ts"] = 0
    await u.say("hello")
    assert "left open for too long" in " ".join(u.wire.texts(300))


async def test_cancel_closes_the_booking(tgenv):
    u = await make_user(tgenv)
    await trip(u)
    await u.press("Cancel")
    assert "Booking cancelled" in " ".join(u.wire.texts(300)[-2:])
    assert "wz" not in u.app.user_data[300]
    await u.say("/cancel")
    assert "Nothing to cancel" in u.last()
    await to_review(u)
    await u.press("Cancel")                                                               # also from the review
    assert "Booking cancelled" in u.wire.texts(300)[-1]


async def test_people_without_credits_or_during_a_pause_are_told_kindly(tgenv):
    env = tgenv
    broke = await make_user(env, 301, credits=0)
    await broke.say(ui.MENU_BOOK)
    assert "no credits yet" in broke.last()
    rich = await make_user(env, 302, credits=3)
    await env.db.set_setting("bookings_paused", "1")
    await rich.say(ui.MENU_BOOK)
    assert "paused" in rich.last()
    await env.db.set_setting("bookings_paused", "0")
    await to_review(rich)
    await env.db.set_setting("bookings_paused", "1")                                      # paused while it was open
    await rich.press("Book now")
    assert "paused" in rich.wire.answers[-1][0] and (await env.db.get_user(302))["credits"] == 3


async def test_too_many_people_or_offers_for_the_balance_is_refused_without_charging(tgenv):
    env = tgenv
    u = await make_user(env, credits=3)
    await trip(u)
    await u.cmd("n", "4")                                                                 # 4 people, 3 credits
    assert "You have 3 credits, but 4 passengers need 4" in u.card_text()
    await u.cmd("n", "2")
    await passenger(u, "A B"); await passenger(u, "C D", "F")
    await u.say("9000000001"); await u.say("a@b.co")
    assert "needs 4 credits" in " ".join(u.labels())                                      # 'Both' would cost 4
    await u.press("Both offers")
    assert "needs 4 credits" in env.wire.answers[-1][0] and "Which offer" in u.card_text()
    assert (await env.db.get_user(300))["credits"] == 3


async def test_the_review_explains_who_each_offer_is_for(tgenv):
    u = await make_user(tgenv)
    await to_review(u, offer="offer2")
    assert "Senior Citizen fare is for passengers aged 60 or over" in u.card_text()
    await u.press("Change"); await u.press("Offer"); await u.press("Offer 1")
    assert "Promotional fare A may need a valid ID" in u.card_text() and "Senior Citizen fare" not in u.card_text()


async def test_pressing_book_now_twice_books_only_once(tgenv):
    env = tgenv
    u = await make_user(env)
    await to_review(u)
    data = u.find("Book now")
    await u.tap(data)
    await u.tap(data)                                                                     # a double tap
    assert (await env.db.get_user(300))["credits"] == 4
    assert len(await env.db.get_user_bookings(300)) == 1
    assert "out of date" in env.wire.answers[-1][0]


async def test_people_with_too_many_open_bookings_must_wait(tgenv):
    from conftest import booking
    env = tgenv
    u = await make_user(env, credits=9)
    for i in range(3):
        await env.db.create_booking_charged(user_tg_id=300, bookings=[booking(f"OPEN{i}")], total_cost=1)
    await u.say(ui.MENU_BOOK)
    assert "still being made" in u.last()


async def test_a_blocked_chat_never_crashes_the_wizard(tgenv):
    env = tgenv
    await make_user(env)
    env.wire.blocked.add(300)                                                             # the person blocked the bot
    await env.person(300, "Rahul").say(ui.MENU_BOOK)                                      # must not raise
    assert not env.wire.sent_to(300)
