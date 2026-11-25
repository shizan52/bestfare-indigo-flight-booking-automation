"""The 'Installed apps' entry and the shortcuts belong to ONE installation: trying or removing another copy must never touch them (and the tests never touch the real account)."""
import os
import uuid
import zipfile
import json
from pathlib import Path

import pytest

import setup_core as core

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the registry is Windows only")


def real_record():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, core.APP_KEY) as k:
            return winreg.QueryValueEx(k, "InstallDir")[0], winreg.QueryValueEx(k, "Version")[0]
    except OSError:
        return None


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    hive = r"Software\BestFareTest-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("BESTFARE_TEST_HIVE", hive)
    monkeypatch.setenv("BESTFARE_SHORTCUT_DIR", str(tmp_path / "links"))
    monkeypatch.setenv("BESTFARE_HOME", str(tmp_path / "data"))
    yield tmp_path
    import winreg
    for key in (hive + chr(92) + "Uninstall", hive + chr(92) + "App", hive):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


def program(path: Path):
    path.mkdir(parents=True)
    (path / core.EXE).write_bytes(b"MZ" + b"x" * 100)


def links(root: Path):
    out = []
    for p in core.shortcut_paths().values():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"link")
        out.append(p)
    return out


def test_the_tests_use_a_made_up_registry_key_and_made_up_shortcut_folders(sandbox):
    before = real_record()
    program(sandbox / "A")
    core.register(sandbox / "A", "9.9.9", 1000)
    assert core.installed() == {"dir": sandbox / "A", "version": "9.9.9"}
    assert real_record() == before                                                      # the real Windows account was not touched
    assert str(sandbox / "links") in str(core.shortcut_paths()["desktop"]) and str(sandbox / "links") in str(core.shortcut_paths()["startmenu"])


def test_removing_another_copy_leaves_the_registered_installation_its_entry_and_its_shortcuts(sandbox):
    program(sandbox / "Mine")
    program(sandbox / "Other")
    core.register(sandbox / "Mine", "1.0.1", 1000)
    made = links(sandbox)
    core.uninstall(sandbox / "Other")
    assert not (sandbox / "Other").exists()
    assert core.installed() is not None and core.installed()["dir"] == sandbox / "Mine"
    assert all(p.is_file() for p in made)


def test_removing_the_registered_installation_removes_its_entry_and_its_shortcuts(sandbox):
    program(sandbox / "Mine")
    core.register(sandbox / "Mine", "1.0.1", 1000)
    made = links(sandbox)
    assert core.owns_registration(sandbox / "Mine") and not core.owns_registration(sandbox / "Other")
    core.uninstall(sandbox / "Mine")
    assert core.installed() is None and not any(p.exists() for p in made) and not (sandbox / "Mine").exists()
