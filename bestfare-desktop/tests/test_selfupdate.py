"""The installed app updating ITSELF: a newer setup is offered by a real server, fetched, checked against the developer's signature, and only then run."""
import hashlib
import os

import pytest

import bundle
import selfupdate
import trust
from cloud import CloudError
from test_updates import dev_key, env  # noqa: F401  (fixtures: a live server, an activated app, a developer key)

SIZE = 1_300_000


def make_setup(tmp_path, signer, version, head=b"MZ", sign=True, size=SIZE):
    p = tmp_path / f"setup-{version}.exe"
    p.write_bytes(head + os.urandom(size - len(head)))
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    return p, digest, bundle.sign_installer(version, digest, signer._k)


def publish(e, tmp_path, version, **kw):
    p, digest, sig = make_setup(tmp_path, e.signer, version, **kw)
    out = e.live.cloud.add_installer(p, version, sig, "Faster start", "boss")
    e.live.cloud.edit_installer(out["id"], state="live", actor="boss")
    return digest, sig


@pytest.fixture(autouse=True)
def trusted(monkeypatch, env):
    monkeypatch.setattr(trust, "RELEASE_PUBLIC", [env.signer.public_b64])
    monkeypatch.setattr(trust, "SHELL_VERSION", "1.0.0")


def offer_of(e):
    e.L.refresh()
    return selfupdate.check(e.L.installer)


def test_nothing_is_offered_until_a_newer_setup_is_live_and_the_offer_carries_all_the_app_needs(env, tmp_path):
    assert offer_of(env) is None
    publish(env, tmp_path, "1.0.0")                                           # the same version: nothing to do
    assert offer_of(env) is None
    digest, _sig = publish(env, tmp_path, "1.1.0")
    o = offer_of(env)
    assert o and o["version"] == "1.1.0" and o["sha256"] == digest and o["size"] == SIZE and o["notes"] == "Faster start" and not o["mandatory"]


def test_the_setup_is_downloaded_with_progress_checked_and_kept_only_when_it_is_the_developers(env, tmp_path):
    publish(env, tmp_path, "1.1.0")
    o = offer_of(env)
    seen = []
    path = selfupdate.fetch(env.L.client, o, lambda d, t: seen.append((d, t)))
    assert path.is_file() and path.name == "BestFare-Setup-1.1.0.exe" and path.stat().st_size == SIZE
    assert seen and seen[-1][0] == SIZE and seen[-1][1] == SIZE and all(a <= b for (a, _), (b, _) in zip(seen, seen[1:]))
    assert selfupdate.staged(o) == path                                       # a second time it is not downloaded again
    again = []
    assert selfupdate.fetch(env.L.client, o, lambda d, t: again.append(d)) == path and again == [SIZE]
    assert not list(path.parent.glob("*.part"))


def test_a_setup_changed_on_the_server_or_on_the_way_is_refused_and_leaves_nothing(env, tmp_path):
    publish(env, tmp_path, "1.1.0")
    o = offer_of(env)
    served = env.live.cloud.installer_path(env.live.cloud.live_installer())
    data = bytearray(served.read_bytes())
    data[5000] ^= 0xFF                                                         # one flipped byte inside the file the server hands out
    served.write_bytes(bytes(data))
    with pytest.raises(selfupdate.SetupError):
        selfupdate.fetch(env.L.client, o)
    assert not list(selfupdate.folder().glob("BestFare-Setup-*"))


def test_a_setup_signed_by_somebody_else_is_never_run_even_with_the_right_hash(env, tmp_path, monkeypatch):
    publish(env, tmp_path, "1.1.0")
    o = offer_of(env)
    monkeypatch.setattr(trust, "RELEASE_PUBLIC", ["x" * 43])                  # the app trusts another developer: the server's file is not theirs
    with pytest.raises(selfupdate.SetupError):
        selfupdate.fetch(env.L.client, o)
    assert not list(selfupdate.folder().glob("BestFare-Setup-*"))


@pytest.mark.parametrize("change", [{"version": "0.9.0"}, {"version": "1.0.0"}, {"version": "x"}, {"size": 10}, {"size": True}, {"size": 10 ** 12}, {"sha256": "zz"},
                                   {"sig": 5}, {"path": "/download/../x.exe"}, {"path": "http://evil.example/x.exe"}, {"available": False}])
def test_an_offer_that_is_not_newer_or_not_well_formed_is_ignored(env, tmp_path, change):
    publish(env, tmp_path, "1.1.0")
    env.L.refresh()
    bad = {**env.L.installer, **change}
    assert selfupdate.check(bad) is None
    for junk in (None, [], "x", 5, {}):
        assert selfupdate.check(junk) is None


def test_the_app_tells_the_server_which_version_it_is(env, tmp_path):
    publish(env, tmp_path, "1.1.0")
    env.L.refresh()
    assert env.L.installer["available"]
    trust.SHELL_VERSION = "1.1.0"                                              # (the fixture restores it)
    env.L.refresh()
    assert env.L.installer == {"available": False}


def test_the_download_address_is_fixed_and_a_file_larger_than_announced_is_cut(env, tmp_path):
    publish(env, tmp_path, "1.1.0")
    for bad in ("/download/../x", "download/x", "/a//b", "/x?y=1", "/x y"):
        with pytest.raises(CloudError):
            env.L.client.fetch_file(bad, tmp_path / "x", limit=10)
    with pytest.raises(CloudError):
        env.L.client.fetch_file(selfupdate.SETUP_PATH, tmp_path / "big", limit=1000)               # the file is bigger than the limit the app accepts


def test_launch_runs_the_setup_in_update_mode_and_waits_for_this_app_to_close(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(selfupdate.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    setup = tmp_path / "BestFare-Setup-1.1.0.exe"
    setup.write_bytes(b"MZ")
    selfupdate.launch(setup)
    args, kw = calls[0]
    assert args[0] == str(setup) and args[1] == "--update" and args[2:] == ["--wait-pid", str(os.getpid())]
    assert kw["close_fds"] is True


def test_wait_for_exit_returns_at_once_for_a_process_that_is_gone_and_does_not_wait_for_itself():
    assert selfupdate.wait_for_exit(os.getpid(), 0.1) is True and selfupdate.wait_for_exit(0, 0.1) is True and selfupdate.wait_for_exit(2 ** 22 + 7, 0.5) is True
