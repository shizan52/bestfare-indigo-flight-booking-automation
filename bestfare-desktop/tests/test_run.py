"""run.py is what the user starts: it must explain itself and the self-check must pass on a healthy computer."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OUTER = ROOT.parent / "run.py"            # the launcher next to the app folder (f:\indigo\run.py)


def run(*args, timeout=120):
    return subprocess.run([sys.executable, str(ROOT / "run.py"), *args], capture_output=True, text=True, timeout=timeout,
                          cwd=str(ROOT), encoding="utf-8", errors="replace")


def test_help_explains_every_option():
    out = run("--help").stdout
    for word in ("--demo", "--debug", "--check", "--tests", "--setup", "PRACTICE MODE", "NOTHING is sent to IndiGo"):
        assert word in out


def test_check_passes_on_this_computer():
    r = run("--check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Everything is ready." in r.stdout and "Microsoft Edge" in r.stdout and "PyQt6" in r.stdout


outer_only = pytest.mark.skipif(not OUTER.is_file(), reason="the launcher lives next to the app folder")


@outer_only
def test_the_launcher_outside_the_app_folder_passes_everything_on(tmp_path):
    for args, expect in ((("--help",), "--demo"), (("--check",), "Everything is ready.")):
        r = subprocess.run([sys.executable, str(OUTER), *args], capture_output=True, text=True, timeout=120,
                           cwd=str(tmp_path), encoding="utf-8", errors="replace")   # from ANY folder
        assert r.returncode == 0 and expect in r.stdout, r.stdout + r.stderr


@outer_only
def test_the_launcher_forwards_arguments_and_the_exit_code(monkeypatch):
    import importlib.util
    monkeypatch.setattr(sys, "dont_write_bytecode", True)                     # keep f:\indigo free of __pycache__
    spec = importlib.util.spec_from_file_location("outer_run", OUTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    def fake_call(cmd, cwd=None):
        seen["cmd"], seen["cwd"] = cmd, cwd
        return 7

    monkeypatch.setattr(mod.subprocess, "call", fake_call)
    assert mod.main(["--demo"]) == 7                                          # the app's exit code is the launcher's
    assert seen["cmd"][1:] == [str(ROOT / "run.py"), "--demo"] and seen["cwd"] == str(ROOT)


@outer_only
def test_the_launcher_explains_when_the_app_folder_is_missing(monkeypatch, capsys):
    import importlib.util
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("outer_run2", OUTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "TARGET", ROOT / "nope" / "run.py")
    assert mod.main([]) == 2 and "Cannot find" in capsys.readouterr().out
