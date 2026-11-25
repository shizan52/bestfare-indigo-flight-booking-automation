r"""The installer, end to end, with the REAL built files and a REAL server (nothing faked):

    python tools\e2e_installer.py --setup1 dist1\BestFare-Setup.exe [--setup2 dist2\BestFare-Setup.exe]

  1. a throw-away cloud server (the real code, the real identity, a temporary database) with an owner, a key with credits, a live release (build 1)
  2. the first setup is uploaded through the admin API (signature checked by the server) and made live; the public page and link are fetched like a visitor would
  3. the downloaded file is installed silently into a temporary folder: files, "Installed apps" entry, shortcuts
  4. the installed BestFare.exe checks itself (--selfcheck) and then really starts against the server (--smoke): signs in, fetches the program, opens its window
  5. (with --setup2) a newer setup is published: the installed app finds it, downloads it, verifies it, runs it, the program folder is swapped and the app comes back
  6. a newer RELEASE is published: the next start downloads it with the update window and runs it
  7. uninstall (silent): files, entry and shortcuts are gone, the user's data stays
Everything runs under a temporary BESTFARE_HOME, a made-up registry key and made-up shortcut folders (BESTFARE_TEST_HIVE / BESTFARE_SHORTCUT_DIR): it never touches
the REAL installation, Installed-apps entry or shortcuts of the person running it, and nothing outside the temporary folders is left behind.
(Builds made before this protection existed - installer 1.0.1 and older - ignore it and use the real registry: do not run this tool with those on a computer that has a real installation.)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE))
import release_tools as rt  # noqa: E402

CLOUD = HERE.parent / "bestfare-cloud"
HIVE = "Software" + chr(92) + "BestFareE2E-" + os.urandom(4).hex()          # (a made-up registry key for the setup and the app to use instead of the real one)
os.environ["BESTFARE_TEST_HIVE"] = HIVE
os.environ["BESTFARE_SHORTCUT_DIR"] = tempfile.mkdtemp(prefix="bf-e2e-links-")
PORT = 8780
BASE = f"http://127.0.0.1:{PORT}"
PW = "Correct-Horse-9-Battery"
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, text: str) -> bool:
    RESULTS.append((bool(ok), text))
    print(("  PASS  " if ok else "  FAIL  ") + text, flush=True)
    return bool(ok)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1024 * 1024), b""):
            h.update(c)
    return h.hexdigest()


def wait_until(fn, seconds: float, step: float = 1.0):
    end = time.time() + seconds
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        time.sleep(step)
    return None


def kill_installed(folder: Path) -> None:
    """Stop any BestFare.exe that runs from `folder` (the app that the update started again keeps running by design)."""
    script = ("Get-CimInstance Win32_Process -Filter \"name='BestFare.exe'\" | Where-Object { $_.ExecutablePath -like ($env:BF_DIR + '*') } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], env={**os.environ, "BF_DIR": str(folder)}, capture_output=True, timeout=60)


def registry_version() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, HIVE + chr(92) + "App") as k:
            return winreg.QueryValueEx(k, "Version")[0]
    except OSError:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup1", required=True)
    ap.add_argument("--setup2")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    setup1 = Path(a.setup1)
    meta1 = json.loads(setup1.with_suffix(".json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix="bf-e2e-"))
    home, data, inst = tmp / "home", tmp / "server", tmp / "Best Fare"
    env = {**os.environ, "BESTFARE_HOME": str(home), "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1", "BESTFARE_SERVER": BASE}      # (the builds may carry the production address: this points them at the throw-away server)
    server = None
    try:
        # 1 ── the server
        data.mkdir(parents=True)
        home.mkdir()
        shutil.copy2(CLOUD / "secrets" / "server_identity.pem", data / "identity.pem")
        py = HERE / ".venv" / "Scripts" / "python.exe"
        base_cmd = [str(py), "-m", "bfcloud", "--data", str(data)]
        subprocess.run([*base_cmd, "create-owner", "boss", "--password-stdin"], input=PW + "\n", text=True, cwd=str(CLOUD), capture_output=True, check=True)
        import trust
        subprocess.run([*base_cmd, "trust-key", trust.RELEASE_PUBLIC[0]], cwd=str(CLOUD), capture_output=True, check=True)
        server = subprocess.Popen([*base_cmd, "serve", "--port", str(PORT), "--public-url", BASE], cwd=str(CLOUD), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        up = wait_until(lambda: urllib.request.urlopen(BASE + "/install", timeout=3).status == 200, 30)
        check(up, "the throw-away server is up and shows its public install page")
        admin = rt.Admin(BASE, "boss", PW)
        key_path = Path.home() / ".bestfare-keys" / "release_signing.pem"
        made = admin._json("POST", "/admin/api/licences", {"label": "E2E user", "credits": 5, "offers": ["offer1", "offer2"]})
        key = made["key"]
        # release build 1
        data1, rkey, m = rt.build_release(1, "1.0.0", "first program", key_path)
        admin.upload(data1, rkey, "first program")
        admin.edit(1, state="live", mandatory=False, rollout=100)
        check(admin.releases()[0]["state"] == "live", "program release build 1 is live")

        # 2 ── the installer: upload, page, link
        page = urllib.request.urlopen(BASE + "/install", timeout=10).read().decode()
        check("Coming soon" in page, "before an installer is published the page says 'Coming soon' (nothing is given out)")
        out = admin.upload_installer(setup1, meta1["version"], meta1["sig"], "first setup")
        check(out.get("ok") and out["sha256"] == meta1["sha256"], f"the server accepted the signed installer {meta1['version']} ({out['size'] // 1024 // 1024} MB)")
        admin.edit_installer(out["id"], state="live")
        page = urllib.request.urlopen(BASE + "/install", timeout=10).read().decode()
        check("Download for Windows" in page and f"Version {meta1['version']}" in page and meta1["sha256"] in page, "the public page offers the download with its version and fingerprint")
        dl = tmp / "downloaded.exe"
        with urllib.request.urlopen(BASE + "/download/BestFare-Setup.exe", timeout=300) as r, open(dl, "wb") as f:
            shutil.copyfileobj(r, f)
        check(sha(dl) == meta1["sha256"], "the file from the link is exactly the signed file (SHA-256 equal)")
        info = admin._json("GET", "/admin/api/installers")
        check(info["link"] == BASE + "/install", "the panel shows the install link")

        # 3 ── silent install
        t0 = time.time()
        r = subprocess.run([str(dl), "/S", "--dir", str(inst), "--no-launch"], env=env, capture_output=True, text=True, timeout=600)
        check(r.returncode == 0 and (inst / "BestFare.exe").is_file(), f"silent install works ({time.time() - t0:.0f} s) -> {inst.name}\\BestFare.exe")
        check((inst / "_internal").is_dir() and not (tmp / "Best Fare.new").exists() and not (tmp / "Best Fare.old").exists(), "no half-installed leftovers (.new/.old)")
        check(registry_version() == meta1["version"], "'Installed apps' entry written with the version")
        import setup_core as core
        links = core.shortcut_paths()
        check(all(p is not None and p.is_file() for p in links.values()), "desktop and Start-menu shortcuts exist")
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, core.registry_keys()[0]) as k:
            unin = winreg.QueryValueEx(k, "UninstallString")[0]
        check("--uninstall" in unin and str(inst) in unin, "the uninstall command points at the installed program")

        # 4 ── the installed app
        rep = tmp / "selfcheck.json"
        r = subprocess.run([str(inst / "BestFare.exe"), "--selfcheck", "--out", str(rep)], env=env, capture_output=True, text=True, timeout=180)
        sc = json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else {}
        check(sc.get("ok") and sc.get("frozen") and sc.get("shell_version") == meta1["version"], f"installed app self-check: ok, frozen, version {sc.get('shell_version')}, {sc.get('checked')} modules")
        rep = tmp / "smoke1.json"
        r = subprocess.run([str(inst / "BestFare.exe"), "--smoke", "--key", key, "--out", str(rep)], env=env, capture_output=True, text=True, timeout=300)
        sm = json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else {}
        check(sm.get("ok") and sm.get("program_build") == 1, f"first start: signed in, program downloaded and opened from memory, window shown (program build {sm.get('program_build')}) {sm.get('error', '')}")
        check((home / "vault.bin").is_file() and (home / "releases").is_dir(), "user data is in the data folder, not in the program folder")

        # 5 ── the app updates itself from a newer setup
        if a.setup2:
            setup2 = Path(a.setup2)
            meta2 = json.loads(setup2.with_suffix(".json").read_text(encoding="utf-8"))
            out2 = admin.upload_installer(setup2, meta2["version"], meta2["sig"], "Faster start; saved contacts menu; payment step fix")
            admin.edit_installer(out2["id"], state="live")
            rep = tmp / "smoke2.json"
            r = subprocess.run([str(inst / "BestFare.exe"), "--smoke", "--key", key, "--out", str(rep)], env=env, capture_output=True, text=True, timeout=600)
            done = wait_until(lambda: registry_version() == meta2["version"], 180, 2)
            time.sleep(4)
            kill_installed(inst)
            check(done, f"the installed app downloaded the newer setup, verified it and updated itself to {registry_version()}")
            rep = tmp / "selfcheck2.json"
            subprocess.run([str(inst / "BestFare.exe"), "--selfcheck", "--out", str(rep)], env=env, capture_output=True, text=True, timeout=180)
            sc = json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else {}
            check(sc.get("ok") and sc.get("shell_version") == meta2["version"], f"after the update the program folder is the new version ({sc.get('shell_version')})")
            check((home / "vault.bin").is_file(), "the user's sign-in survived the update")
            check(not (tmp / "Best Fare.new").exists() and not (tmp / "Best Fare.old").exists(), "no leftovers after the update")

        # 6 ── a newer release (the program) shows the update window and runs
        data2, rkey2, m2 = rt.build_release(2, "1.0.1", "release two", key_path)
        admin.upload(data2, rkey2, "Fixes the payment step")
        admin.edit(2, state="live", mandatory=False, rollout=100)
        rep = tmp / "smoke3.json"
        r = subprocess.run([str(inst / "BestFare.exe"), "--smoke", "--out", str(rep)], env=env, capture_output=True, text=True, timeout=300)
        sm = json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else {}
        check(sm.get("ok") and sm.get("program_build") == 2, f"a newer release was downloaded at start and runs (program build {sm.get('program_build')}) {sm.get('error', '')}")

        # 7 ── uninstall
        r = subprocess.run([str(inst / "BestFare.exe"), "--uninstall", "--silent"], env=env, capture_output=True, text=True, timeout=120)
        gone = wait_until(lambda: not inst.exists(), 40, 1)
        check(r.returncode == 0 and gone, "uninstall removed the program folder")
        check(registry_version() == "" and not any(p is not None and p.exists() for p in core.shortcut_paths().values()), "uninstall removed the registry entry and the shortcuts")
        check((home / "vault.bin").is_file(), "uninstall kept the user's data (as promised)")
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(10)
            except subprocess.TimeoutExpired:
                server.kill()
        kill_installed(inst)
        try:
            import winreg
            for key in (HIVE + chr(92) + "Uninstall", HIVE + chr(92) + "App", HIVE):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
                except OSError:
                    pass
            shutil.rmtree(os.environ["BESTFARE_SHORTCUT_DIR"], ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        if not a.keep:
            time.sleep(1)
            shutil.rmtree(tmp, ignore_errors=True)
    bad = [t for ok, t in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)} passed, {len(bad)} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
