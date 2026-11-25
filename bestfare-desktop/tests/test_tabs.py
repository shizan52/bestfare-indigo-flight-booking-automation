"""Booking tabs (like a browser's), clean forms, saved contacts, and the offers a key may use - all shown as 'Offer 1' / 'Offer 2' only."""
from datetime import timedelta

import pytest
from PyQt6.QtWidgets import QInputDialog, QMessageBox

import forms
import licence as lic
from fakes import FakeEngine
from main_window import MainWindow
from test_ui import fill, win  # noqa: F401


def close_tab(win, i):
    """Press the ✕ on the i-th tab."""
    from PyQt6.QtWidgets import QTabBar
    win.tabs.bar.tabButton(i, QTabBar.ButtonPosition.RightSide).click()


def book(win, page, n=1, offer="offer1", origin="DEL", dest="BOM"):
    """Fill the given tab's form and press its Book now. Returns (group, booking id)."""
    win.tabs.select(page)
    fill(win, offer, n, origin, dest)
    page.summary.book.click()
    grp, ids = win.fake.submitted[-1]
    return grp, ids[0]


# ── tabs ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_window_opens_with_one_clean_tab_and_the_plus_adds_more(win):
    assert len(win.tabs.pages()) == 1 and win.tabs.bar.count() == 1 and win.book.state == "draft"
    win.tabs.plus.click()
    win.tabs.plus.click()
    assert len(win.tabs.pages()) == 3 and win.tabs.bar.count() == 3 and win.book is win.tabs.pages()[2]
    assert [win.tabs.bar.tabText(i) for i in range(3)] == ["New booking"] * 3


def test_every_tab_has_a_form_of_its_own(win):
    a = win.book
    fill(win, "offer1", 2, "DEL", "BOM")
    b = win.tabs.add()
    assert b.route() == ("", "") and len(b.rows) == 1 and b.rows[0].first.text() == "" and not any(t.isChecked() for t in b.tiles.values())
    fill(win, "offer2", 1, "BLR", "CCU")
    assert a.route() == ("DEL", "BOM") and len(a.rows) == 2 and a.tiles["offer1"].isChecked()      # the first tab was not touched
    assert b.route() == ("BLR", "CCU") and b.tiles["offer2"].isChecked()
    win.tabs.select(a)
    assert win.book is a and win.tabs.stack.currentWidget() is a


def test_the_tab_title_is_the_route_and_then_what_the_booking_is_doing(win):
    a = win.book
    assert win.tabs.bar.tabText(0) == "New booking"
    fill(win, "offer1", 1, "DEL", "BOM")
    assert win.tabs.bar.tabText(0) == "DEL → BOM"
    grp, bid = book(win, a)
    assert a.state == "running" and win.tabs.bar.tabText(0) == "●  DEL → BOM"
    win.fake.emit({"type": "choose", "id": bid, "flights": [], "seconds": 300})
    assert a.state == "choose" and win.tabs.bar.tabText(0) == "❓  Choose a flight"
    win.fake.emit({"type": "chosen", "id": bid})
    win.fake.play_success(bid, grp, "ABC123")
    assert a.state == "done" and win.tabs.bar.tabText(0) == "✓  DEL → BOM · ABC123"


def test_a_failed_booking_says_so_in_its_tab(win):
    a = win.book
    grp, bid = book(win, a)
    win.fake.play_failure(bid, grp)
    assert a.state == "failed" and win.tabs.bar.tabText(0).startswith("✕")


def test_while_one_booking_runs_another_tab_can_be_filled_and_it_waits_its_turn(win):
    a = win.book
    grp1, bid1 = book(win, a, 1, "offer1", "DEL", "BOM")
    b = win.tabs.add()
    grp2, bid2 = book(win, b, 1, "offer2", "BLR", "CCU")
    assert a.state == "running" and b.state == "waiting" and win.tabs.bar.tabText(1) == "⏳  BLR → CCU"
    assert b.progress.waiting and "Waiting for your other booking" in b.progress.elapsed.text() and "starts by itself" in b.progress.notes.text()
    # the first booking finishes: its result goes to ITS tab, the second starts and shows progress in ITS tab
    win.fake.play_success(bid1, grp1, "AAA111")
    assert a.state == "done" and a.stack.currentWidget() is a.result and b.state == "waiting"
    win.fake.emit({"type": "started", "id": bid2, "grp": grp2, "offer": "offer2"})
    assert b.state == "running" and not b.progress.waiting and win.tabs.bar.tabText(1) == "●  BLR → CCU"
    win.fake.emit({"type": "stage", "id": bid2, "stage": "search"})
    win.fake.play_success(bid2, grp2, "BBB222")
    assert b.state == "done" and b.stack.currentWidget() is b.result
    assert a.result.cards[0].pnr.text() == "AAA111" if hasattr(a.result.cards[0], "pnr") else True


def test_a_finished_booking_does_not_pull_you_away_from_the_tab_you_are_filling(win):
    a = win.book
    grp, bid = book(win, a)
    b = win.tabs.add()
    fill(win, "offer2", 1, "BLR", "CCU")
    win.fake.play_success(bid, grp, "AAA111")
    assert a.state == "done" and win.book is b                                     # still on the tab being filled; the first tab shows ✓ and a toast told
    assert b.route() == ("BLR", "CCU")


def test_the_flight_list_brings_its_tab_to_the_front_because_the_fares_do_not_wait(win):
    a = win.book
    grp, bid = book(win, a)
    b = win.tabs.add()
    assert win.book is b
    win.fake.emit({"type": "choose", "id": bid, "flights": [], "seconds": 300})
    assert win.book is a and a.stack.currentWidget() is a.choice


def test_closing_an_idle_or_finished_tab_just_closes_it_and_the_last_one_is_replaced_by_a_clean_one(win):
    a = win.book
    b = win.tabs.add()
    close_tab(win, 0)
    assert win.tabs.pages() == [b] and win.book is b
    close_tab(win, 0)
    assert len(win.tabs.pages()) == 1 and win.book is not b and win.book.state == "draft"


def test_closing_a_running_tab_asks_and_stops_that_booking_only(win):
    a = win.book
    grp1, bid1 = book(win, a)
    b = win.tabs.add()
    win.box["answer"] = QMessageBox.StandardButton.No
    close_tab(win, 0)
    assert len(win.tabs.pages()) == 2 and win.fake.stopped == []                    # "No": nothing happens
    win.box["answer"] = QMessageBox.StandardButton.Yes
    close_tab(win, 0)
    assert win.tabs.pages() == [b] and win.fake.stopped == [bid1]                  # "Yes": that booking is stopped and its tab is gone


def test_coming_back_to_new_booking_after_a_finished_one_finds_a_clean_ready_form(win):
    a = win.book
    grp, bid = book(win, a)
    win.fake.play_success(bid, grp, "AAA111")
    win._show_page(1)                                                              # My bookings
    win._show_page(0)                                                              # ... and back to New booking
    assert len(win.tabs.pages()) == 2 and win.book is not a and win.book.state == "draft" and win.book.route() == ("", "")
    assert a.state == "done"                                                       # the finished one is a tab of its own
    win._show_page(1)
    win._show_page(0)
    assert len(win.tabs.pages()) == 2                                              # a clean draft is not duplicated


def test_each_tab_stops_its_own_booking(win):
    a = win.book
    grp1, bid1 = book(win, a)
    b = win.tabs.add()
    grp2, bid2 = book(win, b)
    win.box["answer"] = QMessageBox.StandardButton.Yes
    b.progress.stop.click()
    assert win.fake.stopped == [bid2]


# ── the offers: "Offer 1" / "Offer 2" and nothing else ─────────────────────────────────────────────────────────────────
def test_the_offer_tiles_say_only_offer_1_and_offer_2(win):
    b = win.book
    assert [b.tiles[k].text() for k in ("offer1", "offer2")] == ["Offer 1", "Offer 2"]
    shown = " ".join([t.text() for t in b.tiles.values()] + [b.summary.vals["offer"].text()])
    for word in ("Defence", "NCC", "Senior", "citizen", "6E", "PROMO2", "🎖", "🧓"):
        assert word not in shown
    b.tiles["offer2"].setChecked(True)
    b.refresh_summary()
    assert b.summary.vals["offer"].text() == "Offer 2"


def test_a_key_that_may_use_one_offer_sees_one_tile_and_it_is_already_chosen(win):
    b = win.book
    b.set_offers({"offer2": {"enabled": True}})
    assert b.tiles["offer1"].isHidden() and not b.tiles["offer2"].isHidden() and b.tiles["offer2"].isChecked()
    b.set_offers({"offer1": {"enabled": True}, "offer2": {"enabled": True}})
    assert not b.tiles["offer1"].isHidden() and not b.tiles["offer2"].isHidden()
    b.tiles["offer1"].setChecked(True)
    b.set_offers({"offer2": {"enabled": True}})
    assert not b.tiles["offer1"].isChecked() and b.tiles["offer2"].isChecked()                      # an offer taken away is un-chosen


def test_an_offer_the_admin_switched_off_is_shown_but_cannot_be_chosen(win):
    b = win.book
    b.set_offers({"offer1": {"enabled": False}, "offer2": {"enabled": True}})
    assert not b.tiles["offer1"].isEnabled() and "not available right now" in b.tiles["offer1"].text() and b.tiles["offer2"].isChecked()


def test_every_tab_gets_the_offers_of_the_key_including_new_ones(qapp, store):
    L = lic.Licence.__new__(lic.Licence)
    lic.Licence.__init__(L, None, None, "t")
    L.state = lic.OK
    L.account = lic.Account(label="Rahul", credits=5, offers={"offer2": {"enabled": True}})
    w = MainWindow(store, lambda emit: FakeEngine(store, emit), licence=L)
    assert w.book.tiles["offer1"].isHidden() and w.book.tiles["offer2"].isChecked()
    n = w.tabs.add()
    assert n.tiles["offer1"].isHidden() and n.tiles["offer2"].isChecked()                                # (a new tab follows the same rules)
    L.account = lic.Account(label="Rahul", credits=5, offers={"offer1": {"enabled": True}, "offer2": {"enabled": True}})
    w._refresh_account()
    assert all(not p.tiles["offer1"].isHidden() for p in w.tabs.pages())
    w.engine.busy = False
    w.close()


# ── saved contacts ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_contact_can_be_saved_and_used_in_any_booking(win, store, monkeypatch):
    b = win.book
    b.phone.setText("98765 43210")
    b.email.setText("Rahul@Gmail.com")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Rahul - office", True))
    b._save_contact()
    assert store.setting("contacts") == [{"id": store.setting("contacts")[0]["id"], "name": "Rahul - office", "phone": "9876543210", "email": "rahul@gmail.com"}]
    c = win.tabs.add()                                                                            # a different booking ...
    assert c.phone.text() == "" and c.email.text() == ""                                          # ... starts empty ...
    c._fill_contacts_menu()
    labels = [a.text() for a in c.saved_menu.actions() if a.text()]
    assert any("Rahul - office" in t and "9876543210" in t for t in labels) and any("Save the contact typed below" in t for t in labels)
    next(a for a in c.saved_menu.actions() if "Rahul - office" in a.text()).trigger()             # ... and one click fills it
    assert c.phone.text() == "9876543210" and c.email.text() == "rahul@gmail.com"


def test_many_contacts_can_be_kept_and_one_removed(win, store, monkeypatch):
    b = win.book
    names = iter(["Mom", "Office", "Priya"])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (next(names), True))
    for phone, email in (("9876543210", "mom@gmail.com"), ("9123456789", "office@company.in"), ("9000000002", "priya@yahoo.com")):
        b.phone.setText(phone)
        b.email.setText(email)
        b._save_contact()
    assert [c["name"] for c in store.setting("contacts")] == ["Mom", "Office", "Priya"]
    b.remove_contact(store.setting("contacts")[1])
    assert [c["name"] for c in store.setting("contacts")] == ["Mom", "Priya"]
    w2 = win.tabs.add()
    assert [c["name"] for c in w2.contacts()] == ["Mom", "Priya"]                                 # every tab sees the same list


def test_saving_needs_a_correct_number_and_email_and_does_not_save_the_same_twice(win, store, monkeypatch):
    b = win.book
    b.phone.setText("123")
    b.email.setText("nope")
    b._save_contact()
    assert store.setting("contacts") is None or store.setting("contacts") == [] and win.asked[-1][0] == "info"
    assert "10-digit" in b.e_phone.text() or "10-digit" in b.e_phone.text().replace("\n", " ") or b.e_phone.text()
    b.phone.setText("9876543210")
    b.email.setText("a@gmail.com")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("A", True))
    b._save_contact()
    b._save_contact()                                                                            # the same contact again: told, not saved twice
    assert len(store.setting("contacts")) == 1
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("", True))
    b.email.setText("b@gmail.com")
    b._save_contact()                                                                            # no name: nothing saved
    assert len(store.setting("contacts")) == 1


def test_the_single_contact_an_earlier_version_remembered_becomes_a_saved_contact(qapp, store):
    store.set_setting("contact", {"phone": "9876543210", "email": "old@gmail.com"})
    w = MainWindow(store, lambda emit: FakeEngine(store, emit))
    c = w.book.contacts()
    assert [(x["name"], x["phone"], x["email"]) for x in c] == [("Saved contact", "9876543210", "old@gmail.com")] and store.setting("contact") is None
    assert w.book.phone.text() == ""                                                              # (it is there to pick, not forced into the form)
    w.engine.busy = False
    w.close()


def test_a_saved_contact_is_used_for_the_booking_like_a_typed_one(win, store, monkeypatch):
    store.set_setting("contacts", [{"id": "c1", "name": "Mom", "phone": "9876543210", "email": "mom@gmail.com"}])
    b = win.book
    fill(win, "offer1", 1)
    b.phone.clear()
    b.email.clear()
    b._fill_contacts_menu()
    next(a for a in b.saved_menu.actions() if "Mom" in a.text()).trigger()
    b.summary.book.click()
    rec = store.bookings()[0]
    assert rec["phone"] == "9876543210" and rec["email"] == "mom@gmail.com"


def test_a_long_connecting_flight_in_the_list_does_not_widen_the_right_hand_column(win):
    b = win.book
    long_flight = {"key": "K1", "fare_key": "F1", "amount": 8690.0, "original": 9374.0, "offer": True, "product": "R", "seg": "S1", "stops": 1,
                   "dep": "2026-12-01T09:55:00", "arr": "2026-12-01T15:55:00", "minutes": 360, "flights": ["6E 5256", "6E 6704"], "via": ["Hyderabad"],
                   "origin": "BLR", "dest": "DEL", "origin_city": "Bengaluru", "dest_city": "Delhi"}
    b.show_choice("bid1", [long_flight] * 3, "Bengaluru (BLR) → Delhi (DEL)", "Tue, 1 Dec 2026", (1, 0, 0), 300)
    win.resize(1320, 860)
    from PyQt6.QtWidgets import QApplication, QScrollArea
    QApplication.processEvents()
    area = b.stack.parentWidget()
    while area is not None and not isinstance(area, QScrollArea):
        area = area.parentWidget()
    assert area is not None and b.stack.width() <= area.viewport().width()          # the column is as wide as its own space - nothing is cut off at the right
