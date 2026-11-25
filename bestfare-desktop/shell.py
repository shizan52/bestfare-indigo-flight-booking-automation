"""The shell: the small, plain part of Best Fare that gets a user from "double-click" to the app.

    1. sign in   - the saved key is checked with the server (or the key is typed in), this computer is bound to it
    2. the app   - (distribution) the release is downloaded, verified and opened in memory; (workspace) the files on disk are used
    3. run       - app.main() with the licence

Everything that decides who may book - credits, blocked, expired, the offer codes - stays on the server.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import device
import licence as lic
import trust
from cloud import CloudClient
from device import DeviceKey, Vault


def current_build() -> int:
    """The build this computer runs: the installed release's (a distribution), or the workspace's own number.
    (There is deliberately no setting to override it: the number that is reported is the number that runs.)"""
    try:
        from updater import Releases
        cur = Releases(device.data_dir() / "releases").state()["current"]
        if cur:
            return int(cur)
    except Exception:  # noqa: BLE001
        pass
    try:
        import buildinfo
        return int(buildinfo.BUILD)
    except ImportError:
        return 0


def make_licence() -> lic.Licence:
    vault = Vault(device.data_dir() / "vault.bin")
    client = CloudClient(DeviceKey(vault), current_build())
    return lic.Licence(client, vault, str(current_build()))


def computer_text() -> str:
    info = device.device_info()
    return f"{info['name']}" + (f"  ({', '.join(info['macs'][:2])})" if info["macs"] else "")


def sign_in(app) -> Optional[lic.Licence]:
    """Show the connecting / key windows. None = the user closed them (the app does not start)."""
    import shell_ui
    L = make_licence()
    return L if shell_ui.sign_in(L, computer_text()) else None


def dev_mode() -> bool:
    """The workspace's own tools and tests run without a licence (the development gate, codes from the environment)."""
    return bool(os.environ.get("BESTFARE_DEV_OFFERS"))


def setup_update(L: lic.Licence, offer: dict) -> bool:
    """A newer SETUP is offered for the installed app itself: download it, check it, run it (it replaces the program and starts it again).
    True = the setup was started and this process must end; False = go on with the version that is installed (skipped, or it did not work)."""
    import selfupdate
    import update_ui as ui
    dlg = ui.UpdateDialog(title="A new version is available", tagline="Best Fare is updating itself", current=trust.SHELL_VERSION, new=offer["version"], notes=offer["notes"],
                          mandatory=offer["mandatory"])
    outcome, result = ui.run_update(dlg, lambda progress: selfupdate.fetch(L.client, offer, progress), working_text=f"Downloading Best Fare {offer['version']}…",
                                    cancel_button=None if offer["mandatory"] else "Skip this time", done_text="Starting the update…", close_after_ms=600)
    if outcome == "ok":
        try:
            selfupdate.launch(result)
            return True
        except selfupdate.SetupError:
            return False
    return offer["mandatory"] and outcome == "error"           # (a required update that cannot be had: the app does not go on with an old shell)


def prepare_program(L: lic.Licence, up) -> tuple:
    """Get the program ready: the first start downloads it, a newer release is downloaded now (with a window that shows how far it is), otherwise
    the stored one is simply opened. Returns (manifest, files). Raises UpdateError / CloudError for the caller to tell the user."""
    import update_ui as ui
    import shell_ui
    st = up.releases.state()
    cur = st["current"] or 0
    news = L.update if isinstance(L.update, dict) else {}
    build = news.get("build") if news.get("available") else None
    fresh = isinstance(build, int) and not isinstance(build, bool) and build > cur and build not in st["bad"]
    if not fresh:
        return shell_ui.run_with_progress("Starting Best Fare…", "A moment, please.", lambda: up.prepare(L.update))
    first = cur == 0
    dlg = ui.UpdateDialog(title="Getting Best Fare ready" if first else "Updating Best Fare", tagline="First start" if first else "Getting the latest version",
                          new="" if first else str(news.get("version") or ""), notes="" if first else str(news.get("notes") or ""),
                          steps=("Download", "Verify", "Start"), mandatory=True)
    outcome, result = ui.run_update(dlg, lambda progress: up.prepare(L.update, progress), working_text="The first start downloads the program. A slow connection takes a little longer."
                                    if first else "Downloading the new version…", done_text="Ready", close_after_ms=700)
    if outcome == "ok":
        return result
    if outcome == "error":
        raise result
    raise UpdateErrorClosed()


class UpdateErrorClosed(Exception):
    """The user closed the start-up window instead of letting it finish."""


def run_packed(demo: bool = False, debug: bool = False, smoke=None) -> int:
    """The distribution's way in: sign in, fetch/verify/open the release in memory, run it. Nothing of the program but this shell is on disk.
    `smoke` (a function) is called instead of the usual 'window is up' marker - the build's own test uses it to run without a person."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication, QMessageBox

    import autoupdate
    import packed
    import selfupdate
    import shell_ui
    import update_ui as ui
    from updater import Releases, Updater, UpdateError
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = QApplication.instance() or QApplication(sys.argv)
    ui.load_fonts()
    qapp.setWindowIcon(QIcon(ui.logo(64)))
    L = make_licence()
    if not shell_ui.sign_in(L, computer_text()):
        return 0
    offer = selfupdate.check(getattr(L, "installer", None)) if selfupdate.frozen() and not demo else None
    if offer is not None and setup_update(L, offer):
        return 0
    up = Updater(L.client, Releases(device.data_dir() / "releases"))
    try:
        manifest, files = prepare_program(L, up)
    except UpdateErrorClosed:
        return 0
    except UpdateError as e:
        QMessageBox.warning(None, "Best Fare", str(e))
        return 1
    except Exception as e:  # noqa: BLE001
        QMessageBox.warning(None, "Best Fare", getattr(e, "message", None) or "Best Fare could not start. Please check the internet connection and try again.")
        return 1
    L.client.build = manifest["build"]
    packed.install(files)
    import app as appmod
    watcher = {}

    def ready() -> None:
        up.boot_ok(manifest["build"])
        if smoke is not None:
            smoke()
            return
        if not demo:                                              # while the app is open: newer releases / setups are fetched quietly and offered
            watcher["auto"] = autoupdate.AutoUpdate(L, up, manifest["build"])
            watcher["auto"].start()
    return appmod.main(demo=demo, debug=debug, licence=L, on_ready=ready)
