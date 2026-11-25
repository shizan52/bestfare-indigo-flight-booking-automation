"""The real thing: a distribution folder (shell only), a real server, a published release - and the app started from nothing but the shell."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import bundle
import licence as lic
from cloud import CloudClient
from device import DeviceKey, Vault

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import make_distribution as md  # noqa: E402
import release_tools as rt  # noqa: E402
from livecloud import scrypto  # noqa: E402


def test_a_distribution_holds_the_shell_and_nothing_that_books(tmp_path):
    out = md.make(tmp_path / "dist", "https://example.test/bestfare", make_zip=False)
    names = {p.name for p in out.iterdir()}
    assert set(rt.SHELL_FILES) | {"requirements.txt", "run.bat", "README.txt"} == names
    assert not {"app.py", "engine.py", "indigo_client.py", "psp_flow.py", "main_window.py", "forms.py", "gate.py"} & names
    trust = (out / "trust.py").read_text()
    assert 'SERVER_URL = "https://example.test/bestfare"' in trust and "SERVER_PUBLIC" in trust
    everything = "".join(p.read_text(encoding="utf-8", errors="replace") for p in out.iterdir() if p.is_file())
    assert "6ETST001" not in everything and "PRIVATE KEY" not in everything and "Promo" not in everything
    z = md.make(tmp_path / "dist2", "https://example.test/bestfare", make_zip=True)
    assert z.suffix == ".zip" and z.stat().st_size < 400_000


def test_a_distribution_without_a_server_address_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(md.re, "subn", lambda *a, **k: ("x", 0))
    with pytest.raises(rt.ReleaseError):
        md.make(tmp_path / "d", "https://x")


@pytest.fixture()
def published(live, tmp_path):
    """The workspace built, signed and published as build 1 on a live server that trusts the developer's key."""
    signer = scrypto.Signer.generate()
    key_path = tmp_path / "release.pem"
    key_path.write_bytes(signer.private_pem())
    live.cloud.release_keys.append(signer.public_b64)
    data, key, manifest = rt.build_release(1, "1.2.0", "first", key_path)
    live.cloud.add_release(data, scrypto.b64e(key))
    live.cloud.edit_release(1, state="live")
    return signer


DRIVER = textwrap.dedent('''
    import os, sys, json
    from pathlib import Path
    sys.path.insert(0, os.environ["DIST"])
    import trust
    trust.RELEASE_PUBLIC[:] = [os.environ["RELEASE_PUB"]]            # (this test's own developer key)
    trust.SERVER_PUBLIC = os.environ["SERVER_PUB"]
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    real_exec = QApplication.exec
    def exec_then_quit(self=None):
        QTimer.singleShot(7000, QApplication.instance().quit)        # the window has been up for a few seconds: close it
        return real_exec()
    QApplication.exec = exec_then_quit
    import shell
    code = shell.run_packed(demo=True, debug=False)
    import packed
    Path(os.environ["OUT"]).write_text(json.dumps({"code": code, "active": packed.ACTIVE, "modules": sorted(m for m in sys.modules if getattr(sys.modules[m], "__file__", "") and str(getattr(sys.modules[m], "__file__", "")).startswith("<bestfare:"))}))
''')


def test_the_app_starts_from_nothing_but_the_shell_and_the_program_exists_only_in_memory(live, published, tmp_path):
    dist = md.make(tmp_path / "BestFare", live.url)
    home = tmp_path / "home"
    home.mkdir()
    k = live.cloud.create_licence("Rahul", credits=3, actor="boss")
    vault = Vault(home / "vault.bin")
    L = lic.Licence(CloudClient(DeviceKey(vault), 0, base_url=live.url, server_public=live.public), vault, "t")      # (the user typed their key)
    L.activate(k["key"])
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    out = tmp_path / "out.json"
    env = dict(os.environ, DIST=str(dist), BESTFARE_HOME=str(home), BESTFARE_SERVER=live.url, SERVER_PUB=live.public, RELEASE_PUB=published.public_b64,
               OUT=str(out), QT_QPA_PLATFORM="offscreen", PYTHONPATH="", PYTHONDONTWRITEBYTECODE="1")
    env.pop("BESTFARE_DEV_OFFERS", None)
    r = subprocess.run([sys.executable, str(driver)], cwd=str(dist), env=env, capture_output=True, text=True, timeout=150)
    assert out.exists(), r.stderr[-1500:] + r.stdout[-500:]
    res = json.loads(out.read_text())
    assert res["code"] == 0 and res["active"] is True
    assert {"app", "engine", "main_window", "indigo_client", "psp_flow", "gate", "forms"} <= set(res["modules"])         # the program ran from memory ...
    assert not list(dist.glob("*.py")) or {p.name for p in dist.glob("*.py")} <= set(rt.SHELL_FILES)                   # ... and left no source behind
    assert not list(tmp_path.rglob("engine.py")) and not list(tmp_path.rglob("psp_flow.py"))
    st = json.loads((home / "releases" / "state.json").read_text())
    assert st["current"] == 1 and 1 in st["good"] and st["booting"] is None                                             # it opened a window and was marked good
    stored = (home / "releases" / "1.bfr").read_bytes()
    assert b"def run_psp" not in stored and b"class Engine" not in stored                                             # what is on disk is encrypted
    assert live.cloud.db.scalar("SELECT COUNT(*) FROM events WHERE kind='release_key'") == 1                           # the server gave the key once, to this computer
