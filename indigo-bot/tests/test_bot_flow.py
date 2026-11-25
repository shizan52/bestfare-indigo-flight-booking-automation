"""What people see: welcome, credits, bookings, help, nudges - plus a booking followed all the way from the first tap to
the PNR arriving in the chat (fake Telegram + fake IndiGo engine, real handlers/worker/database)."""
import re

import pytest

import ui
from conftest import booking
from tg import buttons

pytestmark = pytest.mark.asyncio
CODES = re.compile("6ETST001|PROMO2")


async def member(env, uid=300, credits=5, name="Rahul"):
    await env.db.upsert_user(uid, name.lower(), name)
    if credits:
        await env.db.add_credits(uid, credits, "seed")
    return env.person(uid, name, name.lower())


async def test_a_new_person_sees_a_friendly_waiting_card(tgenv):
    env = tgenv
    s = env.person(777, "Sam", "sam")
    await s.say("/start")
    text = s.last()
    assert "Welcome to Best Fare" in text and "waiting for approval" in text and "<code>777</code>" in text
    assert [b[0] for b in buttons(env.wire.sent_to(777)[-1]["markup"])] == [ui.MENU_STATUS, ui.MENU_HELP]
    await s.say(ui.MENU_STATUS)
    assert "Still waiting for approval" in s.last()
    await s.say(ui.MENU_BOOK)
    assert "waiting for approval" in s.last()
    await s.say("anything at all")
    assert "waiting for approval" in s.last()
    assert [p["telegram_id"] for p in await env.db.pending_users()] == [777]


async def test_welcome_shows_the_credits_and_one_big_book_button(tgenv):
    env = tgenv
    u = await member(env, credits=5)
    await u.say("/start")
    msg = env.wire.sent_to(300)[-1]
    assert "Hi Rahul!" in msg["text"] and "Welcome to Best Fare" in msg["text"] and "<b>5 credits</b>" in msg["text"]
    assert msg["markup"].keyboard[0][0].text == ui.MENU_BOOK and len(msg["markup"].keyboard[0]) == 1   # alone on top
    assert [b[0] for b in buttons(msg["markup"])] == [ui.MENU_BOOK, ui.MENU_BOOKINGS, ui.MENU_CREDITS, ui.MENU_HELP]
    z = await member(env, 301, credits=0, name="Zed")
    await z.say("/start")
    assert "no credits yet" in z.last()


async def test_an_admin_start_registers_the_admin_and_adds_the_panel_button(tgenv):
    env = tgenv
    a = env.person(1001, "Boss", "boss")
    await a.say("/start")
    msg = env.wire.sent_to(1001)[-1]
    assert (await env.db.get_user(1001))["is_admin"] and "Admin panel" in msg["text"]
    assert ui.MENU_ADMIN in [b[0] for b in buttons(msg["markup"])]
    await a.say(ui.MENU_ADMIN)
    assert "Admin panel" in a.card_text()


async def test_credits_screen_lists_recent_activity(tgenv):
    env = tgenv
    u = await member(env, credits=5)
    await env.db.add_credits(300, -2, "admin adjustment", admin_tg_id=1001)
    await u.say(ui.MENU_CREDITS)
    text = u.last()
    assert "You have <b>3 credits</b>" in text and "<b>-2</b> · Adjusted by admin" in text
    assert "Book a flight" in " ".join(u.labels())
    await u.say("/balance")
    assert "Your credits" in u.last()


async def test_my_bookings_explains_every_state_in_plain_words(tgenv):
    env = tgenv
    u = await member(env, credits=9)
    await u.say(ui.MENU_BOOKINGS)
    assert "Nothing here yet" in u.last()
    for jid in ("D1", "D2", "F1", "R1", "P1"):
        await env.db.create_booking_charged(user_tg_id=300, bookings=[booking(jid)], total_cost=1)
    await env.db.mark_done("D1", "ABC123", "record_locator", None, 5132.0, True, "2999-01-01T00:00:00Z")
    await env.db.mark_done("D2", "OLD999", "record_locator", None, 5132.0, True, "2000-01-01T00:00:00Z")
    await env.db.mark_failed_and_refund("F1", "no flights available")
    await env.db.mark_running("R1")
    await u.say("/mybookings")
    text = u.last()
    assert "ABC123" in text and "Pay before" in text
    assert "OLD999" in text and "hold has ended" in text
    assert "credits are back" in text and "Booking it now" in text and "Waiting for its turn" in text
    assert "Delhi → Mumbai" in text and "Offer 1" in text and not CODES.search(text)
    mid = env.wire.card(300)["mid"]
    await u.say("anything")                                                       # something else lands in between
    before = len(env.wire.events)
    await u.tap("m:refresh", mid=mid)                                             # nothing changed: no new message at all
    assert len(env.wire.events) == before
    await env.db.mark_done("P1", "NEW456", "record_locator", None, 4000.0, True, "2999-01-01T00:00:00Z")
    await u.tap("m:refresh", mid=mid)                                             # something changed: the SAME message updates
    last = env.wire.events[-1]
    assert last["kind"] == "edit" and last["mid"] == mid and "NEW456" in last["text"]


async def test_a_queued_booking_can_be_cancelled_for_a_full_refund(tgenv):
    from indigo_client import BookingResult
    env = tgenv
    u = await member(env, credits=5)
    for jid in ("Q1", "Q2", "R1"):
        await env.db.create_booking_charged(user_tg_id=300, bookings=[booking(jid, pax=1)], total_cost=1)
    await env.db.mark_running("R1")                                               # this one has already started
    assert (await env.db.get_user(300))["credits"] == 2
    await u.say(ui.MENU_BOOKINGS)
    cancels = [lab for lab in u.labels() if lab.startswith("❌ Cancel")]
    assert len(cancels) == 2 and "Delhi → Mumbai" in cancels[0] and "you can cancel it below" in u.card_text()
    await u.tap("m:cx:Q1")
    assert (await env.db.get_user(300))["credits"] == 3 and (await env.db.get_booking("Q1"))["status"] == "failed"
    assert "Cancelled" in env.wire.answers[-1][0] and len([lab for lab in u.labels() if lab.startswith("❌ Cancel")]) == 1
    await u.tap("m:cx:Q1")                                                        # a second tap cannot refund twice
    assert (await env.db.get_user(300))["credits"] == 3 and "Too late" in env.wire.answers[-1][0]
    await u.tap("m:cx:R1")                                                        # started: too late
    assert (await env.db.get_booking("R1"))["status"] == "running" and (await env.db.get_user(300))["credits"] == 3
    other = await member(env, 301, credits=0, name="Other")
    await other.tap("m:cx:Q2")                                                    # somebody else's booking: refused
    assert (await env.db.get_booking("Q2"))["status"] == "pending" and (await env.db.get_user(301))["credits"] == 0
    assert [r["reason"] for r in await env.db.get_ledger(300)].count("refund: cancelled by you") == 1
    calls = []

    async def engine(req, promo):
        calls.append(promo)
        return BookingResult(success=False, error="should never run")
    env.worker._runner = engine
    await env.worker.process("Q1")                                                # the worker skips a cancelled job
    assert calls == []


async def test_unknown_commands_and_non_text_messages_get_a_kind_answer(tgenv):
    env = tgenv
    u = await member(env)
    await u.say("/frobnicate")
    assert "do not know that command" in u.last()
    s = env.person(999, "Eve")
    await s.say("/frobnicate")
    assert "waiting for approval" in s.last()                                     # strangers still get the waiting card
    from telegram import Chat, Message, Update
    import tg
    msg = Message(message_id=7000, date=None, chat=Chat(300, "private"), from_user=u.user, photo=[])
    msg.set_bot(env.app.bot)
    await env.app.process_update(Update(next(tg._update_ids), message=msg))
    assert "only read text" in u.last()


async def test_help_is_one_short_page(tgenv):
    env = tgenv
    u = env.person(5, "Anyone")                                                   # help is open to everybody
    await u.say("/help")
    text = u.card_text()
    assert "How Best Fare works" in text and "Book now" in text and "Pay Now" in text and not CODES.search(text)
    assert u.labels() == ["✈️ Book a flight"]
    mid = env.wire.card(5)["mid"]
    await u.tap("h:offers", mid=mid)                                              # a button from the old multi-page help
    assert "How Best Fare works" in env.wire.messages[(5, mid)]["text"]


async def test_unknown_messages_get_a_gentle_nudge_not_silence(tgenv):
    env = tgenv
    await member(env)
    await env.person(300, "Rahul").say("what is the weather")
    assert "did not get that" in env.wire.last(300)


async def test_from_the_first_tap_to_the_pnr_in_the_chat(tgenv):
    """The whole chain, with fakes at both ends: Telegram taps -> credits -> queue -> worker -> engine -> PNR delivered to
    the same person, with 'My bookings' / 'Book another' buttons - and no coupon code shown anywhere."""
    from indigo_client import BookingResult
    env = tgenv
    u = await member(env, credits=3)
    await u.say(ui.MENU_BOOK)
    await u.press("Delhi"); await u.press("Mumbai"); await u.press("Tomorrow")
    await u.cmd("n", "1")
    await u.say("rahul sharma"); await u.press("Male")
    await u.say("9876543210"); await u.say("me@example.com")
    await u.press("Offer 1")
    await u.press("Book now")
    assert (await env.db.get_user(300))["credits"] == 2

    seen = {}

    async def engine(req, promo):
        seen.update(origin=req.origin, dest=req.destination, promo=promo, n=len(req.passengers), phone=req.contact_phone,
                    name=(req.passengers[0].first_name, req.passengers[0].last_name, req.passengers[0].gender))
        return BookingResult(success=True, pnr="HGNQRK", kind="record_locator", fare_amount=5132, coupon_applied=True,
                             discount=1902.0, hold_until="2999-01-01T00:30:00Z")
    env.worker._runner = engine
    env.worker._notify = env.bot.make_notifier(env.app)
    job = (await env.db.get_user_bookings(300))[0]["job_id"]
    await env.worker.process(job)

    assert seen == {"origin": "DEL", "dest": "BOM", "promo": "6ETST001", "n": 1, "phone": "9876543210",
                    "name": ("RAHUL", "SHARMA", "M")}
    final = env.wire.sent_to(300)[-1]
    assert "Your booking is ready!" in final["text"] and "<code>HGNQRK</code>" in final["text"]
    assert "Delhi (DEL) → Mumbai (BOM)" in final["text"] and "Offer 1 applied" in final["text"]
    assert "you save <b>₹1,902</b>" in final["text"] and "₹5,132" in final["text"] and "Pay Now" in final["text"]
    assert [b[0] for b in buttons(final["markup"])] == ["📋 My bookings", "✈️ Book another"]
    await u.say(ui.MENU_BOOKINGS)
    assert "HGNQRK" in u.last()
    for e in env.wire.events:                                                     # the customer never sees a coupon code
        if e["chat"] == 300 and e["text"]:
            assert not CODES.search(e["text"]) and not any(CODES.search(b[0]) for b in buttons(e["markup"]))


async def test_a_failed_booking_is_refunded_and_explained_kindly(tgenv):
    from indigo_client import BookingResult
    env = tgenv
    await member(env, credits=2)
    await env.db.create_booking_charged(user_tg_id=300, bookings=[booking("X1")], total_cost=1)

    async def engine(req, promo):
        return BookingResult(success=False, error="no flights available for this route/date")
    env.worker._runner = engine
    env.worker._notify = env.bot.make_notifier(env.app)
    await env.worker.process("X1")
    final = env.wire.sent_to(300)[-1]
    assert "could not book this one" in final["text"] and "No flights are available" in final["text"]
    assert "refunded automatically" in final["text"] and "route/date" not in final["text"]   # no technical detail
    assert (await env.db.get_user(300))["credits"] == 2
    assert "✈️ Book another" in [b[0] for b in buttons(final["markup"])]


async def test_nothing_the_bot_says_is_bengali_or_broken_html(tgenv):
    env = tgenv
    u = await member(env, credits=3)
    for line in [ui.MENU_BOOK, "DELHI", "qwerty", "/cancel", "/start", ui.MENU_CREDITS, ui.MENU_BOOKINGS, "/help", "bad"]:
        await u.say(line)
    await env.person(55, "Eve").say("/start")
    a = env.person(1001, "Boss")
    for line in ["/admin", "/users", "/stats", "/pending", "/health"]:
        await a.say(line)
    assert env.wire.events
    for e in env.wire.events:
        if e["text"]:
            assert not re.search("[ঀ-৿]", e["text"]), e["text"]
            assert len(e["text"]) < 4000
            assert e["text"].count("<b>") == e["text"].count("</b>"), e["text"]          # balanced tags: Telegram would reject
