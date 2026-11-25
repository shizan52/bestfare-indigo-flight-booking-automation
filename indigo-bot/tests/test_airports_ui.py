"""Airport input, dates, keyboards: the small pure pieces every screen is built from."""
import re
from datetime import date, timedelta

import airports
import ui

TODAY = date(2026, 11, 17)


def test_airports_are_found_by_code_city_and_nickname():
    assert airports.resolve("DEL") == ["DEL"] and airports.resolve("del") == ["DEL"]
    assert airports.resolve("bangalore") == ["BLR"] and airports.resolve("Bengaluru") == ["BLR"]
    assert airports.resolve("  Vizag ") == ["VTZ"] and airports.resolve("cochin") == ["COK"]
    assert airports.resolve("mum") == ["BOM"]                                  # a prefix is enough when it is unique
    assert set(airports.resolve("goa")) == {"GOI", "GOX"}                      # two airports -> the person is asked
    assert airports.resolve("zzzz") == [] and airports.resolve("") == []


def test_every_button_airport_is_known():
    for code in airports.POPULAR + airports.SPARE:
        assert airports.known(code), code
    assert airports.label("DEL") == "Delhi (DEL)" and airports.label("XYZ") == "XYZ"
    assert len(airports.POPULAR) == 12                                          # a full 3 x 4 grid


def test_dates_are_understood_in_the_ways_people_type_them():
    p = lambda s, **k: ui.parse_date(s, TODAY, **k)                             # noqa: E731
    assert p("25-12-2026") == p("25/12/2026") == p("25.12.2026") == p("25 Dec 2026") == p("25 december") == date(2026, 12, 25)
    assert p("tomorrow") == date(2026, 11, 18) and p("today") == TODAY and p("+7") == date(2026, 11, 24)
    assert p("10 Jan") == date(2027, 1, 10)                                     # no year: next time that day comes
    assert p("15-03-1950", relative=False, no_year=False) == date(1950, 3, 15)
    assert p("31-02-2026") is None and p("hello") is None and p("") is None
    assert p("tomorrow", relative=False) is None                                # a birth date can't be 'tomorrow'


def test_calendar_only_offers_days_that_can_be_booked():
    rows = ui.calendar_buttons("abc", 2026, 11, TODAY, 330)
    days = {b.text: b.callback_data for r in rows for b in r if b.callback_data and b.callback_data.startswith("w:abc:d:")}
    assert "[17]" in days and "30" in days and "16" not in days                 # today is marked, yesterday is gone
    assert rows[0][0].text == "⠀"                                          # cannot go back before this month
    assert rows[0][2].text == "▶"
    last = TODAY + timedelta(days=330)                                          # the last bookable day
    far = ui.calendar_buttons("abc", last.year, last.month, TODAY, 330)
    shown = {b.text for r in far for b in r if b.callback_data.startswith("w:abc:d:")}
    assert far[0][2].text == "⠀"                                           # cannot go past the last month
    assert str(last.day) in shown and str(last.day + 1) not in shown


def test_all_callback_data_fits_telegram_limit():
    sid = "ab12c"
    keyboards = [ui.airport_buttons(sid, "f"), ui.airport_buttons(sid, "t", "DEL"), ui.calendar_buttons(sid, 2026, 12, TODAY, 330),
                 ui.number_buttons(sid, "n", 6), ui.nav_buttons(sid, keep="Something long and boring"),
                 ui.choice_buttons(sid, "f", ["GOI", "GOX", "ZZZ"])]
    for rows in keyboards:
        for row in rows:
            assert len(row) <= 8
            for b in row:
                assert b.text.strip() and len(b.callback_data.encode()) <= 64, (b.text, b.callback_data)
    assert ui.request_alert_markup(123456789012).inline_keyboard[0][-1].callback_data == "a:ok:123456789012:10"


def test_hold_deadline_is_shown_in_both_local_times():
    assert ui.hold_until_text("2026-11-17T14:37:46Z") == "17 Nov 20:37 BDT / 20:07 IST"
    assert ui.hold_until_text(None) == "" and ui.hold_until_text("garbage") == ""
    assert ui.hold_expired("2000-01-01T00:00:00Z") is True and ui.hold_expired("2999-01-01T00:00:00Z") is False


def test_small_formatters():
    assert ui.money(5132) == "₹5,132" and ui.d_long(date(2026, 12, 9)) == "Wed, 09 Dec 2026"
    assert ui.eta_text(1) == "about 2 min" and ui.eta_text(3) == "about 5 min"
    assert ui.step_header(2).endswith("Step 2 of 5 — Passengers") and ui.credit_word(1) == "1 credit" and ui.pax_word(3) == "3 passengers"
    assert ui.stamp("2026-11-17 14:20:21") == "17 Nov 20:20"                    # UTC -> Bangladesh time
    assert ui.person({"full_name": "A <b>", "username": "x", "telegram_id": 1}) == "A &lt;b&gt; (@x)"
    assert ui.ledger_label("booking x2") == "Booking" and ui.ledger_label("refund: booking failed") == "Refund"


def test_quick_day_buttons_cover_the_next_week_and_open_the_calendar():
    rows = ui.quick_date_buttons("abc", TODAY, 330)
    labels = [b.text for r in rows for b in r]
    assert labels[:3] == ["Today · 17 Nov", "Tomorrow · 18 Nov", "Thu 19 Nov"] and labels[-2] == "Tue 24 Nov"
    assert labels[-1] == "📅 Pick another date" and rows[-1][0].callback_data == "w:abc:cal"
    assert rows[0][0].callback_data == "w:abc:d:2026-11-17"


def test_customer_texts_never_show_coupon_codes_or_airport_jargon():
    codes = ("6ETST001", "PROMO2")
    texts = [ui.HELP_TEXT, ui.welcome_text("A", 3, False), ui.welcome_text("A", 0, False), ui.pending_text(1),
             ui.deactivated_text(), *[ui.offer_name(o) for o in ("offer1", "offer2", "both")],
             *[ui.offer_tag(o) for o in ("offer1", "offer2", "both")],
             ui.bookings_text([{"status": "done", "origin": "DEL", "destination": "BOM", "travel_date": "09-12-2026",
                                "offer": "offer2", "pax_count": 1, "pnr": "X", "pnr_kind": "record_locator"}])]
    labels = [b.text for r in ui.airport_buttons("s", "f") for b in r]
    for t in texts + labels:
        assert not any(c in t for c in codes), t
    assert all(not re.search(r"\b[A-Z]{3}\b", lab) for lab in labels)                      # buttons say "Delhi", not "DEL"
    assert "IATA" not in " ".join(texts) and "airport code" not in " ".join(texts).lower()


def test_main_menu_has_the_four_things_people_need():
    people = [b for row in ui.main_menu(False).keyboard for b in row]
    assert [b if isinstance(b, str) else b.text for b in people] == [ui.MENU_BOOK, ui.MENU_BOOKINGS, ui.MENU_CREDITS, ui.MENU_HELP]
    admin = [b if isinstance(b, str) else b.text for row in ui.main_menu(True).keyboard for b in row]
    assert admin[-1] == ui.MENU_ADMIN
