"""A stopped key means a stopped app: when the admin blocks a user the whole window is covered within seconds, nothing under it can be used, going offline does not
undo it, and it opens again by itself when the admin lets the user back in. Driven with the REAL server and the real window."""
import time

import pytest
from PyQt6.QtWidgets import QMessageBox

import cloud
import main_window as mw
from cloud import Offline
from test_licence_ui import lwin, pump  # noqa: F401  (a signed-in window on a live server)
from test_ui import fill


def block(w, reason="shared the key"):
    w.live.cloud.set_status(w.L.k["id"], "blocked", reason, "boss")


def unblock(w):
    w.live.cloud.set_status(w.L.k["id"], "active", "", "boss")


def locked(w):
    return w.veil.isVisible() and not w.pages.isEnabled() and not w.sidebar.isEnabled()


def test_blocking_covers_the_whole_window_with_the_admins_words_and_nothing_under_it_works(lwin, qapp):
    assert not lwin.veil.isVisible() and lwin.pages.isEnabled()
    block(lwin)
    lwin._check_now()                                                         # (what the 20-second check does)
    assert pump(qapp, lambda: lwin.veil.isVisible())
    assert locked(lwin) and "Your access has been stopped" in lwin.veil.title.text() and "shared the key" in lwin.veil.msg.text()
    assert lwin.veil.key.isVisible() and lwin.veil.check.text() == "Check again"
    assert lwin.veil.geometry() == lwin.centralWidget().rect()
    lwin.book.fares_requested.emit("DEL", "BOM", lwin.book.order().day, 60)                                  # the fare calendar is silent too
    assert lwin.fake.fare_requests == []
    lwin.start_booking(lwin.book.order())
    assert lwin.fake.submitted == []


def test_it_opens_again_by_itself_when_the_admin_unblocks(lwin, qapp):
    block(lwin)
    lwin._check_now()
    assert pump(qapp, lambda: locked(lwin))
    unblock(lwin)
    lwin.veil.check.click()                                                   # ("Check again")
    assert pump(qapp, lambda: not lwin.veil.isVisible() and lwin.pages.isEnabled() and lwin.sidebar.isEnabled())
    assert lwin.book.summary.book.isEnabled() and lwin.fake.fare_requests == []
    assert lwin.heartbeat.interval() == mw.HEARTBEAT_S * 1000


def test_the_check_card_says_what_happened_when_the_answer_is_still_no(lwin, qapp):
    block(lwin)
    lwin._check_now()
    assert pump(qapp, lambda: locked(lwin))
    lwin.veil.check.click()
    assert lwin.veil.state.text().startswith("Checking") and not lwin.veil.check.isEnabled()
    assert pump(qapp, lambda: lwin.veil.check.isEnabled() and lwin.veil.state.text().startswith("Still not possible"))


def test_going_offline_does_not_undo_a_block(lwin, qapp, monkeypatch):
    block(lwin)
    lwin._check_now()
    assert pump(qapp, lambda: locked(lwin))
    monkeypatch.setattr(lwin.L.client, "_send", lambda *a, **k: (_ for _ in ()).throw(Offline()))        # the cable is pulled
    lwin.L.refresh()
    assert lwin.L.state == "offline"
    qapp.processEvents()
    assert locked(lwin) and "shared the key" in lwin.veil.msg.text()                                         # still locked, with the admin's words
    for _ in range(3):
        lwin._tick()
    assert locked(lwin)


def test_a_missing_connection_is_said_in_plain_words_and_is_not_a_lock(lwin, qapp, monkeypatch):
    monkeypatch.setattr(lwin.L.client, "_send", lambda *a, **k: (_ for _ in ()).throw(Offline()))
    lwin.L.refresh()
    qapp.processEvents()
    assert lwin.L.state == "offline" and not lwin.veil.isVisible() and "Not connected" in lwin.sidebar.chip_note.text()
    lwin._opened -= 7200
    lwin.L.last_ok -= 7200
    lwin._tick()
    assert not lwin.veil.isVisible() and lwin.pages.isEnabled()                              # (only a 'stopped' answer locks: waiting for the cable never does)


def test_an_ended_licence_has_its_own_words(lwin, qapp):
    lwin.live.cloud.update_licence(lwin.L.k["id"], expires_days=1, actor="boss")
    lwin.live.cloud.now = lambda: time.time() + 3 * 86400                                                       # three days later the licence has run out
    lwin._check_now()
    assert pump(qapp, lambda: locked(lwin))
    assert "licence has ended" in lwin.veil.title.text().lower() and lwin.veil.icon.text() == "⌛"


def test_a_key_that_must_be_typed_again_has_its_own_words(lwin, qapp):
    lwin.live.cloud.reset_device(lwin.L.k["id"], "boss")                                                         # the admin moved the key to another computer
    lwin._check_now()
    assert pump(qapp, lambda: locked(lwin))
    assert "sign in again" in lwin.veil.title.text().lower() and "type your key again" in lwin.veil.msg.text().lower()


def test_a_running_booking_is_stopped_when_the_key_is_stopped(lwin, qapp):
    from test_ui import fill as fill_form
    fill_form(lwin)
    lwin.start_booking(lwin.book.order())
    bid = lwin.fake.submitted[0][1][0]
    lwin.store.update(bid, status="running")
    block(lwin)
    lwin.fake.busy = False
    lwin._check_now()
    assert pump(qapp, lambda: locked(lwin))
    assert bid in lwin.fake.stopped


def test_the_window_does_not_ask_the_server_over_and_over(lwin, qapp, monkeypatch):
    assert mw.HEARTBEAT_S >= 300 and lwin.heartbeat.interval() >= 300 * 1000
    calls = []
    real = lwin.L.client.session
    monkeypatch.setattr(lwin.L.client, "session", lambda: calls.append(1) or real())
    from PyQt6.QtCore import QEvent
    for _ in range(5):
        lwin.eventFilter(lwin, QEvent(QEvent.Type.ApplicationActivate))                 # (coming back to the window is not a reason to ask)
        lwin._tick()
    qapp.processEvents()
    assert calls == []


def test_the_check_happens_once_at_the_moment_of_booking_and_a_blocked_key_never_books(lwin, qapp, monkeypatch):
    fill(lwin)
    calls = []
    real = lwin.L.client.session
    monkeypatch.setattr(lwin.L.client, "session", lambda: calls.append(1) or real())
    lwin.start_booking(lwin.book.order())
    assert len(lwin.fake.submitted) == 1 and len(calls) == 1                                # one question to the server, then the booking starts
    lwin.fake.busy = False
    block(lwin)
    lwin.start_booking(lwin.book.order())
    assert len(lwin.fake.submitted) == 1                                                    # blocked: nothing starts
    assert locked(lwin) and "shared the key" in lwin.veil.msg.text()
    unblock(lwin)
    lwin.veil.check.click()
    assert pump(qapp, lambda: not lwin.veil.isVisible())
    lwin.start_booking(lwin.book.order())
    assert len(lwin.fake.submitted) == 2


def test_at_booking_time_an_unreachable_server_is_told_in_plain_words_and_nothing_starts(lwin, qapp, monkeypatch):
    fill(lwin)
    monkeypatch.setattr(lwin.L.client, "_send", lambda *a, **k: (_ for _ in ()).throw(Offline()))
    lwin.start_booking(lwin.book.order())
    assert lwin.fake.submitted == [] and not lwin.veil.isVisible() and any("internet" in t.lower() or "reach" in t.lower() for t in lwin.info)


def test_the_card_buttons_work(lwin, qapp, monkeypatch):
    block(lwin)
    lwin._check_now()
    assert pump(qapp, lambda: locked(lwin))
    opened = []
    monkeypatch.setattr(mw.QDialog, "exec", lambda self: opened.append(type(self).__name__) or 0)
    lwin.veil.key.click()
    assert opened == ["LoginDialog"]
    lwin.veil.leave.click()
    assert lwin._closing
