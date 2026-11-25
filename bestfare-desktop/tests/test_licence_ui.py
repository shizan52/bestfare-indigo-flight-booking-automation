"""What the traveller sees: the sign-in window, the credits in the sidebar, the cost of a booking, a stopped account - driven against a
REAL server (offscreen window)."""
import time

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

import licence as lic
import shell_ui
from cloud import CloudClient
from device import DeviceKey, Vault
from fakes import FakeEngine
from main_window import MainWindow
from test_ui import fill


def pump(qapp, cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def licensed(live, tmp_path, credits=5, name="Rahul Sharma"):
    k = live.cloud.create_licence(name, credits=credits, actor="boss")
    vault = Vault(tmp_path / "v.bin")
    L = lic.Licence(CloudClient(DeviceKey(vault), 1, base_url=live.url, server_public=live.public), vault, "test")
    L.activate(k["key"])
    L.k = k
    return L


@pytest.fixture()
def lwin(qapp, store, live, tmp_path, monkeypatch):
    box = {"info": []}
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: box["info"].append(a[2] if len(a) > 2 else ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    L = licensed(live, tmp_path)

    def factory(emit):
        box["e"] = FakeEngine(store, emit)
        return box["e"]

    w = MainWindow(store, factory, licence=L)
    w.show()
    w.start()
    w.fake, w.L, w.info, w.live = box["e"], L, box["info"], live
    yield w
    w.fake.busy = False
    w.close()


def test_the_sidebar_shows_who_is_signed_in_and_how_many_credits_are_left(lwin, qapp):
    s = lwin.sidebar
    assert s.chip.isVisible() and "Rahul Sharma" in s.chip_name.text() and s.chip_credits.text() == "5 credits"
    lwin.live.cloud.adjust_credits(lwin.L.k["id"], -4, "test", "boss")
    lwin.L.refresh()
    assert pump(qapp, lambda: s.chip_credits.text() == "1 credit")
    assert "ask your admin" not in s.chip_note.text()
    lwin.live.cloud.adjust_credits(lwin.L.k["id"], -1, "test", "boss")
    lwin.L.refresh()
    assert pump(qapp, lambda: s.chip_credits.text() == "0 credits") and "Out of credits" in s.chip_note.text()


def test_the_cost_of_a_booking_is_shown_before_pressing_book_now(lwin, qapp):
    fill(lwin, n=2)
    lwin.book.refresh_summary()
    assert "uses 2 credits" in lwin.book.summary.credit_line.text() and "you have 5" in lwin.book.summary.credit_line.text()
    fill(lwin, n=1)
    while len(lwin.book.rows) > 1:
        lwin.book.rows[-1].remove.click() if hasattr(lwin.book.rows[-1], "remove") else lwin.book.rows.pop()
    lwin.book.refresh_summary()


def test_with_too_few_credits_the_window_says_so_and_nothing_is_submitted(lwin, qapp):
    lwin.live.cloud.adjust_credits(lwin.L.k["id"], -4, "test", "boss")
    lwin.L.refresh()
    pump(qapp, lambda: lwin.L.account.credits == 1)
    fill(lwin, n=2)
    lwin.book.summary.book.click()
    assert lwin.fake.submitted == []
    assert any("needs 2 credits but you have 1" in t and "contact your admin" in t for t in lwin.info)
    assert lwin.book.summary.credit_line.isVisible() and "you have 1" in lwin.book.summary.credit_line.text()


def test_an_account_the_admin_blocked_cannot_book_and_the_window_explains(lwin, qapp):
    lwin.live.cloud.set_status(lwin.L.k["id"], "blocked", "shared the key", "boss")
    lwin.L.refresh()
    assert pump(qapp, lambda: not lwin.L.usable)
    assert not lwin.book.summary.book.isEnabled()
    assert "shared the key" in lwin.book.summary.credit_line.text() and "Access stopped" in lwin.sidebar.chip_credits.text()
    fill(lwin)
    lwin.book.summary.book.click()                                              # (even if it is pressed another way)
    lwin.start_booking(lwin.book.order())
    assert lwin.fake.submitted == [] and lwin.veil.isVisible() and "shared the key" in lwin.veil.msg.text()      # (the lock card carries the admin's words)
    lwin.live.cloud.set_status(lwin.L.k["id"], "active", "", "boss")
    lwin.L.resume()
    assert pump(qapp, lambda: lwin.book.summary.book.isEnabled())


def test_a_message_from_the_admin_is_shown_to_everybody(lwin, qapp):
    lwin.live.cloud.set_setting("broadcast", "IndiGo changed something - bookings may be slow today.", "boss")
    lwin.L.refresh()
    assert pump(qapp, lambda: lwin.book.summary.notice.isVisible())
    assert "IndiGo changed something" in lwin.book.summary.notice_text.text()
    lwin.live.cloud.set_setting("broadcast", "", "boss")
    lwin.L.refresh()
    assert pump(qapp, lambda: not lwin.book.summary.notice.isVisible())


def test_without_a_licence_the_window_shows_no_account_chip(qapp, store):
    w = MainWindow(store, lambda emit: FakeEngine(store, emit))
    w.show()
    w.start()
    assert not w.sidebar.chip.isVisible() and w.book.summary.credit_line.isHidden()
    w.close()


# ── the sign-in window ──────────────────────────────────────────────────────────────────────────────────────────────
def test_the_sign_in_window_accepts_a_good_key_and_explains_a_bad_one(qapp, live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=2, actor="boss")
    vault = Vault(tmp_path / "v.bin")
    L = lic.Licence(CloudClient(DeviceKey(vault), 1, base_url=live.url, server_public=live.public), vault, "test")
    dlg = shell_ui.LoginDialog(L, "RAHUL-PC (3c:52:82:aa:bb:cc)")
    dlg.show()
    dlg.key.setText("bf-aaaa-bbbb-cccc-dddd")
    dlg.key.textEdited.emit("bf-aaaa-bbbb-cccc-dddd")
    assert dlg.key.text() == "BF-AAAA-BBBB-CCCC-DDDD"                           # typed in lower case: shown in capitals
    dlg._go()
    assert not dlg.go.isEnabled()                                               # one press at a time; the network works off the window's thread
    assert pump(qapp, lambda: dlg.go.isEnabled())
    assert "not valid" in dlg.msg.text() and dlg.result() != dlg.DialogCode.Accepted
    dlg.key.setText(k["key"])
    dlg._go()
    assert pump(qapp, lambda: dlg.result() == dlg.DialogCode.Accepted)
    assert L.usable and L.account.credits == 2
    assert "RAHUL-PC" in dlg.findChildren(type(dlg.msg))[-1].text()
    dlg.close()


def test_the_sign_in_window_explains_a_second_computer_in_plain_words(qapp, live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=2, actor="boss")
    licensed_first = lic.Licence(CloudClient(DeviceKey(Vault(tmp_path / "a.bin")), 1, base_url=live.url, server_public=live.public), Vault(tmp_path / "a.bin"), "t")
    licensed_first.activate(k["key"])
    vault = Vault(tmp_path / "b.bin")
    L = lic.Licence(CloudClient(DeviceKey(vault), 1, base_url=live.url, server_public=live.public), vault, "test")
    dlg = shell_ui.LoginDialog(L)
    dlg.show()
    dlg.key.setText(k["key"])
    dlg._go()
    assert pump(qapp, lambda: dlg.go.isEnabled())
    assert "another computer" in dlg.msg.text() and "ask your admin" in dlg.msg.text().lower()
    dlg.close()


def test_the_sign_in_window_says_so_without_a_connection(qapp, tmp_path):
    vault = Vault(tmp_path / "v.bin")
    L = lic.Licence(CloudClient(DeviceKey(vault), 1, base_url="http://127.0.0.1:9", server_public="x" * 43), vault, "t")
    dlg = shell_ui.LoginDialog(L)
    dlg.show()
    dlg.key.setText("BF-AAAA-BBBB-CCCC-DDDD")
    dlg._go()
    assert pump(qapp, lambda: dlg.go.isEnabled(), 40)
    assert "internet" in dlg.msg.text().lower()
    dlg.close()
