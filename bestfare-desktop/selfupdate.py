"""The installed app updating ITSELF: a newer setup program is offered by the server, fetched, checked and run.

The program of Best Fare arrives as a signed, encrypted release (updater.py) - that covers every change of the booking code. This module covers
the other part: the small plain app that holds it (the .exe, Python, Qt). When the developer publishes a new SETUP, every installed copy:

    1. hears about it at sign-in (the server's signed answer: version, size, SHA-256, the developer's signature),
    2. downloads it into its own data folder,
    3. checks the SHA-256 AND the developer's signature over (version, hash) - the server cannot forge that signature,
    4. closes itself and runs the setup in update mode, which replaces the program folder and starts Best Fare again.

Nothing is run that the developer's key has not signed. A setup that is not newer than the installed one is never fetched (no going back).
(This is part of the shell: it is plain code on the user's computer.)
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import bundle
import setup_core
import trust
from cloud import CloudClient, CloudError

SETUP_PATH = "/download/BestFare-Setup.exe"                 # the only address a setup is ever fetched from (a server answer cannot name another)
_HEX = re.compile(r"^[0-9a-f]{64}$")


class SetupError(Exception):
    """Something the user can be told in plain words."""


def installed_number() -> int:
    try:
        return bundle.version_number(trust.SHELL_VERSION)
    except ValueError:
        return 0


def check(info: Optional[dict]) -> Optional[dict]:
    """The server's offer, cleaned up - or None when there is nothing to do (no offer, not newer, or not a valid offer)."""
    if not isinstance(info, dict) or not info.get("available"):
        return None
    try:
        version, size, sha, sig = info["version"], info["size"], str(info["sha256"]).lower(), info["sig"]
        number = bundle.version_number(version)
    except (KeyError, ValueError, TypeError):
        return None
    if number <= installed_number() or isinstance(size, bool) or not isinstance(size, int) or not 1_000_000 <= size <= bundle.MAX_INSTALLER:
        return None
    if not _HEX.match(sha) or not isinstance(sig, str) or info.get("path") != SETUP_PATH:
        return None
    return {"version": version, "number": number, "size": size, "sha256": sha, "sig": sig, "notes": str(info.get("notes") or "")[:2000], "mandatory": bool(info.get("mandatory"))}


def folder() -> Path:
    import device
    p = device.data_dir() / "setup"
    p.mkdir(parents=True, exist_ok=True)
    return p


def target(offer: dict) -> Path:
    return folder() / f"BestFare-Setup-{offer['version']}.exe"


def staged(offer: dict) -> Optional[Path]:
    """A setup of this version that was fetched and checked earlier (it is checked again before it is used)."""
    p = target(offer)
    try:
        if p.is_file() and verify_file(p, offer):
            return p
    except SetupError:
        pass
    return None


def verify_file(path: Path, offer: dict) -> bool:
    """The developer signed exactly this version with exactly this content. Anything else is refused."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            head = f.read(2)
            f.seek(0)
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        raise SetupError("The update file could not be read.") from e
    if head != b"MZ" or h.hexdigest() != offer["sha256"]:
        return False
    return bundle.verify_installer(offer["version"], offer["sha256"], offer["sig"], trust.RELEASE_PUBLIC)


def fetch(client: CloudClient, offer: dict, progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Download (or reuse) the setup of `offer` and check it. Raises SetupError when it cannot be trusted - the file is deleted then."""
    have = staged(offer)
    if have is not None:
        if progress:
            progress(offer["size"], offer["size"])
        return have
    part = target(offer).with_suffix(".part")
    try:
        got = client.fetch_file(SETUP_PATH, part, limit=offer["size"] + 4096, progress=progress)
    except CloudError as e:
        _remove(part)
        raise SetupError(e.message) from e
    if got != offer["sha256"] or part.stat().st_size != offer["size"]:
        _remove(part)
        raise SetupError("The update was damaged on the way and was not installed.")
    if not verify_file(part, offer):
        _remove(part)
        raise SetupError("The update could not be verified and was not installed.")
    os.replace(part, target(offer))
    _tidy(keep=target(offer))
    return target(offer)


def _remove(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _tidy(keep: Optional[Path] = None) -> None:
    """Old setups are not kept (they are large)."""
    for f in folder().glob("BestFare-Setup-*"):
        if keep is None or f != keep:
            _remove(f)


def launch(setup: Path, wait_pid: Optional[int] = None) -> None:
    """Run the setup in update mode, apart from this process, which is about to close."""
    args = [str(setup), "--update", "--wait-pid", str(wait_pid or os.getpid())]
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    try:
        subprocess.Popen(args, close_fds=True, creationflags=flags, cwd=str(setup.parent))
    except OSError as e:
        if getattr(e, "winerror", 0) == 5 and flags:                      # (a job object that forbids leaving it: try again without the flag)
            subprocess.Popen(args, close_fds=True, creationflags=flags & ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0), cwd=str(setup.parent))
        else:
            raise SetupError("The update could not be started.") from e


# ── closing and starting again ─────────────────────────────────────────────────────────────────────────────────────
def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def restart_command() -> list[str]:
    """What starts this app again the way it was started."""
    if frozen():
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().with_name("run.py"))]


def start_again() -> None:
    """Start a new copy that waits for this one to finish (so the single-copy lock is free by then). The caller quits right after."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([*restart_command(), "--wait-pid", str(os.getpid())], close_fds=True, creationflags=flags)


wait_for_exit = setup_core.wait_for_exit
