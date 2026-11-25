"""The window, driven like a person would (offscreen, fake engine): form checks, booking, results, history, closing."""
from datetime import timedelta

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

import forms
from fakes import FakeEngine
from main_window import MainWindow


@pytest.fixture()
def win(qapp, store, monkeypatch):
    box = {}
    asked = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: asked.append(("info", a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(("question", a[1] if len(a) > 1 else "")) or box.get("answer", QMessageBox.StandardButton.Yes))

    def factory(emit):
        box["e"] = FakeEngine(store, emit)
        return box["e"]

    w = MainWindow(store, factory)
    w.resize(1320, 860)                                    # (the designed size: the offscreen "screen" is only 800 x 600, which would open the window small, with a slim menu)
    w.show()
    w.start()
    w.fake = box["e"]
    w.asked = asked
    w.box = box
    yield w
    w.fake.busy = False
    w.close()


def fill(w, offer="offer1", n=1, origin="DEL", dest="BOM"):
    b = w.book
    b.origin.set_code(origin)
    b.dest.set_code(dest)
    b.date_field.set_date(forms.today_ist() + timedelta(days=9))
    while len(b.rows) < n:
        b.add_row()
    for i, r in enumerate(b.rows):
        r.first.setText(f"RAHUL{'' if i == 0 else ' ' + 'ABCDEF'[i]}")
        r.last.setText("SHARMA")
        r.male.setChecked(True)
    b.phone.setText("98765 43210")
    b.email.setText("rahul@gmail.com")
    b.tiles[offer].setChecked(True)


def test_there_is_no_help_page_and_nothing_about_the_browser_in_the_sidebar(win):
    texts = [b.text() for b in win.sidebar.nav]
    assert len(texts) == 3 and not any("Help" in t for t in texts)
    assert [t.split()[-1] for t in texts] == ["booking", "bookings", "Activity"]
    from PyQt6.QtWidgets import QLabel
    everything = " ".join(w.text() for w in win.sidebar.findChildren(QLabel))
    assert "browser" not in everything.lower() and "Fix it" not in everything


def test_the_app_does_not_start_anything_when_it_opens(win):
    assert win.fake.started and win.fake.fare_requests == [] and win.fake.submitted == []      # no browser, no IndiGo call


def test_empty_form_shows_what_is_missing_and_books_nothing(win):
    win.book.summary.book.click()
    assert win.fake.submitted == []
    assert win.book.e_origin.isVisible() and "fly from" in win.book.e_origin.text()
    assert win.book.origin.property("error") is True
    assert win.book.stack.currentWidget() is win.book.summary


def test_a_full_booking_from_form_to_pnr(win, qapp):
    fill(win, "offer2", n=2)
    win.book.summary.book.click()
    assert len(win.fake.submitted) == 1
    grp, ids = win.fake.submitted[0]
    assert len(ids) == 1                                                         # one offer = one booking
    rec = win.store.get(ids[0])
    assert rec["origin"] == "DEL" and rec["phone"] == "9876543210" and rec["pax_count"] == 2 and rec["offer"] == "offer2"
    assert win.book.stack.currentWidget() is win.book.progress
    assert not win.book.form_scroll.widget().isEnabled()                          # the form is locked while booking

    win.fake.emit({"type": "started", "id": ids[0], "grp": grp, "offer": "offer2"})
    win.fake.emit({"type": "stage", "id": ids[0], "stage": "hold"})
    win.fake.emit({"type": "note", "id": ids[0], "text": "Best fare found: ₹5,132 (non-stop)"})
    qapp.processEvents()
    assert win.book.progress.steps.active == "hold"
    assert "₹5,132" in win.book.progress.notes.text()

    win.fake.play_success(ids[0], grp, "GCVUQG")
    qapp.processEvents()
    assert win.book.stack.currentWidget() is win.book.result
    assert [c.rec["pnr"] for c in win.book.result.cards] == ["GCVUQG"]
    assert win.book.form_scroll.widget().isEnabled()
    win.book.result.cards[0].findChild(type(win.book.summary.book), "copy").click()
    assert QApplication.clipboard().text() == "GCVUQG"
    assert win.toast.isVisible() and "GCVUQG" in win.toast.text()
    win.history.refresh()
    assert win.history.table.rowCount() == 1


def test_the_result_says_whether_indigos_record_matches(win, qapp):
    fill(win)
    win.book.summary.book.click()
    grp, ids = win.fake.submitted[0]
    win.fake.play_success(ids[0], grp, "GCVUQG", verified="ok")
    qapp.processEvents()
    text = " ".join(lb.text() for lb in win.book.result.cards[0].findChildren(type(win.book.summary.cities)))
    assert "Checked with IndiGo" in text and "Held for you until" in text and "left" in text
    win.book.back_to_form()
    win.book.summary.book.click()
    grp2, ids2 = win.fake.submitted[1]
    win.fake.play_success(ids2[0], grp2, "ABCDEF", verified="mismatch", verify_note="Please check this before you pay: IndiGo's booking has a different travel date.")
    qapp.processEvents()
    text2 = " ".join(lb.text() for lb in win.book.result.cards[0].findChildren(type(win.book.summary.cities)))
    assert "different travel date" in text2 and "Checked with IndiGo" not in text2


def test_a_failed_booking_explains_why_and_lets_the_user_change_details(win, qapp):
    fill(win)
    win.book.summary.book.click()
    grp, ids = win.fake.submitted[0]
    win.fake.play_failure(ids[0], grp)
    qapp.processEvents()
    assert win.book.stack.currentWidget() is win.book.result
    assert win.book.result.cards[0].objectName() == "badbox"
    win.book.result.back_to_form.emit()
    assert win.book.stack.currentWidget() is win.book.summary
    assert win.book.rows[0].first.text() == "RAHUL"                              # nothing to type again


def test_try_again_keeps_the_offer_and_rebooks(win, qapp):
    fill(win, "offer2")
    win.book.summary.book.click()
    grp, ids = win.fake.submitted[0]
    win.fake.play_failure(ids[0], grp)
    qapp.processEvents()
    win.book._retry(win.book.result.cards[0].rec)
    assert len(win.fake.submitted) == 2
    assert [win.store.get(i)["offer"] for i in win.fake.submitted[1][1]] == ["offer2"]


def test_only_one_offer_can_be_chosen_and_there_is_no_both(win):
    tiles = win.book.tiles
    assert list(tiles) == ["offer1", "offer2"] and forms.OFFER_ORDER == ("offer1", "offer2")
    tiles["offer1"].setChecked(True)
    tiles["offer2"].setChecked(True)
    assert not tiles["offer1"].isChecked() and tiles["offer2"].isChecked()               # choosing one lets go of the other
    texts = " ".join(t.text() for t in tiles.values()) + win.book.e_offer.text()
    assert "Both" not in texts and "both" not in texts
    o = forms.Order(origin="DEL", dest="BOM", day=forms.today_ist() + timedelta(days=3), travellers=[forms.Traveller("A", "B", "M")],
                    phone="9876543210", email="a@b.co", offer="both", eligible=True)
    assert forms.validate(o).get("offer")                                               # 'both' is not an offer any more


def test_there_is_no_waiting_timer_the_book_button_always_starts_a_booking(win):
    """IndiGo's moods are the engine's business (it tries again by itself): the window never asks anybody to wait, never shows a countdown, never refuses Book."""
    assert win.book.summary.warn.isHidden()
    fill(win)
    win.book.summary.book.click()
    assert len(win.fake.submitted) == 1 and win.asked == []
    assert not hasattr(win, "_update_cooldown") and not hasattr(win.fake, "cooldown_left")


def test_a_booking_leaves_nothing_behind_in_the_next_form(win, store):
    fill(win)
    win.book.summary.book.click()
    assert store.setting("contact") is None and store.setting("last_route") is None             # nothing is quietly remembered
    from PyQt6.QtWidgets import QApplication
    nxt = win.tabs.add()
    assert win.book is nxt and nxt.route() == ("", "") and nxt.phone.text() == "" and nxt.email.text() == ""


def test_a_new_window_starts_with_a_clean_form(qapp, store):
    store.set_setting("last_route", {"from": "BLR", "to": "CCU"})                                 # (an older version kept this)
    w = MainWindow(store, lambda emit: FakeEngine(store, emit))
    assert w.book.route() == ("", "") and w.book.phone.text() == "" and len(w.tabs.pages()) == 1
    w.engine.busy = False
    w.close()


def test_book_another_opens_a_new_tab_with_a_clean_form_and_keeps_the_result_where_it_was(win):
    fill(win, "offer2", n=3)
    first = win.book
    win.book.summary.book.click()
    grp, ids = win.fake.submitted[0]
    win.fake.play_success(ids[0], grp, "ABC123")
    assert first.state == "done"
    first.result.another.emit()                                                                  # (what the 'Book another flight' button does)
    second = win.book
    assert second is not first and len(win.tabs.pages()) == 2
    assert len(second.rows) == 1 and second.rows[0].first.text() == ""
    assert not any(t.isChecked() for t in second.tiles.values()) and second.phone.text() == ""
    assert first.stack.currentWidget() is first.result                                          # the finished booking is still there to copy the PNR from


def test_travellers_limit_and_remove(win):
    b = win.book
    for _ in range(10):
        b.add_row()
    assert len(b.rows) == forms.MAX_TRAVELLERS and not b.add_btn.isEnabled()
    b._remove_row(b.rows[2])
    assert len(b.rows) == forms.MAX_TRAVELLERS - 1 and [r.num.text() for r in b.rows] == ["1", "2", "3", "4", "5"]
    while len(b.rows) > 1:
        b._remove_row(b.rows[-1])
    b._remove_row(b.rows[0])                                                        # the last row can never be removed
    assert len(b.rows) == 1


def test_no_popular_airport_buttons_under_from_and_to(win):
    texts = [b.text() for b in win.book.findChildren(type(win.sidebar.nav[0]))]
    assert not any(t in ("Delhi", "Mumbai", "Bengaluru", "Kolkata", "Chennai", "Hyderabad") for t in texts)
    assert not any("Popular" in lb.text() for lb in win.book.findChildren(type(win.book.summary.cities)))


def test_swap_exchanges_the_airports(win):
    win.book.origin.set_code("DEL")
    win.book.dest.set_code("BOM")
    win.book._swap()
    assert win.book.route() == ("BOM", "DEL")


def test_closing_during_a_booking_asks_first(win):
    win.fake.busy = True
    win.box["answer"] = QMessageBox.StandardButton.No
    win.close()
    assert win.isVisible() and win.fake.shut is None
    win.box["answer"] = QMessageBox.StandardButton.Yes
    win.close()
    assert win.fake.shut is True


def test_activity_page_tells_the_story_in_plain_words_only(win, qapp):
    fill(win)
    win.book.summary.book.click()
    grp, ids = win.fake.submitted[0]
    win.fake.emit({"type": "log", "level": "info", "text": "Started a session with IndiGo — OK (312 ms)", "t": 1_700_000_000})   # technical: hidden
    win.fake.emit({"type": "note", "id": ids[0], "text": "Offer applied — you save ₹1,902"})
    win.fake.play_success(ids[0], grp, "GCVUQG")
    qapp.processEvents()
    text = win.activity.text()
    assert "Booking started — Offer 1" in text and "Applying your offer" in text and "Confirming your booking" in text
    assert "Offer applied — you save ₹1,902" in text and "Booking done — PNR GCVUQG" in text
    assert "312 ms" not in text and "session" not in text.lower()                       # the technical line never shows
    low = text.lower()
    for leak in ("token", "search", "api", "cdp", "edge", "browser", "extension", "core.js", "hdfc", "gateway", "payment page",
                 "fetch", "cookie", "akamai", "429", "6etst001", "srct", "debug", "proxy", "automation"):
        assert leak not in low, leak


def test_activity_page_has_no_door_to_the_technical_log_in_normal_mode(win):
    assert not any("log folder" in b.text().lower() for b in win.activity.findChildren(type(win.sidebar.nav[0])))


def test_debug_mode_adds_the_technical_story_and_the_log_folder(qapp, store):
    box = {}

    def factory(emit):
        box["e"] = FakeEngine(store, emit)
        return box["e"]

    w = MainWindow(store, factory, debug=True)
    w.start()
    box["e"].emit({"type": "log", "level": "info", "text": "Started a session with IndiGo — OK (312 ms)", "t": 1_700_000_000})
    qapp.processEvents()
    assert "312 ms" in w.activity.text()
    assert any("log folder" in b.text().lower() for b in w.activity.findChildren(type(w.sidebar.nav[0])))
    box["e"].busy = False
    w.close()


def test_the_details_links_open_the_activity_page(win, qapp):
    fill(win)
    win.book.summary.book.click()
    grp, ids = win.fake.submitted[0]
    det = [b for b in win.book.progress.findChildren(type(win.book.summary.book)) if "every step" in b.text()][0]
    det.click()
    assert win.pages.currentWidget() is win.activity and win.sidebar.nav[2].isChecked()
    win._show_page(0)
    win.fake.play_failure(ids[0], grp, "IndiGo has no seats on this route and date. Please try another date.")
    qapp.processEvents()
    why = [b for b in win.book.result.cards[0].findChildren(type(win.book.summary.book)) if "What happened" in b.text()][0]
    why.click()
    assert win.pages.currentWidget() is win.activity
    assert "Booking not completed — IndiGo has no seats" in win.activity.text()


def test_finishing_a_booking_does_not_pull_the_user_away_from_the_activity_page(win, qapp):
    fill(win)
    win.book.summary.book.click()
    grp, ids = win.fake.submitted[0]
    win._show_page(2)
    win.fake.play_success(ids[0], grp, "GCVUQG")
    qapp.processEvents()
    assert win.pages.currentWidget() is win.activity
    assert win.book.stack.currentWidget() is win.book.result


def test_activity_page_helpers(win):
    a = win.activity
    a.add("warn", "<b>not html</b> & 5 < 6")
    assert "<b>not html</b> & 5 < 6" in a.text()
    a.clear()
    assert a.text() == ""
    for i in range(a.MAX_LINES + 50):
        a.add("info", f"line {i}")
    assert a.lines <= a.MAX_LINES + 1


def test_the_activity_story_is_also_the_log_file_story(win, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="bestfare.activity"):
        win._say("step", "Finding the best fare")
    assert "Finding the best fare" in caplog.text


# ── the fare calendar inside the form ────────────────────────────────────────────────────────────────
def day(n):
    return forms.today_ist() + timedelta(days=n)


def test_opening_the_calendar_asks_for_fares_only_when_the_route_is_known(win):
    win.book.date_field.open_calendar()
    assert win.fake.fare_requests == []                                                # no route yet: nothing to ask
    win.book.date_field.calendar.close()
    win.book.origin.set_code("DEL")
    win.book.dest.set_code("BOM")
    win.book.date_field.open_calendar()
    assert len(win.fake.fare_requests) == 1 and win.fake.fare_requests[0][:2] == ("DEL", "BOM")
    assert win.book.date_field.calendar.state == "loading"
    win.book.date_field.calendar.close()


def test_fares_arrive_in_the_calendar_and_the_summary(win, qapp):
    b = win.book
    b.origin.set_code("DEL")
    b.dest.set_code("BOM")
    b.date_field.set_date(day(3))
    b.date_field.open_calendar()
    start = win.fake.fare_requests[0][2]
    fares = {day(i).isoformat(): [6000.0 + i * 10, "L" if i % 2 else "H"] for i in range(1, 30) if i != 5}
    win.fake.play_fares("DEL", "BOM", start, 60, fares)
    qapp.processEvents()
    cal = b.date_field.calendar
    assert cal.state == "ok" and len(cal.fares) == len(fares)
    assert "from ₹6,030 that day" in win.book.summary.vals["fare"].text()
    cal.close()


def test_fares_for_another_route_are_ignored(win, qapp):
    b = win.book
    b.origin.set_code("DEL")
    b.dest.set_code("BOM")
    b.date_field.open_calendar()
    win.fake.play_fares("BLR", "CCU", win.fake.fare_requests[0][2], 60, {day(2).isoformat(): [4000.0, "L"]})
    qapp.processEvents()
    assert b.date_field.calendar.fares == {} and b.date_field.calendar.state == "loading"
    b.date_field.calendar.close()


def test_changing_the_route_clears_the_fares(win, qapp):
    b = win.book
    b.origin.set_code("DEL")
    b.dest.set_code("BOM")
    b.date_field.open_calendar()
    win.fake.play_fares("DEL", "BOM", win.fake.fare_requests[0][2], 60, {day(2).isoformat(): [4000.0, "L"]})
    qapp.processEvents()
    assert b.date_field.calendar.fares
    b.dest.set_code("GOI")
    assert b.date_field.calendar.fares == {} and b.date_field.calendar.state == "idle"
    b.date_field.calendar.close()


def test_the_form_moves_on_by_itself_after_each_pick(win, qapp):
    b = win.book
    seen = []
    b.origin.chosen.connect(lambda c: seen.append(("from", c)))
    b.dest.chosen.connect(lambda c: seen.append(("to", c)))
    idx = b.origin._model                                                          # noqa: SLF001
    b.origin._edited("del")
    b.origin._picked(idx.index(0, 0))
    b.dest._edited("mum")
    b.dest._picked(b.dest._model.index(0, 0))                                      # noqa: SLF001
    assert seen == [("from", "DEL"), ("to", "BOM")] and b.route() == ("DEL", "BOM")
    qapp.processEvents()
    b.date_field.calendar.close()


# ── speed and slow computers ─────────────────────────────────────────────────────────────────────────────────────────
def test_typing_tells_the_engine_the_form_is_being_filled_but_only_every_ten_seconds(win):
    """The browser (opened for the fare calendar) is kept - and kept warm - while the traveller types, so Book now need not wait."""
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    assert win.fake.kept_open == []
    for ch in "98765":
        QApplication.sendEvent(win.book.phone, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_5, Qt.KeyboardModifier.NoModifier, ch))
    assert win.fake.kept_open == [90]                                           # five key presses: one message
    win._last_active -= 11                                                      # ten seconds later
    QApplication.sendEvent(win.book.email, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a"))
    assert win.fake.kept_open == [90, 90]


def test_a_booking_that_takes_long_says_so_calmly(win):
    fill(win)
    win.book.summary.book.click()
    p = win.book.progress
    p.t0 -= 60
    p.tick()
    assert "usually 1 to 2 minutes" in p.elapsed.text() and "longer than usual" not in p.elapsed.text()
    p.t0 -= 120                                                                 # three minutes in
    p.tick()
    text = p.elapsed.text()
    assert "taking a little longer than usual" in text and "nothing is charged" in text
    assert not any(w in text.lower() for w in ("browser", "token", "slow down", "blocked", "429"))


def test_sorting_the_flight_list_moves_the_cards_it_does_not_rebuild_them(win):
    import json
    from pathlib import Path

    import flights
    import indigo_client as ic
    real = json.loads((Path(__file__).resolve().parents[2] / "indigo-bot" / "tests" / "data" / "search_answer_real.json").read_text(encoding="utf-8"))
    opts = [flights.public(o) for o in ic.flight_options(real, "DEL", "BOM")]
    ch = win.book.choice
    ch.show_flights("B", opts, "Delhi (DEL) → Mumbai (BOM)", "Mon, 30 Nov 2026", 1, 300)
    before = {k: id(c) for k, c in ch.cards.items()}
    ch.sort_btns["time"].click()
    ch.nonstop.setChecked(True)
    ch.nonstop.setChecked(False)
    ch.sort_btns["price"].click()
    assert {k: id(c) for k, c in ch.cards.items()} == before                    # the very same card objects, in their new order
    assert len(ch.cards) == 7
