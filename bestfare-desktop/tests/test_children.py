"""Children and infants in a booking: the rules (ages count on the day of the flight), the form (kind selector, date of birth, limits),
what is kept, what the engine is given, and what the credits cost."""
from datetime import date, timedelta

import pytest

import engine as eng
import forms
import gate as gatemod
from forms import ADULT, CHILD, INFANT, Order, Traveller
from indigo_client import BookingResult
from test_gate_engine import credits, signed_in, wait_for  # noqa: F401
from test_engine import Events, FakeEdge, finished, make  # noqa: F401
from test_ui import fill, win  # noqa: F401

DAY = forms.today_ist() + timedelta(days=20)


def born(years=0, months=0, days=0, on: date = DAY) -> str:
    """A date of birth (DD/MM/YYYY) that makes someone exactly this old on `on` (+ a few days so it is not on a birthday edge)."""
    y, m = on.year - years, on.month - months
    while m < 1:
        y, m = y - 1, m + 12
    d = date(y, m, min(on.day, 28)) - timedelta(days=days)
    return d.strftime("%d/%m/%Y")


def family(child_dob=None, infant_dob=None) -> Order:
    o = Order(origin="DEL", dest="BOM", day=DAY, phone="9876543210", email="a@b.co", offer="offer1", eligible=True,
              travellers=[Traveller("RAHUL", "SHARMA", "M"), Traveller("PRIYA", "SHARMA", "F"),
                          Traveller("ARYA", "SHARMA", "F", CHILD, child_dob or born(5)),
                          Traveller("VIHAAN", "SHARMA", "M", INFANT, infant_dob or born(0, 8))])
    return o


# ── the rules ────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_family_with_a_child_and_a_baby_is_a_good_order():
    o = family()
    assert forms.validate(o) == {}
    assert o.counts() == (2, 1, 1) and o.seats == 3
    assert o.travellers[2].dob.count("-") == 2 and o.travellers[3].dob.count("-") == 2          # kept as DD-MM-YYYY from here on
    assert forms.mix_text(*o.counts()) == "2 adults, 1 child, 1 infant"
    assert forms.mix_text(3, 0, 0) == "3 travellers" and forms.mix_text(1, 0, 0) == "1 traveller"


def test_a_child_needs_a_date_of_birth_and_a_baby_too():
    for kind, who in ((CHILD, "child"), (INFANT, "baby")):
        o = family()
        t = [x for x in o.travellers if x.kind == kind][0]
        t.dob = ""
        e = forms.validate(o)
        assert list(e) == [f"t{o.travellers.index(t)}.dob"] and who in e[f"t{o.travellers.index(t)}.dob"] and "date of birth" in e[f"t{o.travellers.index(t)}.dob"]
        t.dob = "31/02/2019"
        assert "like 25/03/2019" in forms.validate(o)[f"t{o.travellers.index(t)}.dob"]


@pytest.mark.parametrize("years,ok", [(2, True), (5, True), (11, True), (12, False), (1, False), (30, False)])
def test_a_child_is_two_to_eleven_on_the_day_of_the_flight(years, ok):
    o = family(child_dob=born(years))
    e = forms.validate(o)
    assert ("t2.dob" not in e) is ok
    if not ok:
        assert ("Adult" in e["t2.dob"]) if years >= 12 else ("Infant" in e["t2.dob"])           # it says what to change the traveller to


@pytest.mark.parametrize("months,ok", [(8, True), (23, True), (24, False), (36, False)])
def test_a_baby_is_under_two_on_the_day_of_the_flight(months, ok):
    o = family(infant_dob=born(0, months))
    e = forms.validate(o)
    assert ("t3.dob" not in e) is ok, e
    if not ok:
        assert "Child" in e["t3.dob"]


def test_the_age_is_counted_on_the_travel_day_not_today():
    turns_12_the_day_after = date(DAY.year - 12, DAY.month, min(DAY.day, 28)) + timedelta(days=1)      # 11 on the flight
    turns_12_on_the_day = date(DAY.year - 12, DAY.month, min(DAY.day, 28))                              # 12 on the flight
    assert forms.validate(family(child_dob=turns_12_the_day_after.strftime("%d/%m/%Y"))) == {}
    assert "t2.dob" in forms.validate(family(child_dob=turns_12_on_the_day.strftime("%d/%m/%Y")))


def test_a_baby_must_be_at_least_three_days_old_and_not_born_in_the_future():
    today = forms.today_ist()
    soon = family(infant_dob=today.strftime("%d/%m/%Y"))
    soon.day = today + timedelta(days=2)                                                                 # born today, flying in 2 days: 2 days old
    assert "too young" in forms.validate(soon)["t3.dob"]
    ok = family(infant_dob=(today - timedelta(days=1)).strftime("%d/%m/%Y"))
    ok.day = today + timedelta(days=2)                                                                   # 3 days old: allowed
    assert "t3.dob" not in forms.validate(ok)
    assert "not come" in forms.validate(family(infant_dob=(today + timedelta(days=3)).strftime("%d/%m/%Y")))["t3.dob"]


def test_nobody_travels_without_a_grown_up_and_a_baby_needs_its_own_adult_lap():
    only_kids = family()
    only_kids.travellers = [t for t in only_kids.travellers if t.kind != ADULT]
    assert "adult" in forms.validate(only_kids)["travellers"]
    two_babies = family()
    two_babies.travellers = [two_babies.travellers[0], two_babies.travellers[3], Traveller("MIRA", "SHARMA", "F", INFANT, born(0, 5))]
    assert "lap" in forms.validate(two_babies)["travellers"]
    two_adults_two_babies = family()
    two_adults_two_babies.travellers.append(Traveller("MIRA", "SHARMA", "F", INFANT, born(0, 5)))
    two_adults_two_babies.travellers.pop(2)                                                              # (2 adults, 2 infants: one on each lap)
    assert forms.validate(two_adults_two_babies) == {}


def test_at_most_six_people_take_a_seat_but_babies_do_not_count_against_that():
    o = Order(origin="DEL", dest="BOM", day=DAY, phone="9876543210", email="a@b.co", offer="offer1", eligible=True,
              travellers=[Traveller(f"A{chr(65 + i)}", "SHARMA", "M") for i in range(6)] + [Traveller("BABY", "SHARMA", "F", INFANT, born(0, 6))])
    assert forms.validate(o) == {}
    o.travellers.append(Traveller("EXTRA", "SHARMA", "M"))
    assert "At most 6" in forms.validate(o)["travellers"]


def test_a_childs_name_and_gender_are_checked_like_anybody_elses():
    o = family()
    o.travellers[2].first, o.travellers[2].gender = "", ""
    e = forms.validate(o)
    assert {"t2.first", "t2.gender"} <= set(e)


def test_older_bookings_without_a_kind_are_all_adults():
    assert forms.record_mix([{"first": "A", "last": "B", "gender": "M"}, {"first": "C", "last": "D", "gender": "F"}]) == (2, 0, 0)
    assert forms.record_mix([{"kind": "adult"}, {"kind": "child"}, {"kind": "infant"}, {"kind": "child"}]) == (1, 2, 1)


def test_the_date_of_birth_is_understood_in_the_ways_people_type_it():
    assert forms.parse_dob("25/03/2019") == forms.parse_dob("25-03-2019") == forms.parse_dob("25.03.2019") == forms.parse_dob("25032019") == date(2019, 3, 25)
    assert forms.parse_dob("5/3/2019") == date(2019, 3, 5)
    for bad in ("", "31/02/2019", "2019/03/25", "25/13/2019", "abc", "25/03/19"):
        assert forms.parse_dob(bad) is None
    assert forms.age_words(date(2019, 3, 25), date(2026, 12, 10)) == "7 years"
    assert forms.age_words(date(2025, 6, 1), date(2026, 12, 10)) == "1 year 6 months"
    assert forms.age_words(date(2026, 6, 10), date(2026, 12, 10)) == "6 months" and forms.age_words(date(2026, 12, 1), date(2026, 12, 10)) == "9 days"


def test_what_the_engine_is_given_carries_every_kind_and_the_dates_of_birth():
    o = family()
    assert forms.validate(o) == {}
    req = forms.to_request(o, "offer1")
    assert [(p.kind, p.dob) for p in req.passengers][:2] == [("adult", ""), ("adult", "")]
    assert req.passengers[2].kind == "child" and len(req.passengers[2].dob) == 10 and req.passengers[3].kind == "infant"
    assert [len(req.adults), len(req.children), len(req.infants), len(req.seats)] == [2, 1, 1, 3]
    rec = o.records()
    assert rec[2]["kind"] == "child" and rec[3]["kind"] == "infant" and rec[0]["dob"] == ""
    again = eng.request_from({"origin": "DEL", "dest": "BOM", "travel_date": DAY.isoformat(), "offer": "offer1", "email": "a@b.co", "phone": "9876543210",
                              "travellers": rec})
    assert [(p.kind, p.dob) for p in again.passengers] == [(p.kind, p.dob) for p in req.passengers]


# ── the form ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_add_buttons_make_adult_child_and_infant_rows_and_respect_the_limits(win):
    b = win.book
    assert [r.kind for r in b.rows] == [ADULT] and b.add_infant.isEnabled()
    b.add_child.click()
    b.add_infant.click()
    assert [r.kind for r in b.rows] == [ADULT, CHILD, INFANT]
    assert not b.add_infant.isEnabled()                                          # one infant per adult: no second lap
    assert b.rows[0].dob_row.isHidden() and not b.rows[1].dob_row.isHidden() and not b.rows[2].dob_row.isHidden()
    assert "2 of 6 seats" in b.count.text() and "1 infant" in b.count.text()      # (the infant takes no seat)
    for _ in range(8):
        b.add_btn.click()
    assert sum(r.kind != INFANT for r in b.rows) == forms.MAX_TRAVELLERS and not b.add_btn.isEnabled() and not b.add_child.isEnabled()
    b.add_row(kind=INFANT)                                                       # (an infant may still come while adults outnumber infants)
    assert sum(r.kind == INFANT for r in b.rows) == 2


def test_the_date_of_birth_types_itself_into_shape_and_shows_the_age_on_the_travel_day(win):
    b = win.book
    b.date_field.set_date(DAY)
    b.add_child.click()
    r = b.rows[1]
    r.dob.textEdited.emit("25032019")                                           # what typing eight digits does
    assert r.dob.text() == "25/03/2019"
    assert "years old on the travel date" in r.age.text() and r.age.text().startswith("= ")
    b.date_field.set_date(DAY + timedelta(days=60))
    assert "years old" in r.age.text()                                           # the hint follows the travel date
    r.dob.textEdited.emit("2503")
    assert r.dob.text() == "25/03"


def test_changing_a_traveller_from_child_to_adult_hides_the_date_and_keeps_the_name(win):
    b = win.book
    b.add_child.click()
    r = b.rows[1]
    r.first.setText("ARYA")
    r.kind_btns[ADULT].click()
    assert r.kind == ADULT and r.dob_row.isHidden() and r.first.text() == "ARYA"
    assert r.value().dob == "" and r.value().kind == ADULT                                                    # an adult carries no date of birth


def test_the_page_names_every_problem_of_a_child_and_a_baby_on_their_own_rows(win):
    b = win.book
    fill(win, n=1)
    b.add_child.click()
    b.add_infant.click()
    for r in b.rows[1:]:
        r.first.setText("KID")
        r.last.setText("SHARMA")
        r.female.setChecked(True)
    b.rows[1].dob.setText(born(14).replace("/", ""))                              # 14 years: not a child
    b.rows[1].dob.textEdited.emit(b.rows[1].dob.text())
    win.book._book_clicked()
    assert win.store.bookings() == []                                              # nothing was booked
    assert "Adult" in b.rows[1].err.text()
    assert "date of birth" in b.rows[2].err.text()
    assert b.rows[1].dob.property("error") is True and b.rows[2].dob.property("error") is True


def test_a_good_family_books_and_the_summary_and_history_say_who_is_coming(win, qapp):
    b = win.book
    fill(win, n=2)
    b.add_child.click()
    b.add_infant.click()
    for r, dob in ((b.rows[2], born(6, on=b.date_field.date())), (b.rows[3], born(0, 7, on=b.date_field.date()))):
        r.first.setText("KID" if r.kind == CHILD else "BABY")
        r.last.setText("SHARMA")
        r.female.setChecked(True)
        r.dob.textEdited.emit(dob.replace("/", ""))
    assert win.book.summary.vals["pax"].text() == "2 adults, 1 child, 1 infant"
    win.book._book_clicked()
    rec = win.store.bookings()[0]
    assert [t["kind"] for t in rec["travellers"]] == [ADULT, ADULT, CHILD, INFANT]
    assert rec["travellers"][2]["dob"].count("-") == 2 and rec["travellers"][0]["dob"] == ""
    import main_window as mw
    assert mw.pax_cell(rec) == "3 + 1 infant"


def test_new_booking_starts_with_one_adult_again(win):
    b = win.book
    b.add_child.click()
    b.add_infant.click()
    b.new_booking()
    assert [r.kind for r in b.rows] == [ADULT]


# ── the credits ──────────────────────────────────────────────────────────────────────────────────────────────
def test_the_credit_line_does_not_count_the_infant_unless_the_admin_says_otherwise(win, signed_in, live):
    win.licence = signed_in
    win.book.credit_text = win._credit_text
    text, bad = win._credit_text(3, 1)
    assert "3 credits" in text and not bad and signed_in.account.infant_credits == 0         # 3 seats + 1 infant on a lap: 3 credits, 5 in hand
    live.cloud.set_setting("infant_credits", 1, "boss")
    signed_in.refresh()
    text, bad = win._credit_text(3, 1)
    assert "4 credits" in text and signed_in.account.infant_credits == 1
    signed_in.refresh()
    text, bad = win._credit_text(5, 1)
    assert bad and "6 credits" in text                                           # 5 in hand: not enough


def test_a_family_booking_charges_adults_and_children_only_and_gives_the_engine_everyone(live, signed_in, make, store):
    seen = {}

    async def runner(bid, req, promo):
        seen["kinds"] = [(p.kind, bool(p.dob)) for p in req.passengers]
        seen["credits_while_running"] = credits(live, signed_in.k)
        return BookingResult(success=True, pnr="FAM123", kind="record_locator")

    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    o = family()
    assert forms.validate(o) == {}
    _g, ids = e.submit(o)
    r = finished(ev, ids[0])
    assert r["status"] == "done"
    assert seen["kinds"] == [("adult", False), ("adult", False), ("child", True), ("infant", True)]
    assert seen["credits_while_running"] == 2                                      # 5 - (2 adults + 1 child); the infant costs nothing
    b = live.cloud.licence_detail(signed_in.k["id"])["bookings_list"][0]
    assert b["travellers"] == 3 and b["infants"] == 1


def test_when_infants_are_free_only_the_seats_are_charged_and_a_refund_gives_back_what_was_charged(live, signed_in, make, store):
    live.cloud.set_setting("infant_credits", 0, "boss")

    async def runner(bid, req, promo):
        return BookingResult(success=False, error="no flights available for this route/date")

    signed_in.refresh()
    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    o = family()
    forms.validate(o)
    _g, ids = e.submit(o)
    r = finished(ev, ids[0])
    assert r["status"] == "failed"
    assert wait_for(lambda: credits(live, signed_in.k) == 5)                       # 3 charged, 3 returned
    ledger = live.cloud.licence_detail(signed_in.k["id"])["ledger"]
    assert any(x["delta"] == -3 and x["kind"] == "booking" for x in ledger) and any(x["delta"] == 3 and x["kind"] == "refund" for x in ledger)


def test_without_enough_credits_for_the_whole_family_nothing_starts(live, signed_in, make, store):
    live.cloud.adjust_credits(signed_in.k["id"], -3, "test", "boss")                # 2 left, the family needs 3 (the infant is free)
    ran = []

    async def runner(bid, req, promo):
        ran.append(1)
        return BookingResult(success=True, pnr="X")

    signed_in.refresh()
    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    o = family()
    forms.validate(o)
    _g, ids = e.submit(o)
    r = finished(ev, ids[0])
    assert r["status"] == "failed" and "3 credits" in r["error"] and not ran and credits(live, signed_in.k) == 2


def test_at_most_four_children_and_four_infants_like_indigos_own_picker(win):
    o = Order(origin="DEL", dest="BOM", day=DAY, phone="9876543210", email="a@b.co", offer="offer1", eligible=True,
              travellers=[Traveller("ADULT", "SHARMA", "M")] + [Traveller(f"KID{chr(65 + i)}", "SHARMA", "F", CHILD, born(5)) for i in range(5)])
    assert "at most 4 children" in forms.validate(o)["travellers"]
    b = win.book
    for _ in range(6):
        b.add_child.click()
    assert sum(r.kind == CHILD for r in b.rows) == forms.MAX_CHILDREN and not b.add_child.isEnabled()


# ── what the traveller is told, and what IndiGo's record must agree with ─────────────────────────────────────────
def test_the_infant_fee_is_explained_when_it_turns_up():
    import logging

    class FakeEngine:
        current, expect_choice = "bid1", True
        _slow_noted: set = set()

        def __init__(self):
            self.notes = []

        def _note(self, bid, text):
            self.notes.append(text)

        def _stage(self, bid, stage): ...

    fe = FakeEngine()
    handler = eng._Progress(fe)
    rec = logging.LogRecord("indigo_client", logging.INFO, __file__, 1, "infant fee Rs%s total Rs%s", (2250.0, 12514.0), None)
    handler.emit(rec)
    assert fe.notes == ["The infant fee ₹2,250 is added to the fare you chose — the total is ₹12,514."]


def test_the_flight_list_says_the_infant_fee_comes_later(win):
    b = win.book
    opts = [{"key": "K1", "fare_key": "F1", "amount": 10264.0, "original": 14068.0, "offer": True, "product": "R", "seg": "S1", "stops": 0,
             "dep": "2026-12-01T03:30:00", "arr": "2026-12-01T05:55:00", "minutes": 145, "flights": ["6E 353"], "via": [], "origin": "DEL", "dest": "BOM",
             "origin_city": "Delhi", "dest_city": "Mumbai"}]
    b.show_choice("bid1", opts, "Delhi → Mumbai", "Tue, 1 Dec", (1, 1, 1), 300)
    assert not b.choice.infant_note.isHidden() and "infant" in b.choice.infant_note.text().lower()
    assert "1 adult, 1 child, 1 infant" in b.choice.sub.text()
    assert b.choice.cards["K1"].each is None                                        # (children and a baby pay other fares: no "each")
    b.show_choice("bid2", opts, "Delhi → Mumbai", "Tue, 1 Dec", (2, 0, 0), 300)
    assert b.choice.infant_note.isHidden() and b.choice.cards["K1"].each is not None
