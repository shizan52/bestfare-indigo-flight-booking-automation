r"""The admin panel and the app, connected for real - on any server (production included). NOTHING is booked: no browser, no IndiGo.

    set BESTFARE_ADMIN_USER / BESTFARE_ADMIN_PASS (an OWNER), then
    python tools\e2e_admin_app.py --admin https://bestfare-admin.shizanvps.me --server https://bestfare.shizanvps.me [--released]

It makes ONE temporary user through the panel's own API, opens the real app window with its key (the engine is a stand-in that can book nothing), and then does what an
admin does to a user - Block, Unblock, Edit (name, MAC lock), Reset computer, Show key, New key, Delete - checking after EACH step what the app did:

  * the window must lock (or stay open), with the admin's own words;
  * pressing Book must do nothing while the key may not book;
  * a restart of the app must show the key window (and refuse an old / deleted key) whenever it has to - the app never goes on without a key the server accepts;
  * the panel's activity log must say who did what.

`--released` does all of it with the program the server hands out (the published release, downloaded and verified the way a user's computer does it), not with the
workspace's files. Credits and bookings are NOT touched (that part has its own tests). At the end the user is deleted.
"""
from __future__ import annotations

import argparse
import faulthandler
import gc
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
HERE = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import release_tools as rt  # noqa: E402

LABEL = "E2E CONNECTION TEST (delete me)"
RESULTS: list[bool] = []


def check(ok, text: str, detail="") -> bool:
    RESULTS.append(bool(ok))
    print(("  PASS  " if ok else "  FAIL  ") + text, flush=True)
    if not ok and detail:
        print("          (" + str(detail) + ")", flush=True)
    return bool(ok)


def part(title: str) -> None:
    print("\n== " + title, flush=True)


def only_the_shell_from_files(work: Path) -> None:
    """--released: only the SHELL (what every user's computer has on disk) and the test stand-in may come from files; the program itself must come from the release."""
    shell_dir = work / "shell"
    shell_dir.mkdir()
    for n in rt.SHELL_FILES:
        shutil.copy(HERE / n, shell_dir / n)
    shutil.copy(HERE / "tests" / "fakes.py", shell_dir / "fakes.py")
    drop = {HERE.resolve(), (HERE / "tests").resolve()}
    sys.path[:] = [str(shell_dir)] + [p for p in sys.path if Path(p).resolve() not in drop]


def scenario(a, call, user: str, lid: int, key: str, work: Path) -> None:
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import licence as lic
    from cloud import CloudClient, CloudError, Offline
    from device import DeviceKey, Vault

    def pump(cond, seconds=25.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            if cond():
                return True
            time.sleep(0.03)
        return False

    def view() -> dict:
        return call("GET", f"/admin/api/licences/{lid}")["licence"]

    def events() -> list[dict]:
        return [e for e in call("GET", "/admin/api/events?limit=500")["events"] if e.get("licence_id") == lid]

    def pc(name: str, build: int = 3) -> "lic.Licence":
        """One computer: its own vault (device key + the saved sign-in). Asking again for the same name is that computer after a restart."""
        vault = Vault(work / f"{name}.bin")
        return lic.Licence(CloudClient(DeviceKey(vault), build, base_url=a.server), vault, "e2e")

    # ── 1. the key ──────────────────────────────────────────────────────────────────────────────────────────────────────────
    part("1. The key the admin made")
    L = pc("pc1")
    L.activate(key)
    check(L.state == lic.OK and L.account.credits == 5, "the app signs in with the key the admin made: 5 credits")
    d = view()
    check(d["bound"] and d["status"] == "active", f"the panel shows the key bound to this computer ({d['device']['name']})")
    check(call("POST", f"/admin/api/licences/{lid}/reveal-key")["key"] == key, "'Show key' shows exactly the key the user was given")

    up = manifest = None
    if a.released:
        part("2. The released program: what a user's computer downloads")
        import packed
        from updater import Releases, Updater
        L.client.build = 2                                                       # a computer that still runs an older program
        L.refresh()
        news = L.update if isinstance(L.update, dict) else {}
        check(news.get("available") and news.get("build", 0) >= 3, f"a computer on build 2 is told a newer program exists: build {news.get('build')} ({news.get('version')})")
        up = Updater(L.client, Releases(work / "releases"))
        manifest, files = up.prepare(L.update)
        check(manifest["build"] == news.get("build"), f"it downloads build {manifest['build']}: the developer's signature is checked and the files are opened in memory")
        differ = sorted(n for n, data in rt.collect().items() if files.get(n) != data)
        check(not differ, "the released program is exactly the program in the workspace that was tested" + (f" - DIFFERENT: {', '.join(differ)}" if differ else ""))
        L.client.build = manifest["build"]
        only_the_shell_from_files(work)
        packed.install(files)

    import paths
    from store import Store
    from fakes import FakeEngine
    import main_window as mw
    import shell_ui
    if a.released:
        check(str(mw.__file__).startswith("<bestfare:") and str(paths.__file__).startswith("<bestfare:"), "the window below runs from the released program in memory, not from workspace files")
    store = Store(paths.db_path())
    box: dict = {}

    def factory(emit):
        box["e"] = FakeEngine(store, emit)
        return box["e"]
    win = mw.MainWindow(store, factory, licence=L)
    win.show()
    win.start()
    app.processEvents()

    class Person:
        """Somebody at the keyboard for the windows that wait for a person: types the keys it was given into the key window, presses 'Close' on a 'stopped' window."""

        def __init__(self, keys=()):
            self.keys, self.log, self.errors, self.pending, self.last = list(keys), [], [], False, None
            self.timer = QTimer()
            self.timer.timeout.connect(self.tick)

        def __enter__(self):
            self.timer.start(40)
            return self

        def __exit__(self, *x):
            self.timer.stop()

        def tick(self):
            w = QApplication.activeModalWidget()
            if isinstance(w, shell_ui.LoginDialog):
                if id(w) != self.last:
                    self.last = id(w)
                    self.log.append(("key window", w.msg.text()))
                if not w.go.isEnabled():
                    return
                if self.pending and w.msg.text():
                    self.errors.append(w.msg.text())
                self.pending = False
                if self.keys:
                    w.key.setText(self.keys.pop(0))
                    self.pending = True
                    w._go()
                else:
                    w.reject()
            elif isinstance(w, shell_ui.StatusDialog) and w.title.text() in ("Your access has been stopped", "Your licence has ended"):
                self.log.append(("stopped window", w.title.text() + " / " + w.detail.text()))
                w.reject()

    def locked() -> bool:
        return win.veil.isVisible() and not win.pages.isEnabled()

    def is_open() -> bool:
        return not win.veil.isVisible() and win.pages.isEnabled() and win.sidebar.isEnabled()

    def settle() -> None:
        pump(lambda: not win._checking, 15)

    def stops_at_next_check(why: str) -> float:
        settle()
        t0 = time.monotonic()
        win._heartbeat()                                                                  # (what the window's timer does)
        ok = pump(locked)
        check(ok, f"{why}: the running window is covered {time.monotonic() - t0:.1f} s after its next check")
        return time.monotonic() - t0

    def book_does_nothing(why: str) -> None:
        """The user presses Book. (Only ever while the key may NOT book - and the engine is a stand-in, so nothing could be booked anyway.)"""
        if not locked():
            check(False, f"{why}: the window is locked before Book is pressed")
            return
        before = len(box["e"].submitted)
        win.start_booking(win.book.order(), win.book)
        check(len(box["e"].submitted) == before, f"{why}: pressing Book does nothing - no booking starts")

    def restart(keys=()) -> tuple:
        """The app is closed and opened again: the real start-up windows, with somebody typing `keys`. Returns (started, person)."""
        Lr = pc("pc1")
        with Person(keys) as who:
            started = shell_ui.sign_in(Lr, "TEST PC")
        return started, who, Lr

    patch = lambda body: call("PATCH", f"/admin/api/licences/{lid}", {"label": LABEL, "note": "", "mac_lock": "", "clear_expiry": False, "offers": ["offer1", "offer2"], **body})  # noqa: E731
    post = lambda what, body=None: call("POST", f"/admin/api/licences/{lid}/{what}", body)  # noqa: E731

    part("3. The window is open")
    check(not win.veil.isVisible() and win.pages.isEnabled(), f"the window is open and asks the server every {mw.HEARTBEAT_S // 60} minutes (and once before every booking)")

    # ── 4. block / unblock ─────────────────────────────────────────────────────────────────────────────────────────────────
    part("4. BLOCK and UNBLOCK")
    post("block", {"reason": "Blocked for the e2e test"})
    d = view()
    check(d["status"] == "blocked" and d.get("status_by") == user and "e2e test" in d["block_reason"], f"the panel says who blocked ({d.get('status_by')}) and why")
    stops_at_next_check("BLOCK")
    check("Your access has been stopped" in win.veil.title.text() and "e2e test" in win.veil.msg.text(), "the lock card says 'Your access has been stopped' with the admin's own words")
    box["e"].fare_requests.clear()
    win.book.fares_requested.emit("DEL", "BOM", win.book.order().day, 60)
    check(box["e"].fare_requests == [] and not win.sidebar.isEnabled(), "BLOCK: nothing under the lock works: no fare search, no menu, no form")
    book_does_nothing("BLOCK")
    try:
        L.begin(1, "offer1", "DEL-BOM", "2026-12-22", "e2e-blocked")
        check(False, "the server refuses a blocked user's booking")
    except CloudError as e:
        check(e.code in ("blocked", "unauthorized"), f"BLOCK: the SERVER refuses the booking too ('{e.code}') - the window is not the only guard")
    if up is not None:
        try:
            up.client.release_key(manifest["build"])
            check(False, "a blocked user cannot be given the key that opens the program")
        except CloudError as e:
            check(e.code in ("blocked", "unauthorized"), f"BLOCK: the server will not even hand a blocked user the key to open the program ('{e.code}')")
    orig = L.client._send
    L.client._send = lambda *x, **k: (_ for _ in ()).throw(Offline())
    L.refresh()
    app.processEvents()
    win._tick()
    check(win.veil.isVisible() and "e2e test" in win.veil.msg.text(), "BLOCK: pulling the cable does not undo it - the window stays locked")
    L.client._send = orig
    started, who, _ = restart()
    check(started is False and any(k == "stopped window" and "e2e test" in t for k, t in who.log), "BLOCK + RESTART: the app opens only a 'stopped' window with the admin's words and does not start")
    post("unblock")
    check(view()["status"] == "active", "the panel shows the user active again")
    win.veil.check.click()
    check(pump(is_open), "UNBLOCK: 'Check again' opens the window again by itself")
    settle()
    check(win._verified() is True, "UNBLOCK: the window's one check before a booking now says yes (nothing is started)")
    started, who, _ = restart()
    check(started is True and not who.log, "UNBLOCK + RESTART: the app starts without asking for anything")

    # ── 5. edit ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
    part("5. EDIT")
    patch({"label": LABEL + " (renamed)", "note": "e2e note"})
    win._check_now()
    settle()
    check(L.account.label == LABEL + " (renamed)" and is_open(), "EDIT name + note: the app shows the new name and the window stays open (the user is not thrown out)")
    mine = events()
    check(any(e["kind"] == "licence_edited" and "name" in e["detail"] and "note" in e["detail"] and e["actor"] == user for e in mine), "the activity log says exactly what was edited, and by whom")
    check(not any(e["kind"] == "offers_changed" for e in mine), "...and does not claim the coupons changed when they did not")
    n = sum(1 for e in mine if e["kind"] == "licence_edited")
    patch({"label": LABEL + " (renamed)", "note": "e2e note"})
    check(sum(1 for e in events() if e["kind"] == "licence_edited") == n, "a save that changes nothing writes nothing to the log")
    if a.full:                                                                            # (the server counts a wrong MAC like a wrong key: see --full)
        patch({"label": LABEL, "note": "", "mac_lock": "02:00:00:00:00:01"})
        stops_at_next_check("EDIT MAC lock (another computer)")
        check("different computer" in win.veil.msg.text(), "...it says: " + win.veil.msg.text()[:80])
        patch({"mac_lock": ""})
        win.veil.check.click()
        check(pump(is_open), "the MAC lock removed -> 'Check again' opens the window again")
    else:
        patch({"label": LABEL, "note": ""})

    # ── 6. reset computer ────────────────────────────────────────────────────────────────────────────────────────────────────
    part("6. RESET COMPUTER")
    post("reset-device")
    check(not view()["bound"], "the panel shows the key as 'not used yet': free for ONE new computer")
    stops_at_next_check("RESET")
    check("sign in again" in win.veil.title.text().lower(), "RESET: this computer is told 'Please sign in again'")
    book_does_nothing("RESET")
    with Person([key]) as who:
        win.veil.key.click()
    check(pump(is_open) and not who.errors and view()["bound"], "RESET: typing the key again in the window ('Use another key') opens it - this computer is the key's computer again")
    if a.full:                                                                            # (each refusal below is counted by the server as a wrong key: see --full)
        L2 = pc("pc2")
        try:
            L2.activate(key)
            check(False, "a second computer is refused")
        except CloudError as e:
            check(e.code == "device_mismatch", f"RESET: a SECOND computer with the same key is refused ('{e.message[:70]}')")
        check(view()["flagged"], "the panel flags the key: a second computer tried it")
        post("reset-device")
        L2.activate(key)
        check(L2.state == lic.OK, "after the next reset the second computer takes the key")
        stops_at_next_check("RESET (the key moved to the other computer)")
        with Person([key]) as who:
            win.veil.key.click()
        check(who.errors and "another computer" in who.errors[0] and locked(), "the first computer typing the key again is refused: 'already in use on another computer'",
              f"errors={who.errors} log={who.log} locked={locked()} title={win.veil.title.text()}")
        post("reset-device")
        with Person([key]) as who:
            win.veil.key.click()
        check(pump(is_open) and not who.errors, "(the key is moved back to this computer for the next steps)", f"errors={who.errors} log={who.log} locked={locked()} msg={win.veil.msg.text()}")

    # ── 7. new key ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    part("7. NEW KEY")
    new = post("regenerate-key")["key"]
    check(new != key and post("reveal-key")["key"] == new, "a different key was made, and 'Show key' now shows the NEW one")
    stops_at_next_check("NEW KEY")
    check("sign in again" in win.veil.title.text().lower(), "NEW KEY: the running window says 'Please sign in again'")
    check("new key" in win.veil.msg.text().lower(), "its words tell the user to type the new key: " + win.veil.msg.text()[:100])
    book_does_nothing("NEW KEY")
    check(not Vault(work / "pc1.bin").load().get("licence_key"), "NEW KEY: this computer forgot the old key (nothing is left that could open the app by itself)")
    started, who, _ = restart()
    check(started is False and any(k == "key window" for k, _ in who.log), "NEW KEY + RESTART: the app shows the key window and does not start without a key")
    started, who, Lr = restart([key, new])
    check(who.errors and "not valid" in who.errors[0], "NEW KEY + RESTART: the OLD key typed again is refused: " + (who.errors[0][:80] if who.errors else "?"))
    check(started is True and Lr.state == lic.OK and Lr.account.credits == 5, "NEW KEY + RESTART: the NEW key opens the app, credits (5) kept")
    win.veil.check.click()
    check(pump(is_open), "the window that was still open follows by itself once the new key is in")

    # ── 8. what the log says ─────────────────────────────────────────────────────────────────────────────────────────────────
    part("8. The activity log")
    mine = events()
    kinds = {e["kind"] for e in mine}
    want = {"licence_created", "device_bound", "blocked", "unblocked", "licence_edited", "device_reset", "key_viewed", "key_regenerated"} | ({"mac_refused", "device_refused"} if a.full else set())
    check(want <= kinds, "the log recorded every step" + (f" - MISSING: {sorted(want - kinds)}" if want - kinds else ""))
    check(all(e["actor"] == user for e in mine if e["kind"] in ("blocked", "unblocked", "licence_edited", "device_reset", "key_viewed", "key_regenerated")),
          "every admin action in the log carries the admin's name")

    # ── 9. delete ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
    part("9. DELETE")
    call("DELETE", f"/admin/api/licences/{lid}")
    stops_at_next_check("DELETE")
    check("sign in again" in win.veil.title.text().lower(), "DELETE: the running window says 'Please sign in again'")
    book_does_nothing("DELETE")
    started, who, _ = restart([new])
    check(started is False and who.errors and "not valid" in who.errors[0], "DELETE + RESTART: the key of the deleted user is refused and the app does not start",
          f"started={started} errors={who.errors} log={who.log}")
    try:
        view()
        gone = False
    except rt.ReleaseError:
        gone = True
    check(gone, "the panel no longer has the user")
    check(any(e["kind"] == "licence_deleted" and LABEL in e["detail"] and e["actor"] == user for e in call("GET", "/admin/api/events?limit=60")["events"]),
          "the activity log keeps who deleted whom")
    check(box["e"].submitted == [] and not store.bookings(5), "NOTHING was booked in the whole test: the stand-in engine was never asked to book, the history is empty")
    try:
        win.fake_close = True
        box["e"].busy = False
        win.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        store.close()                                                                     # (so the temporary folder can be deleted on Windows)
    except Exception:  # noqa: BLE001
        pass
    del win, store


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--admin", required=True, help="the admin panel's address (no /admin)")
    ap.add_argument("--server", required=True, help="the address the app talks to")
    ap.add_argument("--released", action="store_true", help="use the program the server hands out (the published release), not the workspace's files")
    ap.add_argument("--server-public", default="", help="the identity key of a THROW-AWAY server (the real server's is pinned inside the app)")
    ap.add_argument("--full", action="store_true", help="also the steps in which the server counts 'wrong keys' (a wrong MAC address, a second computer): FIVE of those in 15 minutes lock "
                                                        "the internet address out for 15 minutes - use it on a throw-away server, never twice on a real one")
    a = ap.parse_args()
    user, pw = os.environ.get("BESTFARE_ADMIN_USER"), os.environ.get("BESTFARE_ADMIN_PASS")
    if not user or not pw:
        print("Set BESTFARE_ADMIN_USER and BESTFARE_ADMIN_PASS first.")
        return 2
    if a.server_public:
        import trust
        trust.SERVER_PUBLIC = a.server_public
    faulthandler.dump_traceback_later(900, exit=True)                                    # (a window nobody can answer must not hang this test for ever)
    work = Path(tempfile.mkdtemp(prefix="bf-e2e-"))
    (work / "home").mkdir()
    os.environ["BESTFARE_HOME"] = str(work / "home")
    os.environ["BESTFARE_SERVER"] = a.server
    admin = rt.Admin(a.admin, user, pw)
    call = admin._json
    made = call("POST", "/admin/api/licences", {"label": LABEL, "credits": 5, "offers": ["offer1", "offer2"]})
    lid = made["id"]
    try:
        scenario(a, call, user, lid, made["key"], work)
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        RESULTS.append(False)
    finally:
        try:
            call("DELETE", f"/admin/api/licences/{lid}")
        except rt.ReleaseError:
            pass                                                                          # (already deleted by the test itself)
        except Exception as e:  # noqa: BLE001
            print("COULD NOT DELETE the temporary user:", e)
        try:
            left = [x for x in call("GET", "/admin/api/licences?limit=200&search=E2E%20CONNECTION")["items"] if "E2E CONNECTION TEST" in x.get("label", "")]
            print("temporary users left on the server:", len(left))
        except Exception as e:  # noqa: BLE001
            print("could not look for leftovers:", e)
        gc.collect()
        for _ in range(5):
            shutil.rmtree(work, ignore_errors=True)
            if not work.exists():
                break
            time.sleep(0.4)
    bad = RESULTS.count(False)
    print(f"\n{len(RESULTS) - bad} passed, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
