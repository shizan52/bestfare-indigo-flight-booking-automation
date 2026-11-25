"""What installing, updating and removing Best Fare actually DO - no windows here (setup_ui.py draws those), so it can be tested on its own.

An install is a per-user folder (no administrator needed), a start-menu and desktop shortcut, and an entry in Windows' "Installed apps" list:

    %LOCALAPPDATA%\\Programs\\Best Fare\\   BestFare.exe, _internal\\ ...      <- replaced as a whole by an update (the old one is kept until the new one is in place)
    %LOCALAPPDATA%\\BestFare\\             the user's data (history, saved contacts, sign-in, the booking browser's profile)   <- never touched by an update

An update never leaves a half-installed program: the new files are put in a sibling folder first, then the two folders are swapped with one rename each;
if anything fails, the old folder is put back.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

APP_NAME = "Best Fare"
EXE = "BestFare.exe"
MANIFEST = "bestfare-setup.json"                                   # inside the payload: {"version": ..., "size": installed bytes, "files": n}
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\BestFare"
APP_KEY = r"Software\BestFare"


def registry_keys() -> tuple[str, str]:
    """(the 'Installed apps' key, the key this setup reads its own record from) - for the real Windows account. Tests and the end-to-end tools set BESTFARE_TEST_HIVE to
    a made-up key, so that trying the installer can never touch the registration of a REAL installation of this user."""
    hive = os.environ.get("BESTFARE_TEST_HIVE")
    if hive:
        return hive + chr(92) + "Uninstall", hive + chr(92) + "App"
    return UNINSTALL_KEY, APP_KEY
NEED_FREE = 600 * 1024 * 1024
Progress = Callable[[str, int, int], None]                         # (what is happening, done, total)


class SetupError(Exception):
    """Something the user can be told in plain words."""


def nothing(_what: str, _done: int, _total: int) -> None:
    return None


def version_number(version: str) -> int:
    """1.2.3 -> 1002003 (the same rule the server uses; kept here so the setup needs nothing else)."""
    import re
    m = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})", str(version).strip())
    return int(m.group(1)) * 1_000_000 + int(m.group(2)) * 1_000 + int(m.group(3)) if m else 0


def wait_for_exit(pid: int, seconds: float = 40.0) -> bool:
    """Wait until a process has ended (True), or give up (False)."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k.OpenProcess(0x00100000, False, pid)                     # SYNCHRONIZE
        if not h:
            return True                                               # (already gone, or not ours to wait for)
        try:
            return k.WaitForSingleObject(h, int(seconds * 1000)) == 0
        finally:
            k.CloseHandle(h)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


# ── where things are ──────────────────────────────────────────────────────────────────────────────────────────────
def default_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_NAME


def data_dir() -> Path:
    base = os.environ.get("BESTFARE_HOME") or os.path.join(os.environ.get("LOCALAPPDATA") or str(Path.home()), "BestFare")
    return Path(base)


def log_file() -> Path:
    p = data_dir() / "logs"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(os.environ.get("TEMP", ".")) / "BestFare-Setup.log"
    return p / "setup.log"


def log(text: str) -> None:
    try:
        with open(log_file(), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def _folder(csidl: int) -> Optional[Path]:
    if os.name != "nt":
        return None
    import ctypes
    buf = ctypes.create_unicode_buffer(520)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
        return Path(buf.value)
    return None


def desktop_folder() -> Optional[Path]:
    return _folder(0x10)                                           # CSIDL_DESKTOPDIRECTORY


def programs_folder() -> Optional[Path]:
    return _folder(0x02)                                           # CSIDL_PROGRAMS (this user's Start menu \ Programs)


def shortcut_paths() -> dict[str, Optional[Path]]:
    fake = os.environ.get("BESTFARE_SHORTCUT_DIR")                 # (tests / end-to-end tools: shortcuts go to a made-up folder, never to the real desktop)
    if fake:
        return {"desktop": Path(fake) / "desktop" / f"{APP_NAME}.lnk", "startmenu": Path(fake) / "startmenu" / f"{APP_NAME}.lnk"}
    d, p = desktop_folder(), programs_folder()
    return {"desktop": d / f"{APP_NAME}.lnk" if d else None, "startmenu": p / f"{APP_NAME}.lnk" if p else None}


# ── the payload ───────────────────────────────────────────────────────────────────────────────────────────────────
def payload_path() -> Path:
    env = os.environ.get("BESTFARE_PAYLOAD")
    if env:
        return Path(env)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "payload.zip"
    return Path(__file__).resolve().parent / "dist" / "payload.zip"


def read_manifest(payload: Path) -> dict:
    try:
        with zipfile.ZipFile(payload) as z:
            m = json.loads(z.read(MANIFEST))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        raise SetupError("This setup file is damaged. Please download it again.") from e
    if not isinstance(m, dict) or not isinstance(m.get("version"), str) or not isinstance(m.get("size"), int):
        raise SetupError("This setup file is damaged. Please download it again.")
    return m


# ── what is installed now ────────────────────────────────────────────────────────────────────────────────────────
def installed() -> Optional[dict]:
    """The existing installation of this Windows user: {"dir": Path, "version": str} - or None."""
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_keys()[1]) as k:
            d = Path(winreg.QueryValueEx(k, "InstallDir")[0])
            v = str(winreg.QueryValueEx(k, "Version")[0])
    except OSError:
        return None
    return {"dir": d, "version": v} if (d / EXE).is_file() else None


def app_running(target: Path) -> bool:
    """Is Best Fare open from this folder? (A running program's file cannot be opened for writing.)"""
    exe = Path(target) / EXE
    if not exe.is_file():
        return False
    try:
        with open(exe, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def check_target(target: Path, need: int = NEED_FREE) -> None:
    """Refuse a folder that would be wrong, with a sentence that says why."""
    t = Path(target)
    if not t.is_absolute() or ".." in t.parts:
        raise SetupError("Please choose a full folder path.")
    if t.parent == t or len(t.parts) < 2:
        raise SetupError("Please choose a folder, not a whole drive.")
    win = Path(os.environ.get("WINDIR", r"C:\Windows")).resolve()
    pf = [Path(os.environ.get(k, "")).resolve() for k in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(k)]
    try:
        r = t.resolve()
    except OSError:
        raise SetupError("That folder cannot be used.")
    if r == win or win in r.parents:
        raise SetupError("Please do not install into the Windows folder.")
    if any(r == p or p in r.parents for p in pf):
        raise SetupError("Best Fare installs for your own Windows account. Please choose a folder in your own space (the suggested one is fine).")
    if t.exists() and not t.is_dir():
        raise SetupError("That is a file, not a folder.")
    if t.exists() and any(t.iterdir()) and not (t / EXE).is_file():
        raise SetupError("That folder already has other files in it. Please choose an empty or a new folder.")
    probe = t
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        raise SetupError("That drive cannot be used.")
    if free < need:
        raise SetupError(f"There is not enough free space there (about {need // 1024 // 1024} MB is needed).")
    try:
        t.mkdir(parents=True, exist_ok=True)
        test = t / ".bf-write-test"
        test.write_bytes(b"x")
        test.unlink()
    except OSError:
        raise SetupError("Best Fare cannot write to that folder. Please choose another.")


# ── the steps ────────────────────────────────────────────────────────────────────────────────────────────────────
def extract(payload: Path, dest: Path, progress: Progress = nothing) -> int:
    """Unpack the payload into `dest` (a fresh folder). Every name is checked to stay inside `dest`. Returns the bytes written."""
    dest = Path(dest)
    root = dest.resolve()
    try:
        z = zipfile.ZipFile(payload)
    except (OSError, zipfile.BadZipFile) as e:
        raise SetupError("This setup file is damaged. Please download it again.") from e
    with z:
        infos = [i for i in z.infolist() if i.filename != MANIFEST]
        total = sum(i.file_size for i in infos) or 1
        done = 0
        for i in infos:
            name = i.filename.replace("\\", "/")
            out = (dest / name).resolve()
            if name.startswith("/") or ":" in name or (out != root and root not in out.parents):
                raise SetupError("This setup file is damaged. Please download it again.")
            if i.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                with z.open(i) as src, open(out, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        done += len(chunk)
                        progress(name, done, total)
            except OSError as e:
                raise SetupError("Best Fare could not write its files there (is the disk full, or is a virus checker in the way?).") from e
    if not (dest / EXE).is_file():
        raise SetupError("This setup file is incomplete. Please download it again.")
    return done


def _rename(src: Path, dst: Path, seconds: float = 30.0) -> None:
    """A rename that waits a little: a virus checker or the closing program may hold a file for a moment."""
    end = time.monotonic() + seconds
    while True:
        try:
            os.rename(src, dst)
            return
        except OSError as e:
            if time.monotonic() > end:
                raise SetupError("Best Fare could not replace its files. Please close Best Fare (and anything that is looking at its folder) and try again.") from e
            time.sleep(0.4)


def remove_tree(path: Path, tries: int = 5) -> bool:
    for _ in range(tries):
        if not Path(path).exists():
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not Path(path).exists():
            return True
        time.sleep(0.5)
    return not Path(path).exists()


def swap_in(new: Path, target: Path) -> Optional[Path]:
    """Put `new` where `target` is. The old folder moves aside first and comes back if the new one cannot take its place. Returns the old folder (to delete)."""
    old: Optional[Path] = None
    if target.exists():
        old = target.with_name(target.name + ".old")
        remove_tree(old)
        _rename(target, old)
    try:
        _rename(new, target, seconds=10.0)
    except SetupError:
        if old is not None:
            try:
                _rename(old, target, seconds=10.0)
            except SetupError:
                pass
        raise
    return old


def install_files(payload: Path, target: Path, progress: Progress = nothing) -> Optional[Path]:
    """Unpack into a sibling folder, then swap it in. Returns the previous folder, if there was one (the caller deletes it when it is happy)."""
    target = Path(target)
    new = target.with_name(target.name + ".new")
    remove_tree(new)
    try:
        extract(payload, new, progress)
        old = swap_in(new, target)
    except BaseException:
        remove_tree(new)
        raise
    return old


def _powershell(script: str, env: dict) -> bool:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script],
                           env={**os.environ, **env}, capture_output=True, creationflags=flags, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def make_shortcut(link: Path, target: Path, description: str = APP_NAME) -> bool:
    """A .lnk file pointing at BestFare.exe in `target` (made with Windows' own shortcut object)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    exe = Path(target) / EXE
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:BF_LNK);$s.TargetPath=$env:BF_EXE;$s.WorkingDirectory=$env:BF_DIR;"
              "$s.IconLocation=$env:BF_EXE+',0';$s.Description=$env:BF_DESC;$s.Save()")
    ok = _powershell(script, {"BF_LNK": str(link), "BF_EXE": str(exe), "BF_DIR": str(target), "BF_DESC": description})
    return ok and link.is_file()


def make_shortcuts(target: Path, desktop: bool, startmenu: bool) -> list[str]:
    """Returns the names of the shortcuts that could not be made (never fatal: the program works without them)."""
    failed = []
    paths = shortcut_paths()
    for key, want in (("desktop", desktop), ("startmenu", startmenu)):
        link = paths[key]
        if want and link is not None and not make_shortcut(link, target):
            failed.append(key)
    return failed


def refresh_shortcuts(target: Path) -> None:
    """After an update: shortcuts that exist are made again (the icon and target stay right); ones the user deleted stay deleted."""
    for link in shortcut_paths().values():
        if link is not None and link.is_file():
            make_shortcut(link, target)


def register(target: Path, version: str, size_bytes: int) -> None:
    """The 'Installed apps' entry (Uninstall works from Windows Settings) and the record this setup finds next time."""
    if os.name != "nt":
        return
    import winreg
    exe = str(Path(target) / EXE)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry_keys()[0], 0, winreg.KEY_SET_VALUE) as k:
        for name, value in (("DisplayName", APP_NAME), ("DisplayVersion", version), ("Publisher", APP_NAME), ("InstallLocation", str(target)),
                            ("DisplayIcon", f'"{exe}",0'), ("UninstallString", f'"{exe}" --uninstall'), ("QuietUninstallString", f'"{exe}" --uninstall --silent'),
                            ("InstallDate", time.strftime("%Y%m%d"))):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        for name, value in (("NoModify", 1), ("NoRepair", 1), ("EstimatedSize", max(1, size_bytes // 1024))):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry_keys()[1], 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "InstallDir", 0, winreg.REG_SZ, str(target))
        winreg.SetValueEx(k, "Version", 0, winreg.REG_SZ, version)


def owns_registration(target: Path) -> bool:
    """Is `target` the installation the registry (and the shortcuts) belong to? Removing some OTHER copy must leave them alone."""
    have = installed()
    if have is None:
        return False
    try:
        return Path(have["dir"]).resolve() == Path(target).resolve()
    except OSError:
        return False


def unregister() -> None:
    if os.name != "nt":
        return
    import winreg
    for key in registry_keys():
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


def install(payload: Path, target: Path, *, desktop: bool = True, startmenu: bool = True, update: bool = False, progress: Progress = nothing) -> dict:
    """The whole install / update. Returns {"version", "dir", "shortcut_problems"}. Raises SetupError with a sentence fit to show."""
    m = read_manifest(payload)
    target = Path(target)
    progress("Preparing", 0, 1)
    if not update:
        check_target(target)
    if app_running(target):
        raise SetupError("Best Fare is open. Please close it and try again.")
    log(f"install {m['version']} -> {target} (update={update})")
    old = install_files(payload, target, progress)
    progress("Shortcuts", 0, 1)
    if update:
        refresh_shortcuts(target)
        problems: list[str] = []
    else:
        problems = make_shortcuts(target, desktop, startmenu)
    register(target, m["version"], int(m["size"]))
    if old is not None and not remove_tree(old):
        log(f"could not delete {old}")
    progress("Done", 1, 1)
    return {"version": m["version"], "dir": target, "shortcut_problems": problems}


# ── starting, and removing ──────────────────────────────────────────────────────────────────────────────────────────
def launch(target: Path, extra: Optional[list[str]] = None) -> None:
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    subprocess.Popen([str(Path(target) / EXE), *(extra or [])], cwd=str(target), close_fds=True, creationflags=flags)


def uninstall(target: Path, *, purge_data: bool = False, running_from_target: bool = False, progress: Progress = nothing) -> None:
    """Remove the program, its shortcuts and its entry. The user's data stays unless `purge_data`. When this very program runs from `target` (it
    does, for 'Uninstall' from Windows) its folder cannot be deleted yet: a detached helper deletes it a moment after this process ends."""
    target = Path(target)
    if not running_from_target and app_running(target):
        raise SetupError("Best Fare is open. Please close it and try again.")
    log(f"uninstall {target} (purge={purge_data})")
    mine = owns_registration(target)                                # (the shortcuts and the 'Installed apps' entry belong to the registered installation only)
    progress("Shortcuts", 0, 3)
    if mine:
        for link in shortcut_paths().values():
            if link is not None:
                try:
                    link.unlink()
                except OSError:
                    pass
    progress("Registry", 1, 3)
    if mine:
        unregister()
    progress("Files", 2, 3)
    victims = [target, target.with_name(target.name + ".old"), target.with_name(target.name + ".new")]
    if purge_data:
        victims.append(data_dir())
    if running_from_target:
        _delete_later(victims)
    else:
        for v in victims:
            if not remove_tree(v):
                raise SetupError("Some files could not be removed. Please close anything that is using Best Fare's folder and try again.")
    progress("Done", 3, 3)


def _delete_later(paths: list[Path]) -> None:
    """A hidden helper that waits for this process to end, then deletes the folders (the program cannot delete the folder it is running from).
    It is a small batch file in the temp folder (so the paths need no quoting tricks) that tries again for a while if something still holds a file."""
    import tempfile
    first = Path(paths[0])
    lines = ["@echo off", "set n=0", ":again", "ping -n 3 127.0.0.1 >nul"]
    for p in paths:
        if "%" in str(p) or '"' in str(p):
            continue                                              # (never put such a path into a batch file)
        lines.append(f'rmdir /s /q "{p}" 2>nul')
    lines += ["set /a n+=1", f'if exist "{first}" if %n% lss 12 goto again', 'del "%~f0"']
    fd, name = tempfile.mkstemp(prefix="bf-remove-", suffix=".cmd")
    with os.fdopen(fd, "w", encoding="mbcs", errors="replace") as f:
        f.write(chr(10).join(lines) + chr(10))                     # (text mode turns each line end into CR LF, which cmd wants)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(["cmd.exe", "/c", "call", name], close_fds=True, creationflags=flags, cwd=os.environ.get("TEMP") or None)
