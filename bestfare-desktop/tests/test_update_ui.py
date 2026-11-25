"""The update window, the 'update ready' card and the part that notices updates while the app is open - driven for real (Qt, threads, signals)."""
import time

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import autoupdate
import selfupdate
import update_ui as ui
from cloud import CloudError


@pytest.fixture(autouse=True)
def fonts(qapp):
    ui.load_fonts()


def pump(app, until, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end and not until():
        app.processEvents()
        time.sleep(0.01)
    return until()


def test_release_notes_become_a_short_clean_list():
    assert ui.notes_lines("- Faster start\n* Saved contacts; Payment fix\r\n\n•  Bigger text") == ["Faster start", "Saved contacts", "Payment fix", "Bigger text"]
    assert len(ui.notes_lines("\n".join(f"line {i}" for i in range(20)))) == 7 and ui.notes_lines("") == [] and len(ui.notes_lines("x" * 500)[0]) == 140


def test_the_window_shows_what_is_new_and_one_primary_button_per_state(qapp):
    d = ui.UpdateDialog(title="A new version is available", current="1.0.0", new="1.1.0", notes="Faster start\nSaved contacts")
    d.ask("Best Fare will restart.")
    d.show()
    assert d.primary.isVisible() and d.primary.text() == "Update now" and d.secondary.isVisible() and not d.steps.isVisible() and not d.meter.isVisible()
    d.working("Downloading…", "Skip this time")
    assert d.steps.isVisible() and d.meter.isVisible() and not d.primary.isVisible() and d.secondary.text() == "Skip this time"
    d.progress(40, 100)
    assert d.meter.bar.value() == 400 and d.steps.dots[0].property("dot") == "active"
    d.progress(100, 100)
    assert d.steps.dots[0].property("dot") == "done" and d.steps.dots[1].property("dot") == "active"
    d.show_done("Ready.", "Restart now")
    assert d.primary.isVisible() and d.primary.text() == "Restart now" and all(x.property("dot") == "done" for x in d.steps.dots) and d.meter.bar.value() == 1000
    d.failed("No internet.")
    assert d.problem.isVisible() and d.problem.text() == "No internet." and d.primary.text() == "Try again" and not d.steps.isVisible()
    d.close()


def test_a_required_update_cannot_be_skipped_or_closed_by_esc_or_the_x(qapp):
    d = ui.UpdateDialog(new="1.1.0", mandatory=True)
    d.ask("Required.")
    d.show()
    assert not d.secondary.isVisible() and not d.close_btn.isVisible()
    d.reject()
    d.close()
    assert d.isVisible()
    d.set_closable(True)
    d.reject()
    assert not d.isVisible()


def drive(dlg, job, clicks, **kw):
    """Run `run_update` with scripted clicks: [(milliseconds, 'primary' | 'secondary'), ...]."""
    for ms, which in clicks:
        QTimer.singleShot(ms, lambda w=which: getattr(dlg, w).click())
    return ui.run_update(dlg, job, **kw)


def test_the_work_runs_off_the_window_thread_shows_progress_and_closes_by_itself(qapp):
    dlg = ui.UpdateDialog(new="1.1.0")
    seen = []
    dlg.meter.set = lambda d, t, orig=dlg.meter.set: (seen.append((d, t)), orig(d, t))

    def job(progress):
        for i in range(1, 5):
            progress(i * 25, 100)
            time.sleep(0.02)
        return "result"
    assert ui.run_update(dlg, job, close_after_ms=10) == ("ok", "result")
    assert seen and seen[-1] == (100, 100)


def test_asking_first_later_means_nothing_runs_and_update_now_runs_it(qapp):
    ran = []
    dlg = ui.UpdateDialog(new="1.1.0")
    assert drive(dlg, lambda p: ran.append(1), [(80, "secondary")], ask="Update?") == ("later", None) and ran == []
    dlg = ui.UpdateDialog(new="1.1.0")
    out = drive(dlg, lambda p: ran.append(2) or "done", [(80, "primary")], ask="Update?", close_after_ms=10)
    assert out == ("ok", "done") and ran == [2]


def test_a_failure_is_shown_in_plain_words_and_try_again_runs_it_again(qapp):
    attempts = []

    def job(progress):
        attempts.append(1)
        if len(attempts) == 1:
            raise CloudError("offline", "Cannot reach the Best Fare server.")
        return "worked"
    dlg = ui.UpdateDialog(new="1.1.0")
    shown = []
    QTimer.singleShot(400, lambda: (shown.append(dlg.problem.text()), dlg.primary.click()))
    assert ui.run_update(dlg, job, close_after_ms=10) == ("ok", "worked") and len(attempts) == 2 and shown == ["Cannot reach the Best Fare server."]


def test_a_failure_can_be_left_with_close_and_a_required_window_offers_quit_instead_of_a_trap(qapp):
    def job(progress):
        raise CloudError("offline", "Cannot reach the Best Fare server.")
    dlg = ui.UpdateDialog(new="1.1.0", mandatory=True)
    QTimer.singleShot(400, lambda: (setattr(dlg, "_seen", dlg.secondary.text()), dlg.secondary.click()))
    outcome, err = ui.run_update(dlg, job)
    assert outcome == "error" and isinstance(err, CloudError) and dlg._seen == "Quit"


def test_a_download_can_be_skipped_while_it_runs(qapp):
    def job(progress):
        for i in range(500):
            progress(i, 500)
            time.sleep(0.01)
    dlg = ui.UpdateDialog(new="1.1.0")
    out = drive(dlg, job, [(300, "secondary")], cancel_button="Skip this time")
    assert out == ("cancelled", None)


# ── noticing updates while the app is open ─────────────────────────────────────────────────────────────────────────────
class FakeUpdater:
    def __init__(self, state=None, fails=False):
        self.releases = type("R", (), {"state": lambda s: state or {"current": 1, "previous": None, "bad": [], "good": [], "booting": None}})()
        self.fails, self.asked = fails, []

    def background(self, update):
        self.asked.append(update["build"])
        if self.fails:
            raise RuntimeError("no")
        return update["build"]


class FakeLicence:
    def __init__(self, update=None, installer=None):
        self.update = update or {"available": False}
        self.installer = installer or {"available": False}
        self.listeners = []
        self.client = object()

    def on_change(self, fn):
        self.listeners.append(fn)


def release(build=2, version="1.0.1", mandatory=False):
    return {"available": True, "build": build, "version": version, "notes": "Fixes the payment step", "mandatory": mandatory}


@pytest.fixture()
def calm(monkeypatch):
    calls = {"again": 0, "launched": []}
    monkeypatch.setattr(selfupdate, "start_again", lambda: calls.__setitem__("again", calls["again"] + 1))
    monkeypatch.setattr(selfupdate, "launch", lambda path, pid=None: calls["launched"].append(path))
    return calls


def test_a_newer_release_is_fetched_quietly_and_offered_once_restart_starts_the_app_again(qapp, calm):
    quit_calls = []
    L, up = FakeLicence(release()), FakeUpdater()
    auto = autoupdate.AutoUpdate(L, up, 1, quit_app=lambda: quit_calls.append(1), busy=lambda: False)
    auto.check()
    assert pump(qapp, lambda: auto.card is not None) and up.asked == [2]
    assert "1.0.1" in auto.card.text.text()
    auto.check()
    pump(qapp, lambda: False, 0.3)
    assert up.asked == [2]                                                     # (the same release is not fetched or announced twice)
    auto.card.go.click()
    assert calm["again"] == 1 and quit_calls == [1]
    auto.card.close()


def test_a_release_that_is_not_newer_than_the_running_one_does_nothing(qapp, calm):
    auto = autoupdate.AutoUpdate(FakeLicence(release(build=2)), FakeUpdater({"current": 2, "previous": 1, "bad": [], "good": [], "booting": None}), 2, busy=lambda: False)
    auto.check()
    pump(qapp, lambda: False, 0.3)
    assert auto.card is None and not auto.working
    auto = autoupdate.AutoUpdate(FakeLicence(release(build=3)), FakeUpdater({"current": 2, "previous": 1, "bad": [3], "good": [], "booting": None}), 2, busy=lambda: False)
    auto.check()
    pump(qapp, lambda: False, 0.3)
    assert auto.card is None                                                    # (a release that crashed here before is not offered again)


def test_a_restart_never_cuts_a_booking_short(qapp, calm):
    busy = {"yes": True}
    auto = autoupdate.AutoUpdate(FakeLicence(release()), FakeUpdater(), 1, quit_app=lambda: None, busy=lambda: busy["yes"])
    auto.check()
    assert pump(qapp, lambda: auto.card is not None)
    auto.card.go.click()
    assert calm["again"] == 0 and "booking is running" in auto.card.text.text() and not auto.card.go.isEnabled()
    busy["yes"] = False
    assert pump(qapp, lambda: calm["again"] == 1, 6)
    auto.card.close()


def test_a_required_release_is_asked_for_in_a_window_that_cannot_be_closed(qapp, calm):
    auto = autoupdate.AutoUpdate(FakeLicence(release(mandatory=True)), FakeUpdater(), 1, quit_app=lambda: None, busy=lambda: False)
    auto.check()
    assert pump(qapp, lambda: auto.modal is not None) and auto.card is None
    assert not auto.modal.secondary.isVisible() and auto.modal.primary.text() == "Restart now"
    auto.modal.primary.click()
    assert pump(qapp, lambda: calm["again"] == 1, 4)


def test_a_failed_quiet_download_is_not_shown_and_is_tried_again_at_the_next_check(qapp, calm):
    up = FakeUpdater(fails=True)
    auto = autoupdate.AutoUpdate(FakeLicence(release()), up, 1, busy=lambda: False)
    auto.check()
    assert pump(qapp, lambda: not auto.working and up.asked == [2])
    assert auto.card is None
    up.fails = False
    auto.check()
    assert pump(qapp, lambda: auto.card is not None)
    auto.card.close()


def test_a_newer_setup_is_fetched_checked_and_run_by_restart(qapp, calm, monkeypatch, tmp_path):
    offer = {"version": "1.1.0", "number": 1001000, "size": 2_000_000, "sha256": "a" * 64, "sig": "s", "notes": "n", "mandatory": False}
    setup = tmp_path / "BestFare-Setup-1.1.0.exe"
    monkeypatch.setattr(selfupdate, "check", lambda info: offer if info.get("available") else None)
    monkeypatch.setattr(selfupdate, "fetch", lambda client, o, progress=None: setup)
    quit_calls = []
    auto = autoupdate.AutoUpdate(FakeLicence(installer={"available": True}), FakeUpdater(), 1, quit_app=lambda: quit_calls.append(1), busy=lambda: False)
    auto.check()
    assert pump(qapp, lambda: auto.card is not None) and "1.1.0" in auto.card.text.text()
    auto.card.go.click()
    assert calm["launched"] == [setup] and calm["again"] == 0 and quit_calls == [1]
    auto.card.close()


def test_the_licence_refresh_wakes_the_watcher_so_news_is_not_left_for_the_timer(qapp, calm):
    L = FakeLicence()
    auto = autoupdate.AutoUpdate(L, FakeUpdater(), 1, busy=lambda: False)
    auto.start()
    assert len(L.listeners) == 1
    L.update = release()
    L.listeners[0](L)                                                           # (the licence changed, on another thread)
    assert pump(qapp, lambda: auto.card is not None)
    auto.card.close()
    auto._timer.stop()
