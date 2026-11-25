"""The flights told in plain words, and the 'Choose your flight' panel - driven with IndiGo's REAL answer (the trimmed fixture of
the bot's tests: its real field names and values), so what the traveller sees is what IndiGo really sends."""
import json
from pathlib import Path

import pytest

import flights
import indigo_client as ic
from choice_panel import ChoicePanel, FlightCard

REAL = json.loads((Path(__file__).resolve().parents[2] / "indigo-bot" / "tests" / "data" / "search_answer_real.json").read_text(encoding="utf-8"))


def real_options():
    return ic.flight_options(REAL, "DEL", "BOM")


def by_seg(opts, seg):
    return next(o for o in opts if o["seg"] == seg)


# ── the words ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_times_durations_and_stops_in_plain_words():
    opts = real_options()
    first = by_seg(opts, "DEL864BOM")
    assert flights.timeline(first) == "15:30 → 18:00" and flights.duration(first["minutes"]) == "2h 30m"
    assert flights.stops_text(first) == "Non-stop" and flights.flight_number(first) == "6E 864"
    night = by_seg(opts, "DEL6114BOM")
    assert flights.timeline(night) == "23:00 → 01:25 +1" and flights.days_later(night) == 1
    nag = by_seg(opts, "DELNAGBOM")
    assert flights.stops_text(nag) == "1 stop · Nagpur" and flights.flight_number(nag) == "6E 2402 + 6E 6282"
    assert flights.line(first) == "6E 864 · 15:30 → 18:00"


@pytest.mark.parametrize("minutes,text", [(150, "2h 30m"), (120, "2h"), (45, "45m"), (65, "1h 05m"), (None, ""), (0, "")])
def test_duration_words(minutes, text):
    assert flights.duration(minutes) == text


def test_the_saving_is_only_claimed_where_the_offer_really_applies():
    opts = real_options()
    assert flights.saving(by_seg(opts, "DEL864BOM")) == 2060.0
    assert flights.saving(by_seg(opts, "DEL353BOM")) == 0.0                         # its lowest fare is a plain fare


def test_sorting_and_filtering():
    opts = real_options()
    by_time = flights.arranged(opts, "time")
    assert [o["dep"] for o in by_time] == sorted(o["dep"] for o in opts)
    by_price = flights.arranged(opts, "price")
    assert [o["amount"] for o in by_price] == sorted(o["amount"] for o in opts) and by_price[0]["seg"] == "DEL864BOM"
    assert all(o["stops"] == 0 for o in flights.arranged(opts, "time", nonstop_only=True))
    assert flights.lowest_key(opts) == by_price[0]["key"] and flights.lowest_key([]) is None


def test_what_travels_to_the_window_has_no_booking_internals():
    o = real_options()[0]
    assert "fare_key" in o and "fare_key" not in flights.public(o) and flights.public(o)["key"] == o["key"]


# ── the panel ───────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def panel(qapp):
    p = ChoicePanel()
    p.resize(370, 780)
    p.show()
    p.got = []
    p.chosen.connect(lambda bid, key: p.got.append((bid, key)))
    yield p
    p.close()


def shown(p, pax=1, opts=None):
    p.show_flights("B1", opts or real_options(), "Delhi (DEL) → Mumbai (BOM)", "Mon, 30 Nov 2026", pax, 300)
    return p


def texts(card):
    return [lb.text() for lb in card.findChildren(type(card.when))]


def test_every_flight_is_a_card_with_its_details_and_its_lowest_fare(panel):
    shown(panel)
    assert len(panel.cards) == 7 and list(panel.cards) == [o["key"] for o in flights.arranged(panel.options, "price")]
    card = next(c for c in panel.cards.values() if c.o["seg"] == "DEL864BOM")
    t = texts(card)
    assert "15:30 → 18:00" in t and "₹5,438" in t and "2h 30m · Non-stop" in t and "6E 864" in t and "Offer: save ₹2,060" in t
    plain = next(c for c in panel.cards.values() if c.o["seg"] == "DEL353BOM")
    assert "₹7,811" in texts(plain) and "Regular fare" in texts(plain)
    conn = next(c for c in panel.cards.values() if c.o["seg"] == "DELNAGBOM")
    assert "1 stop · Nagpur" in " ".join(texts(conn)) and "6E 2402 + 6E 6282" in texts(conn)
    night = next(c for c in panel.cards.values() if c.o["seg"] == "DEL6114BOM")
    assert "23:00 → 01:25 +1" in texts(night)


def test_the_header_says_what_is_being_chosen(panel):
    shown(panel, pax=2)
    assert panel.route.text() == "Delhi (DEL) → Mumbai (BOM)"
    assert panel.sub.text() == "Mon, 30 Nov 2026  ·  2 travellers  ·  7 flights"


def test_the_lowest_fare_is_marked_and_ready_but_the_traveller_may_pick_another(panel):
    shown(panel)
    low = flights.lowest_key(panel.options)
    pills = [c for c in panel.cards.values() if c.pill is not None]
    assert [c.key for c in pills] == [low] and panel.selected == low and panel.cards[low].property("sel") is True
    assert panel.go.text() == "Continue with 6E 864 · ₹5,438" and "6E 864 · 15:30 → 18:00" in panel.pick.text()
    other = next(c for c in panel.cards.values() if c.o["seg"] == "DEL449BOM")
    other.clicked.emit(other.key)
    assert panel.selected == other.key and other.property("sel") is True and panel.cards[low].property("sel") is False
    assert panel.go.text() == "Continue with 6E 449 · ₹7,811"


def test_nothing_is_booked_until_continue_is_pressed_and_then_only_once(panel):
    shown(panel)
    card = next(c for c in panel.cards.values() if c.o["seg"] == "DEL449BOM")
    card.clicked.emit(card.key)
    assert panel.got == []
    panel.go.click()
    panel.go.click()
    assert panel.got == [("B1", card.key)]


def test_a_double_click_picks_and_continues(panel):
    shown(panel)
    card = next(c for c in panel.cards.values() if c.o["seg"] == "DELAMDBOM")
    card.confirmed.emit(card.key)
    assert panel.got == [("B1", card.key)]


def test_the_list_starts_with_the_lowest_fare_and_can_be_put_in_time_order(panel):
    shown(panel)
    assert panel.sort_btns["price"].isChecked() and not panel.sort_btns["time"].isChecked()
    assert [c.o["amount"] for c in panel.cards.values()] == sorted(o["amount"] for o in panel.options)
    assert next(iter(panel.cards)) == panel.selected                                  # the ready pick is on top
    pick = next(c for c in panel.cards.values() if c.o["seg"] == "DEL449BOM")
    pick.clicked.emit(pick.key)
    panel.sort_btns["time"].click()
    assert list(panel.cards) == [o["key"] for o in flights.arranged(panel.options, "time")]
    assert panel.selected == pick.key and panel.cards[pick.key].property("sel") is True       # the pick survives the re-sort
    panel.sort_btns["price"].click()
    assert [c.o["amount"] for c in panel.cards.values()] == sorted(o["amount"] for o in panel.options)


def test_non_stop_only_hides_connections_and_moves_a_hidden_pick_to_a_shown_flight(panel):
    shown(panel)
    assert panel.nonstop.isVisible()
    conn = next(c for c in panel.cards.values() if c.o["seg"] == "DELNAGBOM")
    conn.clicked.emit(conn.key)
    panel.nonstop.setChecked(True)
    assert len(panel.cards) == 4 and all(c.o["stops"] == 0 for c in panel.cards.values())
    assert panel.selected in panel.cards and panel.go.isEnabled()
    panel.nonstop.setChecked(False)
    assert len(panel.cards) == 7


def test_the_non_stop_switch_is_not_offered_when_it_would_change_nothing(panel):
    only_nonstop = [o for o in real_options() if o["stops"] == 0]
    shown(panel, opts=only_nonstop)
    assert not panel.nonstop.isVisible()


def test_a_group_sees_what_each_person_pays(panel):
    shown(panel, pax=2)
    card = next(c for c in panel.cards.values() if c.o["seg"] == "DEL864BOM")
    assert "₹2,719 each" in texts(card)
    shown(panel, pax=1)
    assert not any("each" in t for c in panel.cards.values() for t in texts(c))


def test_cancel_and_the_clock(panel):
    shown(panel)
    got = []
    panel.cancelled.connect(got.append)
    panel.cancel.click()
    assert got == ["B1"]
    assert panel.timer.text().startswith("⏳ Please choose within 4:5") or panel.timer.text().startswith("⏳ Please choose within 5:00")
    panel.deadline -= 250                                                            # 50 s left: it turns urgent
    panel.tick()
    assert "0:4" in panel.timer.text() and panel.timer.property("urgent") is True


def test_a_new_list_replaces_the_old_one_completely(panel):
    shown(panel)
    n_old = len(panel.cards)
    shown(panel, opts=real_options()[:3])
    assert n_old == 7 and len(panel.cards) == 3 and panel.list_lay.count() == 4          # 3 cards + the stretch
    assert panel.go.isEnabled() is True and panel.selected in panel.cards


def test_flight_cards_have_a_stable_look(qapp):
    c = FlightCard(real_options()[0], True, 1)
    assert c.objectName() == "flightcard" and c.property("sel") is False


def test_nothing_on_the_flight_list_tells_how_the_work_is_done(panel):
    from PyQt6.QtWidgets import QAbstractButton, QLabel
    shown(panel, pax=2)
    words = [w.text() for w in panel.findChildren(QLabel)] + [w.text() for w in panel.findChildren(QAbstractButton)]
    low = " ".join(words).lower()
    for leak in ("token", "search", "api", "cdp", "edge", "browser", "extension", "core.js", "journey", "fare key", "promo", "coupon",
                 "6etst001", "srct", "session", "gateway", "inventory", "sell", "product", "class"):
        assert leak not in low, leak
