"""Getting the program's code to the user - and keeping it safe doing so.

    server says "build N is available"  ->  download the bundle  ->  check it (the developer's signature, the build number, the size, the
    hash the server announced)  ->  keep it (still encrypted)  ->  at start-up ask the server for the key that opens it, for THIS computer
    only  ->  open it in memory  ->  run it.

Safety rules, each one tested:
  * a bundle is believed only if the developer's signature is on it (the server cannot forge one);
  * the build number only ever goes up (an old, vulnerable version cannot be pushed back), unless the running one has proved bad;
  * a version that crashed before its window opened is marked bad and the previous one is used - the app never ends up in a state it
    cannot start from;
  * a stored bundle is checked again every time it is opened (a file changed on disk is thrown away);
  * only the current and the previous bundle are kept.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Optional

import bundle
import trust
from cloud import CloudClient, CloudError, Offline


class UpdateError(Exception):
    """Something the user can be told in plain words."""


class TryAgain(UpdateError):
    """Nothing is wrong with the program itself - the connection is (a damaged file could not be replaced right now). Nothing is marked bad."""


class Releases:
    """The folder of stored bundles and the little state file next to them."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def state_file(self) -> Path:
        return self.folder / "state.json"

    def state(self) -> dict:
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        if not isinstance(d, dict):
            d = {}

        def build(v):                           # (this file can be edited by anybody on this computer: only real build numbers are believed)
            return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10 ** 7 else None

        def builds(v):
            return [b for b in (v if isinstance(v, list) else []) if build(b)][-20:]

        return {"current": build(d.get("current")), "previous": build(d.get("previous")), "bad": builds(d.get("bad")),
                "good": builds(d.get("good")),   # versions that have shown their window at least once on this computer
                "booting": build(d.get("booting"))}

    def save(self, d: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def file(self, build: int) -> Path:
        return self.folder / f"{int(build)}.bfr"                    # (an int: no text can ever turn this into another folder)

    def store(self, build: int, data: bytes) -> None:
        tmp = self.file(build).with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, self.file(build))

    def tidy(self, keep: list[Optional[int]]) -> None:
        keep_names = {self.file(b).name for b in keep if b}
        for f in self.folder.glob("*.bfr"):
            if f.name not in keep_names:
                try:
                    f.unlink()
                except OSError:
                    pass
        for f in self.folder.glob("*.part"):
            try:
                f.unlink()
            except OSError:
                pass


class Updater:
    def __init__(self, client: CloudClient, releases: Releases, trusted: Optional[list[str]] = None):
        self.client, self.releases = client, releases
        self.trusted = list(trusted if trusted is not None else trust.RELEASE_PUBLIC)
        self._lock = threading.RLock()

    # ── checking and downloading ───────────────────────────────────────────────────────────────────────────────────
    def verify(self, data: bytes, want_build: Optional[int] = None) -> tuple[dict, bytes]:
        try:
            manifest, payload = bundle.read_bundle(data, self.trusted)
        except bundle.BundleError as e:
            raise UpdateError("An update could not be verified and was not installed.") from e
        if want_build is not None and manifest["build"] != want_build:
            raise UpdateError("An update did not match what the server announced and was not installed.")
        return manifest, payload

    def stage(self, info: dict, progress=None) -> int:
        """Download and verify the release the server announced (`info` is an answer of update_check / session). Returns its build."""
        build = info.get("build")
        if isinstance(build, bool) or not isinstance(build, int) or build < 1:
            raise UpdateError("The update information was not valid.")
        with self._lock:
            st = self.releases.state()
            cur = st["current"] or 0
            if build <= cur and cur not in st["bad"]:
                raise UpdateError("That update is not newer than this version.")           # (an old release can never be pushed back)
            if build in st["bad"]:
                raise UpdateError("That update did not work on this computer before.")
            size = info.get("size")
            if isinstance(size, int) and size > bundle.MAX_BUNDLE:
                raise UpdateError("The update is too large.")
            if self.releases.file(build).exists():
                data = self.releases.file(build).read_bytes()
            else:
                try:
                    data = self.client.download(build, progress) if progress else self.client.download(build)
                except CloudError as e:
                    raise UpdateError(e.message) from e
            if info.get("sha256") and hashlib.sha256(data).hexdigest() != info["sha256"]:
                raise UpdateError("The update was damaged on the way and was not installed.")
            self.verify(data, build)
            self.releases.store(build, data)
            return build

    def adopt(self, build: int) -> None:
        """Make a staged release the one to run next (while the app is open: the running version stays as 'previous' until the restart)."""
        with self._lock:
            st = self.releases.state()
            if st["current"] != build:
                st["previous"], st["current"] = st["current"], build
            self.releases.save(st)
            self.releases.tidy([st["current"], st["previous"]])

    # ── opening ────────────────────────────────────────────────────────────────────────────────────────────────────
    def open(self, build: int) -> tuple[dict, dict[str, bytes]]:
        """The release's files, decrypted in memory with the key this computer was given for it. Checked again, every time."""
        f = self.releases.file(build)
        try:
            data = f.read_bytes()
        except OSError as e:
            raise UpdateError("The program files are missing.") from e
        manifest, payload = self.verify(data, build)
        try:
            key = self.client.release_key(build)
        except CloudError as e:
            if e.code == "not_found":
                raise UpdateError("This version is no longer available. Please restart Best Fare to get the current one.") from e
            raise
        try:
            return manifest, bundle.decrypt_files(manifest, payload, key)
        except bundle.BundleError as e:
            raise UpdateError("The program files could not be opened.") from e

    def open_or_replace(self, build: int) -> tuple[dict, dict[str, bytes]]:
        """Open a stored release. A file that fails (damaged by a disk, an antivirus, or edited by someone) is thrown away and the SAME release
        is fetched again from the server - checked as strictly as the first time. Only when that fails too is the release given up."""
        try:
            return self.open(build)
        except UpdateError as first:
            if "no longer available" in str(first):
                raise
            try:
                data = self.client.download(build)
                self.verify(data, build)
            except Offline as e:                              # (the damaged file stays where it is: nothing is lost by waiting for the connection)
                raise TryAgain("Best Fare could not get its program files. Please check the internet connection and try again.") from e
            except (CloudError, UpdateError):
                try:
                    self.releases.file(build).unlink()
                except OSError:
                    pass
                raise first
            self.releases.store(build, data)
            return self.open(build)

    # ── the start-up decision ──────────────────────────────────────────────────────────────────────────────────────
    def prepare(self, update: dict, progress=None) -> tuple[dict, dict[str, bytes]]:
        """Which version runs now, with its files in memory. `update` is what the server said at sign-in."""
        with self._lock:
            st = self.releases.state()
            if st["booting"] is not None:                          # the last start never got as far as showing the window ...
                if st["booting"] not in st["good"]:                # ... a version that has NEVER worked here is bad: the previous one is used.
                    if st["booting"] not in st["bad"]:             # (one that has worked before is not blamed for a one-off: a power cut, a kill)
                        st["bad"].append(st["booting"])
                    if st["current"] == st["booting"]:
                        st["current"], st["previous"] = st["previous"], None
                st["booting"] = None
                self.releases.save(st)
            build = update.get("build") if update.get("available") else None
            cur = st["current"]
            if isinstance(build, int) and build > (cur or 0) and build not in st["bad"]:
                try:
                    self.adopt(self.stage(update, progress))
                except UpdateError:
                    if cur is None:
                        raise
            st = self.releases.state()
        order = [b for b in (st["current"], st["previous"]) if b and b not in st["bad"]]               # (a file that has gone missing is fetched again)
        if not order:
            raise UpdateError("Best Fare could not get its program files. Please check the internet connection and try again.")
        last: Optional[Exception] = None
        for b in order:
            try:
                manifest, files = self.open_or_replace(b)
            except TryAgain:
                raise
            except UpdateError as e:
                last = e
                st = self.releases.state()
                st["bad"].append(b)
                if st["current"] == b:
                    st["current"], st["previous"] = st["previous"], None
                self.releases.save(st)
                try:
                    self.releases.file(b).unlink()
                except OSError:
                    pass
                continue
            st = self.releases.state()
            if st["current"] != b:
                st["current"] = b
            st["booting"] = b
            self.releases.save(st)
            return manifest, files
        raise UpdateError(str(last) if last else "Best Fare could not start.")

    def boot_ok(self, build: int) -> None:
        """The window is up: this version works on this computer."""
        with self._lock:
            st = self.releases.state()
            if st["booting"] == build:
                st["booting"] = None
            if build not in st["good"]:
                st["good"] = (st["good"] + [build])[-5:]
            self.releases.save(st)

    # ── while the app runs ─────────────────────────────────────────────────────────────────────────────────────────
    def background(self, update: dict, progress=None) -> Optional[int]:
        """A newer release was announced while the app is open: fetch it quietly so the next start (or 'Restart now') has it ready."""
        build = update.get("build") if update.get("available") else None
        st = self.releases.state()
        if not isinstance(build, int) or build <= (st["current"] or 0) or build in st["bad"]:
            return None
        try:
            self.adopt(self.stage(update, progress))
        except UpdateError:
            return None
        return build
