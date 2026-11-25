"""What the installer does to a computer: unpack safely, swap without ever leaving half a program, and remove cleanly."""
import json
import os
import zipfile
from pathlib import Path

import pytest

import setup_core as core


def payload(tmp_path, version="1.1.0", extra=None, exe=True, name="payload.zip"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        if exe:
            z.writestr("BestFare.exe", b"MZ" + b"x" * 5000)
        z.writestr("_internal/lib/a.dll", b"a" * 3000)
        for k, v in (extra or {}).items():
            z.writestr(k, v)
        z.writestr(core.MANIFEST, json.dumps({"version": version, "size": 8002, "files": 2, "notes": "n"}))
    return p


@pytest.fixture(autouse=True)
def no_windows_side_effects(monkeypatch, tmp_path):
    """Registry and shortcuts are the one part that touches the real Windows account: here they are recorded, not done."""
    log = {"registered": [], "shortcuts": [], "refreshed": []}
    monkeypatch.setattr(core, "register", lambda target, version, size: log["registered"].append((str(target), version)))
    monkeypatch.setattr(core, "make_shortcuts", lambda target, d, s: log["shortcuts"].append((d, s)) or [])
    monkeypatch.setattr(core, "refresh_shortcuts", lambda target: log["refreshed"].append(str(target)))
    monkeypatch.setattr(core, "NEED_FREE", 1)
    return log


def test_version_numbers_compare_like_the_server_does():
    assert core.version_number("1.2.3") == 1002003 and core.version_number("1.10.0") > core.version_number("1.9.9") and core.version_number("x") == 0


def test_a_payload_is_read_and_a_damaged_one_is_refused_with_plain_words(tmp_path):
    assert core.read_manifest(payload(tmp_path))["version"] == "1.1.0"
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(core.SetupError, match="damaged"):
        core.read_manifest(junk)
    with pytest.raises(core.SetupError):
        core.read_manifest(tmp_path / "missing.zip")


def test_install_unpacks_registers_and_leaves_no_temporary_folders(tmp_path, no_windows_side_effects):
    target = tmp_path / "Best Fare"
    seen = []
    out = core.install(payload(tmp_path), target, progress=lambda w, d, t: seen.append(w))
    assert (target / "BestFare.exe").is_file() and (target / "_internal" / "lib" / "a.dll").read_bytes() == b"a" * 3000
    assert not (tmp_path / "Best Fare.new").exists() and not (tmp_path / "Best Fare.old").exists()
    assert out["version"] == "1.1.0" and no_windows_side_effects["registered"] == [(str(target), "1.1.0")] and no_windows_side_effects["shortcuts"] == [(True, True)]
    assert seen[0] == "Preparing" and seen[-1] == "Done"


def test_an_update_replaces_the_folder_as_a_whole_and_keeps_shortcuts_without_asking(tmp_path, no_windows_side_effects):
    target = tmp_path / "Best Fare"
    core.install(payload(tmp_path, "1.0.0", {"old-only.txt": b"gone after the update"}, name="a.zip"), target)
    assert (target / "old-only.txt").exists()
    core.install(payload(tmp_path, "1.1.0", name="b.zip"), target, update=True)
    assert not (target / "old-only.txt").exists() and (target / "BestFare.exe").is_file()          # nothing of the old version is mixed into the new
    assert no_windows_side_effects["refreshed"] == [str(target)] and no_windows_side_effects["registered"][-1][1] == "1.1.0"
    assert not (tmp_path / "Best Fare.old").exists()


def test_when_the_new_folder_cannot_take_its_place_the_old_program_is_put_back(tmp_path, monkeypatch):
    target = tmp_path / "Best Fare"
    core.install(payload(tmp_path, "1.0.0", {"keep.txt": b"v1"}, name="a.zip"), target)
    real = core._rename

    def flaky(src, dst, seconds=30.0):
        if Path(src).name.endswith(".new"):
            raise core.SetupError("locked")
        return real(src, dst, seconds)
    monkeypatch.setattr(core, "_rename", flaky)
    with pytest.raises(core.SetupError):
        core.install(payload(tmp_path, "1.1.0", name="b.zip"), target, update=True)
    assert (target / "keep.txt").read_bytes() == b"v1" and (target / "BestFare.exe").is_file()      # the old version is whole and in its place
    assert not (tmp_path / "Best Fare.new").exists() and not (tmp_path / "Best Fare.old").exists()


@pytest.mark.parametrize("evil", ["../escaped.txt", "..\\escaped.txt", "/abs.txt", "C:/win.txt", "a/../../escaped.txt"])
def test_a_payload_that_tries_to_write_outside_its_folder_is_refused(tmp_path, evil):
    bad = payload(tmp_path, extra={evil: b"x"})
    with pytest.raises(core.SetupError, match="damaged"):
        core.install(bad, tmp_path / "Best Fare")
    assert not (tmp_path / "escaped.txt").exists() and not (tmp_path / "Best Fare").exists() or not any((tmp_path / "Best Fare").glob("escaped*"))
    assert not (tmp_path / "Best Fare.new").exists()


def test_a_payload_without_the_program_is_refused(tmp_path):
    with pytest.raises(core.SetupError, match="incomplete"):
        core.install(payload(tmp_path, exe=False), tmp_path / "Best Fare")


def test_the_install_folder_is_checked_with_a_reason_for_every_no(tmp_path, monkeypatch):
    with pytest.raises(core.SetupError, match="full folder"):
        core.check_target(Path("relative/dir"))
    with pytest.raises(core.SetupError, match="whole drive"):
        core.check_target(Path(tmp_path.anchor))
    with pytest.raises(core.SetupError, match="Windows folder"):
        core.check_target(Path(os.environ.get("WINDIR", r"C:\Windows")) / "BestFare")
    pf = os.environ.get("ProgramFiles")
    if pf:
        with pytest.raises(core.SetupError, match="own Windows account"):
            core.check_target(Path(pf) / "Best Fare")
    full = tmp_path / "full"
    full.mkdir()
    (full / "somebody-elses.txt").write_text("x")
    with pytest.raises(core.SetupError, match="other files"):
        core.check_target(full)
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(core.SetupError, match="file, not a folder"):
        core.check_target(f)
    monkeypatch.setattr(core.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    monkeypatch.setattr(core, "NEED_FREE", 10 ** 9)
    with pytest.raises(core.SetupError, match="free space"):
        core.check_target(tmp_path / "new")
    monkeypatch.undo()
    core.check_target(tmp_path / "fine" / "Best Fare", need=1)                                   # a new folder, even a few levels deep, is fine


def test_a_running_program_is_noticed_and_not_replaced(tmp_path, monkeypatch):
    target = tmp_path / "Best Fare"
    core.install(payload(tmp_path, "1.0.0", name="a.zip"), target)
    monkeypatch.setattr(core, "app_running", lambda t: True)
    with pytest.raises(core.SetupError, match="open"):
        core.install(payload(tmp_path, "1.1.0", name="b.zip"), target, update=True)
    assert (target / "BestFare.exe").is_file()


def test_uninstall_removes_the_program_and_keeps_the_users_data_unless_asked(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "vault.bin").write_bytes(b"sealed")
    monkeypatch.setenv("BESTFARE_HOME", str(home))
    monkeypatch.setattr(core, "unregister", lambda: None)
    monkeypatch.setattr(core, "shortcut_paths", lambda: {"desktop": None, "startmenu": None})
    target = tmp_path / "Best Fare"
    core.install(payload(tmp_path), target)
    core.uninstall(target)
    assert not target.exists() and (home / "vault.bin").is_file()
    core.install(payload(tmp_path), target)
    core.uninstall(target, purge_data=True)
    assert not target.exists() and not home.exists()


def test_uninstall_from_inside_the_folder_hands_the_deleting_to_a_helper_that_cannot_be_fooled_by_odd_paths(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(core.subprocess, "Popen", lambda args, **kw: started.append(args))
    monkeypatch.setattr(core, "unregister", lambda: None)
    monkeypatch.setattr(core, "shortcut_paths", lambda: {"desktop": None, "startmenu": None})
    target = tmp_path / "Best Fare"
    core.install(payload(tmp_path), target)
    core.uninstall(target, running_from_target=True)
    assert target.exists()                                                                       # (still there: the helper removes it a moment after this process ends)
    assert started and started[0][0] == "cmd.exe" and started[0][2] == "call"
    script = Path(started[0][3])
    text = script.read_text(encoding="mbcs")
    script.unlink()
    assert f'rmdir /s /q "{target}"' in text and "goto again" in text and text.rstrip().endswith('del "%~f0"')
