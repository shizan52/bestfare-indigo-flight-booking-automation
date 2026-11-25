"""Practice mode: the real window + a pretend engine. Every outcome can be tried without touching IndiGo."""
import time
from datetime import timedelta

import pytest
from PyQt6.QtWidgets import QMessageBox

import demo
import forms
from demo import DemoBar, DemoEngine
from main_window import MainWindow


def wait_until(qapp, pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


@pytest.fixture()
def dwin(qapp, store, monkeypatch):
    monkeypatch.setattr(demo, "STEP_SECONDS", {k: 0.03 for k in demo.STEP_SECONDS})
    monkeypatch.setattr(demo, "FARE_SECONDS", 0.05)
    infos = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: infos.append(a[2] if len(a) > 2 else ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    w = MainWindow(store, lambda emit: DemoEngine(store, emit), demo=True)
    w.show()
    w.start()
    w.infos = infos
    yield w
    w.close()


def book(w, offer="offer1"):
    b = w.book
    b.origin.set_code("DEL")
    b.dest.set_code("BOM")
    b.date_field.set_date(forms.today_ist() + timedelta(days=5))
    b.rows[0].first.setText("RAHUL")
    b.rows[0].last.setText("SHARMA")
    b.rows[0].male.setChecked(True)
    b.phone.setText("9876543210")
    b.email.setText("a@b.co")
    b.tiles[offer].setChecked(True)
    b.summary.book.click()


def bar(w) -> DemoBar:
    return w.findChild(DemoBar)


def choose(w, qapp, nth=None):
    """The traveller's side of 'Choose your flight': wait for the list, optionally tap the nth card (by departure), press Continue."""
    wait_until(qapp, lambda: w.book.stack.currentWidget() is w.book.choice)
    ch = w.book.choice
    if nth is not None:
        card = list(ch.cards.values())[nth]
        card.clicked.emit(card.key)
    ch.go.click()
    return ch


def choose_list(w, qapp):
    """Wait for the flight list to show (without choosing)."""
    wait_until(qapp, lambda: w.book.stack.currentWidget() is w.book.choice)
    return w.book.choice


def flights_low(ch):
    import flights
    return flights.lowest_key(ch.options)


def pick(w, outcome):
    bar(w).combo.setCurrentIndex([k for k, _t in demo.OUTCOMES].index(outcome))


def test_practice_mode_is_clearly_marked_and_offers_every_outcome(dwin):
    assert "PRACTICE" in dwin.windowTitle()
    assert [k for k, _t in demo.OUTCOMES] == ["success", "mismatch", "no_seats", "slow_down", "gives_up", "browser"]
    assert bar(dwin) is not None and bar(dwin).combo.count() == 6


def test_success_gives_a_practice_pnr_a_check_and_history(dwin, qapp):
    book(dwin, "offer2")
    choose(dwin, qapp)
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result)
    rec = dwin.book.result.cards[0].rec
    assert rec["pnr"].startswith("DEMO") and len(rec["pnr"]) == 6 and rec["verified"] == "ok" and rec["flight"]
    assert "Booking done — PNR " + rec["pnr"] in dwin.activity.text() and "Details checked with IndiGo" in dwin.activity.text()
    dwin.history.refresh()
    assert dwin.history.table.rowCount() == 1 and dwin.history.table.item(0, dwin.history.C_STATUS).text() == "On hold"


def test_a_booking_indigo_disagrees_with_shows_the_warning(dwin, qapp):
    pick(dwin, "mismatch")
    book(dwin)
    choose(dwin, qapp)
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result)
    texts = " ".join(lb.text() for lb in dwin.book.result.cards[0].findChildren(type(dwin.book.summary.cities)))
    assert "different travel date" in texts and "Checked with IndiGo" not in texts


@pytest.mark.parametrize("outcome,expect", [("no_seats", "no seats"), ("gives_up", "not responding well"), ("browser", "Something went wrong")])
def test_every_failure_can_be_tried(dwin, qapp, outcome, expect):
    pick(dwin, outcome)
    book(dwin)
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result)
    card = dwin.book.result.cards[0]
    assert card.objectName() == "badbox" and expect in card.rec["error"]


def test_a_slow_indigo_is_handled_by_the_app_itself_the_booking_goes_through_and_nobody_is_told_to_wait(dwin, qapp):
    pick(dwin, "slow_down")
    book(dwin)
    choose(dwin, qapp)
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result)
    assert dwin.book.result.cards[0].rec["status"] == "done"                              # no failure, no timer, no second try by the traveller
    text = dwin.activity.text()
    assert "taking a little longer than usual" in text and "minutes" not in text.lower() and not dwin.infos


def test_when_indigo_does_not_answer_the_app_says_so_plainly_and_the_next_press_of_book_works_at_once(dwin, qapp):
    pick(dwin, "gives_up")
    book(dwin)
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result)
    card = dwin.book.result.cards[0]
    assert card.rec["status"] == "failed" and "press Book again" in card.rec["error"]
    assert not any(ch.isdigit() for ch in card.rec["error"])                              # no "wait 15 minutes", no countdown at all
    pick(dwin, "success")
    dwin.book.back_to_form()
    before = len(dwin.store.bookings())
    dwin.book.summary.book.click()                                                         # nothing is locked: the very next press is a booking again
    assert len(dwin.store.bookings()) == before + 1 and not dwin.infos
    choose(dwin, qapp)                                                                     # (and it is carried to the end: no pretend booking may still be running when the window closes)
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result)
    assert dwin.book.result.cards[0].rec["status"] == "done"


def test_stop_ends_a_practice_booking(dwin, qapp, monkeypatch):
    monkeypatch.setattr(demo, "STEP_SECONDS", {k: 1.0 for k in demo.STEP_SECONDS})
    book(dwin)
    wait_until(qapp, lambda: dwin.engine.busy)
    dwin.engine.stop(dwin.group_ids[0])
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result, 8)
    assert "stopped" in dwin.book.result.cards[0].rec["error"]


def test_the_booking_waits_for_the_travellers_choice_of_flight(dwin, qapp):
    book(dwin, "offer2")
    ch = choose_list(dwin, qapp)
    assert dwin.book.stack.currentWidget() is dwin.book.choice and dwin.book.progress.bid           # waiting: no PNR yet
    assert len(ch.cards) == 10 and ch.selected == flights_low(ch)                                    # the lowest fare is ready
    time.sleep(0.3)
    qapp.processEvents()
    assert dwin.book.stack.currentWidget() is dwin.book.choice                                        # it really waits
    assert dwin.store.get(dwin.group_ids[0])["status"] == "running"
    ch.go.click()
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result)


def test_the_flight_that_was_tapped_is_the_flight_that_is_booked(dwin, qapp):
    book(dwin, "offer2")
    ch = choose_list(dwin, qapp)
    cards = list(ch.cards.values())                                                  # earliest first
    pick_card = cards[5]
    low = flights_low(ch)
    assert pick_card.key != low                                                       # not the lowest: the traveller decides
    pick_card.clicked.emit(pick_card.key)
    assert ch.selected == pick_card.key and pick_card.property("sel") is True
    ch.go.click()
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result)
    rec = dwin.book.result.cards[0].rec
    import flights
    want = ch.option(pick_card.key)
    assert rec["flight"] == flights.line(want) and rec["fare"] == want["amount"]
    texts = " ".join(lb.text() for lb in dwin.book.result.cards[0].findChildren(type(dwin.book.summary.cities)))
    assert flights.flight_number(want) in texts and flights.timeline(want) in texts                   # the PNR card names the flight
    dwin.history.refresh()
    assert dwin.history.table.item(0, dwin.history.COLS.index("Flight")).text() == flights.line(want)
    assert any("Your flight: " + flights.line(want) in ln for ln in dwin.activity.text().splitlines())


def test_cancelling_on_the_flight_list_stops_without_holding_anything(dwin, qapp):
    book(dwin)
    ch = choose_list(dwin, qapp)
    ch.cancel.click()
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result)
    rec = dwin.book.result.cards[0].rec
    assert rec["status"] == "failed" and "stopped" in rec["error"] and not rec.get("pnr")


def test_no_list_is_shown_when_there_are_no_seats(dwin, qapp):
    pick(dwin, "no_seats")
    book(dwin)
    wait_until(qapp, lambda: dwin.book.stack.currentWidget() is dwin.book.result)
    assert "Choose your flight" not in dwin.activity.text()


def test_practice_flights_look_like_the_real_list():
    from datetime import date
    import flights
    a = demo.make_flights("DEL", "BOM", date(2026, 12, 1), 2)
    assert a == demo.make_flights("DEL", "BOM", date(2026, 12, 1), 2) and a != demo.make_flights("DEL", "BLR", date(2026, 12, 1), 2)
    assert len(a) == 10 and [o["dep"] for o in a] == sorted(o["dep"] for o in a)
    assert {o["stops"] for o in a} == {0, 1} and all(o["via"] for o in a if o["stops"]) and all(o["flights"] for o in a)
    assert any(o["offer"] and flights.saving(o) > 0 for o in a) and any(not o["offer"] for o in a)
    assert all(o["amount"] > 0 and (o["original"] is None or o["original"] > o["amount"]) for o in a)


def test_the_calendar_shows_practice_fares_when_it_is_opened(dwin, qapp):
    b = dwin.book
    b.origin.set_code("DEL")
    b.dest.set_code("BOM")
    b.date_field.open_calendar()
    cal = b.date_field.calendar
    assert cal.state == "loading"
    wait_until(qapp, lambda: cal.state == "ok")
    assert len(cal.fares) > 40 and cal.cheapest_day() is not None
    assert {c for _p, c in cal.fares.values()} <= {"L", "M", "H"}
    assert any(not c.enabled_day and c.note == "—" for m in cal.cells for c in m if c.d is not None)     # some days have no flight
    cal.close()


def test_practice_fares_are_steady_and_realistic():
    from datetime import date
    a = demo.make_fares("DEL", "BOM", date(2026, 12, 1), 30)
    assert a == demo.make_fares("DEL", "BOM", date(2026, 12, 1), 30)
    assert a != demo.make_fares("DEL", "BLR", date(2026, 12, 1), 30)
    assert all(4000 <= p <= 16000 for p, _c in a.values())


def test_practice_history_is_a_separate_file(home):
    import paths
    assert paths.db_path(True) != paths.db_path(False) and paths.db_path(True).name == "bestfare-demo.db"
