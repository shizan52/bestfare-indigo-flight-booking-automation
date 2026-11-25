r"""Build Best Fare's Windows setup program.

    python tools\build_installer.py --server https://api.example.com:8443 --version 1.0.0 [--pin HEX] [--notes "..."] [--out dist] [--key KEYFILE]
    python tools\build_installer.py ... --reuse-app          # only the setup part again (the app folder from the last run is kept)

What it makes, in OUT (default: dist\):
    BestFare-Setup.exe        the one file people download (the install wizard with the whole app inside)
    BestFare-Setup.json       its version, size, SHA-256 and the developer's signature - what tools\publish_installer.py uploads

How:
  1. the SHELL (the small plain app: sign-in, updater, windows) is copied to a work folder and the server address, the certificate PIN and the
     version are stamped into trust.py - nothing of the booking program is in it (it arrives from the server, signed and encrypted, after sign-in)
  2. PyInstaller freezes it into BestFare.exe (+ Python, Qt, aiohttp, cryptography) - every module the program will import later is listed, so it can run
  3. the folder is zipped into the payload and PyInstaller makes the install wizard with the payload inside
  4. the developer's release key signs (version, SHA-256) - apps only ever run a setup that carries this signature
Work files live in %TEMP%\bestfare-build and are removed at the end (unless --keep-work).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_tools as rt  # noqa: E402
import make_distribution as md  # noqa: E402

HERE = rt.HERE
EXE_SOURCES = ("entry.py",)                                       # the installed app's own start (besides the shell files)
SETUP_SOURCES = ("setup_main.py", "setup_core.py", "setup_ui.py", "update_ui.py")
SHELL_FOR_EXE = tuple(n for n in rt.SHELL_FILES if n != "run.py")
# the setup program needs none of the network / crypto part of the shell
SETUP_EXCLUDES = ("selfupdate", "cloud", "device", "licence", "trust", "bundle", "autoupdate", "shell", "shell_ui", "packed", "updater", "cryptography",
                  "aiohttp", "tkinter", "unittest", "pydoc", "sqlite3", "asyncio", "ssl", "email", "http", "xml", "multiprocessing", "PyQt6.QtNetwork",
                  "PyQt6.QtSvg", "PyQt6.QtOpenGL", "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtWebEngineCore", "PyQt6.QtPrintSupport", "PyQt6.QtSql", "PyQt6.QtTest",
                  "PyQt6.QtMultimedia", "PyQt6.QtDBus", "PyQt6.QtBluetooth", "PyQt6.QtPositioning", "PyQt6.QtSensors", "PyQt6.QtXml", "PyQt6.QtPdf", "PyQt6.QtNfc")
APP_EXCLUDES = ("tkinter", "unittest", "pydoc", "test", "PyQt6.QtSvg", "PyQt6.QtOpenGL", "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtWebEngineCore", "PyQt6.QtPrintSupport",
                "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtMultimedia", "PyQt6.QtDBus", "PyQt6.QtBluetooth", "PyQt6.QtPositioning", "PyQt6.QtSensors", "PyQt6.QtXml",
                "PyQt6.QtPdf", "PyQt6.QtNfc", "PyQt6.QtNetwork")


class BuildError(Exception):
    pass


def say(text: str = "") -> None:
    print(text, flush=True)


# ── the module list: everything the shell AND the program will import ─────────────────────────────────────────────
def program_sources() -> list[Path]:
    out = [HERE / n for n in rt.APP_MODULES] + [rt.ENGINE / n for n in rt.ENGINE_MODULES]
    out += [HERE / n for n in SHELL_FOR_EXE + EXE_SOURCES + SETUP_SOURCES]
    return [p for p in out if p.exists()]


def collect_imports(files: list[Path]) -> list[str]:
    """Every module the code imports (also the ones imported inside functions), minus the code's own modules. These become PyInstaller hidden imports,
    so the program that arrives later finds everything it needs inside the frozen app."""
    import importlib.util
    local = {p.stem for p in files} | {p.stem for p in HERE.glob("*.py")} | {p.stem for p in rt.ENGINE.glob("*.py")}
    found: set[str] = set()
    for p in files:
        tree = ast.parse(p.read_text(encoding="utf-8-sig"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    found.add(a.name)
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                found.add(n.module)
                for a in n.names:
                    full = f"{n.module}.{a.name}"
                    try:
                        if importlib.util.find_spec(full) is not None:
                            found.add(full)
                    except (ImportError, ValueError, AttributeError):
                        pass
    keep = set()
    for m in found:
        top = m.split(".")[0]
        if top in local or top in ("telegram", "__main__", "winreg", "msvcrt", "_winapi", "nt"):
            continue
        try:
            if importlib.util.find_spec(m) is None:
                continue
        except (ImportError, ValueError, AttributeError):
            continue
        keep.add(m)
    return sorted(keep | {"winreg"})


# ── the icon ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def make_icon(path: Path) -> None:
    """bestfare.ico: the logo at 16..256 px, each size stored as a PNG inside the .ico container."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    sys.path.insert(0, str(HERE))
    import update_ui as ui
    ui.load_fonts()
    sizes = (16, 24, 32, 48, 64, 128, 256)
    blobs = []
    for s in sizes:
        pm = ui.logo(s)
        pm.setDevicePixelRatio(1.0)
        pm = pm.scaled(s, s)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pm.save(buf, "PNG")
        blobs.append(bytes(buf.data()))
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, b in zip(sizes, blobs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(b), offset)
        offset += len(b)
    path.write_bytes(head + entries + b"".join(blobs))
    del app


def version_file(path: Path, version: str, description: str, original: str) -> None:
    a, b, c = (int(x) for x in version.split("."))
    v = f"{a}, {b}, {c}, 0"
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v}), prodvers=({v}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Best Fare'), StringStruct('FileDescription', '{description}'), StringStruct('FileVersion', '{version}.0'),
      StringStruct('InternalName', '{original.split('.')[0]}'), StringStruct('OriginalFilename', '{original}'), StringStruct('ProductName', 'Best Fare'),
      StringStruct('ProductVersion', '{version}.0')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ])
""", encoding="utf-8")


# ── pieces ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def stage_shell(stage: Path, server: str, pin: str, version: str, imports: list[str]) -> None:
    """The shell's plain code, stamped. Nothing else of the program is in here."""
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for n in SHELL_FOR_EXE + EXE_SOURCES + SETUP_SOURCES:
        shutil.copy2(HERE / n, stage / n)
    t = (stage / "trust.py").read_text(encoding="utf-8")
    for pattern, repl in ((r'^SERVER_URL = "[^"]*"', f'SERVER_URL = "{server}"'), (r'^TLS_PIN = "[^"]*"', f'TLS_PIN = "{pin}"'),
                          (r'^SHELL_VERSION = "[^"]*"', f'SHELL_VERSION = "{version}"')):
        t, n = re.subn(pattern, repl, t, flags=re.M)
        if n != 1:
            raise BuildError(f"could not stamp trust.py ({pattern[:20]})")
    (stage / "trust.py").write_text(t, encoding="utf-8")
    (stage / "_bf_imports.py").write_text("MODULES = " + json.dumps(imports) + "\n", encoding="utf-8")
    for f in stage.glob("*.py"):                                 # the same safety net as for a release and a distribution
        txt = f.read_text(encoding="utf-8")
        for pat in rt.SECRET_PATTERNS:
            if pat.search(txt):
                raise BuildError(f"{f.name} contains something that must never be shipped")


def pyinstaller(args: list[str], work: Path) -> None:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN", *args]
    say("  " + " ".join(Path(a).name if os.sep in a else a for a in cmd[3:8]) + " ...")
    r = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError("PyInstaller failed:\n" + (r.stdout + r.stderr)[-3000:])


def freeze_app(work: Path, stage: Path, imports: list[str], icon: Path, version: str) -> Path:
    vf = work / "app_version.txt"
    version_file(vf, version, "Best Fare", "BestFare.exe")
    args = ["--onedir", "--windowed", "--name", "BestFare", "--icon", str(icon), "--version-file", str(vf), "--distpath", str(work / "app-dist"),
            "--workpath", str(work / "app-build"), "--specpath", str(work), "--paths", str(stage), "--noupx"]
    for m in imports:
        args += ["--hidden-import", m]
    for m in APP_EXCLUDES:
        args += ["--exclude-module", m]
    for extra in ("_bf_imports", "update_ui", "setup_core", "setup_ui", "selfupdate", "autoupdate", "shell", "shell_ui", "packed", "updater", "bundle", "trust", "device", "cloud", "licence"):
        args += ["--hidden-import", extra]
    args.append(str(stage / "entry.py"))
    pyinstaller(args, work)
    out = work / "app-dist" / "BestFare"
    if not (out / "BestFare.exe").is_file():
        raise BuildError("the app folder was not made")
    return out


def check_app(app_dir: Path, work: Path) -> dict:
    """The frozen app must be able to import everything and draw a window - without Python, without internet."""
    report = work / "selfcheck.json"
    report.unlink(missing_ok=True)
    r = subprocess.run([str(app_dir / "BestFare.exe"), "--selfcheck", "--out", str(report)], capture_output=True, text=True, timeout=180)
    if not report.exists():
        raise BuildError("the built app did not run its self-check:\n" + (r.stdout + r.stderr)[-1500:])
    data = json.loads(report.read_text(encoding="utf-8"))
    if not data.get("ok"):
        raise BuildError("the built app's self-check failed: " + json.dumps({k: data.get(k) for k in ("missing", "errors")}))
    return data


def make_payload(app_dir: Path, version: str, notes: str, payload: Path) -> dict:
    total = files = 0
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(app_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(app_dir).as_posix())
                total += f.stat().st_size
                files += 1
        manifest = {"version": version, "size": total, "files": files, "notes": notes[:1500]}
        z.writestr("bestfare-setup.json", json.dumps(manifest))
    return manifest


def freeze_setup(work: Path, stage: Path, payload: Path, icon: Path, version: str) -> Path:
    vf = work / "setup_version.txt"
    version_file(vf, version, "Best Fare Setup", "BestFare-Setup.exe")
    args = ["--onefile", "--windowed", "--name", "BestFare-Setup", "--icon", str(icon), "--version-file", str(vf), "--distpath", str(work / "setup-dist"),
            "--workpath", str(work / "setup-build"), "--specpath", str(work), "--paths", str(stage), "--noupx", "--add-data", f"{payload}{os.pathsep}."]
    for m in SETUP_EXCLUDES:
        args += ["--exclude-module", m]
    args.append(str(stage / "setup_main.py"))
    pyinstaller(args, work)
    exe = work / "setup-dist" / "BestFare-Setup.exe"
    if not exe.is_file():
        raise BuildError("the setup program was not made")
    return exe


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build(server: str, version: str, out: Path, *, pin: str = "", notes: str = "", key: Path | None = None, work: Path | None = None, reuse_app: bool = False,
          keep_work: bool = False) -> dict:
    import bundle
    server = md.check_address(server)
    pin = md.check_pin(pin)
    try:
        bundle.version_number(version)
    except ValueError:
        raise BuildError("the version must look like 1.2.3")
    work = Path(work or Path(os.environ.get("TEMP", ".")) / "bestfare-build")
    work.mkdir(parents=True, exist_ok=True)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    stage = work / "stage"
    t0 = time.time()
    say(f"Best Fare {version}  ->  {server}")
    imports = collect_imports(program_sources())
    say(f"1/5  the shell: {len(SHELL_FOR_EXE)} files stamped, {len(imports)} modules the program will need")
    stage_shell(stage, server, pin, version, imports)
    icon = work / "bestfare.ico"
    make_icon(icon)
    app_dir = work / "app-dist" / "BestFare"
    if reuse_app and (app_dir / "BestFare.exe").is_file():
        say("2/5  (the app from the last build is reused)")
    else:
        say("2/5  freezing the app (a few minutes)")
        app_dir = freeze_app(work, stage, imports, icon, version)
    say("3/5  checking the frozen app")
    chk = check_app(app_dir, work)
    say(f"      ok: {chk['checked']} modules, window {chk['window_pixels']}")
    say("4/5  packing the payload and the setup program")
    payload = work / "payload.zip"
    manifest = make_payload(app_dir, version, notes, payload)
    exe = freeze_setup(work, stage, payload, icon, version)
    final = out / "BestFare-Setup.exe"
    shutil.copy2(exe, final)
    digest = sha256(final)
    meta = {"version": version, "size": final.stat().st_size, "sha256": digest, "installed_size": manifest["size"], "server": server, "built": time.strftime("%Y-%m-%d %H:%M:%S")}
    if key is not None:
        say("5/5  signing")
        meta["sig"] = bundle.sign_installer(version, digest, rt.load_signing_key(key))
    else:
        say("5/5  (not signed: no --key)")
    (out / "BestFare-Setup.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    say(f"Done in {int(time.time() - t0)} s: {final}  ({meta['size'] / 1024 / 1024:.1f} MB)  sha256 {digest[:16]}...")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", required=True, help="the server's address the app will use, e.g. https://api.example.com:8443")
    ap.add_argument("--version", required=True)
    ap.add_argument("--pin", default="")
    ap.add_argument("--notes", default="")
    ap.add_argument("--out", default=str(HERE / "dist"))
    ap.add_argument("--key", default=str(Path.home() / ".bestfare-keys" / "release_signing.pem"), help="the developer's release-signing key")
    ap.add_argument("--no-sign", action="store_true")
    ap.add_argument("--work", default=None)
    ap.add_argument("--reuse-app", action="store_true")
    ap.add_argument("--keep-work", action="store_true")
    a = ap.parse_args()
    try:
        build(a.server, a.version, Path(a.out), pin=a.pin, notes=a.notes, key=None if a.no_sign else Path(a.key), work=Path(a.work) if a.work else None,
              reuse_app=a.reuse_app, keep_work=a.keep_work or a.reuse_app)
    except (BuildError, rt.ReleaseError) as e:
        print("BUILD FAILED:", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
