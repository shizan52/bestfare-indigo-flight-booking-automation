"""Updates, end to end with a REAL server: publish -> every app gets it, opened in memory only; and everything an attacker could try."""
import hashlib
import sys
import zipfile
import io
from pathlib import Path

import pytest

import bundle
import packed
import licence as lic
import updater
from cloud import CloudClient, CloudError
from device import DeviceKey, Vault
from updater import Releases, UpdateError, Updater

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import release_tools as rt  # noqa: E402
from livecloud import scrypto  # noqa: E402


@pytest.fixture()
def dev_key(tmp_path):
    s = scrypto.Signer.generate()
    p = tmp_path / "release.pem"
    p.write_bytes(s.private_pem())
    return s, p


@pytest.fixture()
def env(live, dev_key, tmp_path):
    """A server that trusts the developer's key, an activated app, and an updater that trusts the same key."""
    signer, key_path = dev_key
    live.cloud.release_keys.append(signer.public_b64)
    k = live.cloud.create_licence("Rahul", credits=3, actor="boss")
    vault = Vault(tmp_path / "v.bin")
    L = lic.Licence(CloudClient(DeviceKey(vault), 0, base_url=live.url, server_public=live.public), vault, "t")
    L.activate(k["key"])
    up = Updater(L.client, Releases(tmp_path / "releases"), trusted=[signer.public_b64])

    class E:
        pass
    e = E()
    e.live, e.L, e.up, e.signer, e.key_path, e.k = live, L, up, signer, key_path, k

    def publish(build, files=None, live_now=True, **kw):
        files = files or {"hello.py": f"BUILD = {build}\n".encode(), "data.txt": b"resource"}
        data, key, _m = bundle.build(files, build_no=build, version=f"1.0.{build}", channel="stable", notes="n", signing_key=signer._k)
        live.cloud.add_release(data, scrypto.b64e(key))
        if live_now:
            live.cloud.edit_release(build, state="live", **kw)
        return data
    e.publish = publish
    return e


def info(e):
    return e.L.refresh() and e.L.update


# ── the happy path ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_published_update_reaches_the_app_and_runs_from_memory_only(env, tmp_path):
    env.publish(1, {"hello.py": b"def greet():\n    return 'hello from release 1'\n", "stations.json": b"{}"})
    env.L.refresh()
    assert env.L.update["available"] and env.L.update["build"] == 1
    manifest, files = env.up.prepare(env.L.update)
    assert manifest["build"] == 1 and set(files) == {"hello.py", "stations.json"}
    packed.install(files)
    try:
        import importlib
        mod = importlib.import_module("hello")
        assert mod.greet() == "hello from release 1" and mod.__file__.startswith("<bestfare:")      # no file on disk behind it
        assert packed.RESOURCES["stations.json"] == b"{}"
    finally:
        packed.uninstall()
        sys.modules.pop("hello", None)
    stored = [p.read_bytes() for p in (tmp_path / "releases").glob("*.bfr")]
    assert len(stored) == 1 and b"hello from release 1" not in stored[0]                         # what is kept on disk is encrypted
    assert not list(tmp_path.rglob("hello.py"))                                                 # and no source file was ever written


def test_a_newer_release_replaces_the_old_one_and_only_two_are_kept(env, tmp_path):
    for b in (1, 2, 3):
        env.publish(b)
        env.L.refresh()
        env.up.prepare(env.L.update)
        env.up.boot_ok(b)
    st = env.up.releases.state()
    assert st["current"] == 3 and st["previous"] == 2
    assert sorted(p.name for p in (tmp_path / "releases").glob("*.bfr")) == ["2.bfr", "3.bfr"]


def test_the_server_can_hold_back_a_release_for_part_of_the_users_and_a_withdrawn_one_stops(env):
    env.publish(1)
    env.publish(2, rollout=1)                                                  # 1 % of computers
    env.L.refresh()
    assert env.L.update["build"] == 1                                          # this computer is (almost certainly) not in the first 1 %
    env.live.cloud.edit_release(2, state="live", rollout=100)
    env.L.refresh()
    assert env.L.update["build"] == 2
    env.live.cloud.edit_release(2, state="withdrawn")
    env.L.refresh()
    assert env.L.update["build"] == 1


# ── an attacker, one trick at a time ────────────────────────────────────────────────────────────────────────────────
def test_a_bundle_not_signed_by_the_developer_is_refused_even_if_the_server_serves_it(env, live_unsafe=None):
    env.publish(1)
    evil, ekey, _ = bundle.build({"hello.py": b"import os; os.system('calc')\n"}, build_no=2, version="9", channel="stable", notes="",
                                 signing_key=scrypto.Signer.generate()._k)
    env.live.cloud.release_keys.append(scrypto.Signer.generate().public_b64)        # (a server that was tricked into trusting nothing useful)
    env.live.cloud.release_keys[:] = []                                               # the server's own check is bypassed on purpose: a hacked server
    env.live.cloud.release_keys.append(bundle._b64e(b"x" * 32))
    # a hacked server places the file directly and marks it live
    (env.live.cloud.dir / "releases" / "2.bfr").write_bytes(evil)
    m = {"format": 1, "build": 2, "version": "9", "channel": "stable", "notes": "", "created": 1, "payload": {}}
    env.live.cloud.db.run("INSERT INTO releases (build, version, channel, notes, state, file_name, sha256, size, key_enc, manifest, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (2, "9", "stable", "", "live", "2.bfr", hashlib.sha256(evil).hexdigest(), len(evil),
                           __import__("bfcloud.crypto", fromlist=["x"]).seal(env.live.cloud._seal, ekey, b"release-key/2"), "{}", 1.0))
    env.L.refresh()
    assert env.L.update["build"] == 2
    with pytest.raises(UpdateError) as e:
        env.up.stage(env.L.update)
    assert "could not be verified" in str(e.value)
    assert not env.up.releases.file(2).exists()


def test_a_bundle_whose_content_was_changed_is_refused(env):
    data = env.publish(1)
    z = zipfile.ZipFile(io.BytesIO(data))
    parts = {n: z.read(n) for n in z.namelist()}
    parts["payload.bin"] = parts["payload.bin"][:-1] + bytes([parts["payload.bin"][-1] ^ 1])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zz:
        for n, d in parts.items():
            zz.writestr(n, d)
    with pytest.raises(UpdateError):
        env.up.verify(buf.getvalue(), 1)
    bad = {**parts, "manifest.json": parts["manifest.json"].replace(b'"build":1', b'"build":9')}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zz:
        for n, d in bad.items():
            zz.writestr(n, d)
    with pytest.raises(UpdateError):
        env.up.verify(buf.getvalue(), 1)


def test_an_old_release_can_never_be_pushed_back_over_a_newer_one(env):
    env.publish(1)
    env.publish(2)
    env.L.refresh()
    env.up.prepare(env.L.update)
    env.up.boot_ok(2)
    with pytest.raises(UpdateError) as e:
        env.up.stage({"build": 1, "size": 10})
    assert "not newer" in str(e.value)
    assert env.up.releases.state()["current"] == 2


def test_a_release_that_crashed_at_start_is_dropped_and_the_previous_one_runs(env):
    env.publish(1)
    env.L.refresh()
    env.up.prepare(env.L.update)
    env.up.boot_ok(1)                                                          # build 1 works
    env.publish(2)
    env.L.refresh()
    m, _f = env.up.prepare(env.L.update)
    assert m["build"] == 2                                                     # build 2 starts ... and the app dies before showing its window
    # next start
    env.L.refresh()
    m, _f = env.up.prepare(env.L.update)
    assert m["build"] == 1 and 2 in env.up.releases.state()["bad"]
    env.L.refresh()
    m, _f = env.up.prepare(env.L.update)                                       # the bad one is not tried again ... and 1, which has worked before,
    assert m["build"] == 1                                                     # is not blamed for a one-off crash either


def test_a_stored_bundle_that_was_edited_on_disk_is_thrown_away_and_fetched_again(env):
    env.publish(1)
    env.L.refresh()
    env.up.prepare(env.L.update)
    env.up.boot_ok(1)
    f = env.up.releases.file(1)
    f.write_bytes(f.read_bytes()[:-20] + b"X" * 20)                            # somebody edits the stored file (or a disk goes wrong)
    env.up.releases.save({**env.up.releases.state(), "booting": None})
    env.L.refresh()
    m, files = env.up.prepare({"available": False})
    assert m["build"] == 1 and files["hello.py"] == b"BUILD = 1\n"                # a fresh, verified copy: the damage is not a dead end
    assert 1 not in env.up.releases.state()["bad"] and env.up.releases.file(1).read_bytes() != f.read_bytes()[:-20] + b"X" * 20


def test_a_damaged_file_that_cannot_be_replaced_ends_the_release_for_good(env):
    env.publish(1)
    env.L.refresh()
    env.up.prepare(env.L.update)
    env.up.boot_ok(1)
    f = env.up.releases.file(1)
    f.write_bytes(f.read_bytes()[:-20] + b"X" * 20)
    env.up.releases.save({**env.up.releases.state(), "booting": None})
    env.live.cloud.edit_release(1, state="withdrawn")                           # ... and the server no longer offers it
    with pytest.raises(UpdateError):
        env.up.prepare({"available": False})
    assert not f.exists() and 1 in env.up.releases.state()["bad"]


def test_a_damaged_file_with_no_connection_is_not_blamed_on_the_release(env, monkeypatch):
    from cloud import Offline
    env.publish(1)
    env.L.refresh()
    env.up.prepare(env.L.update)
    env.up.boot_ok(1)
    f = env.up.releases.file(1)
    f.write_bytes(f.read_bytes()[:-20] + b"X" * 20)
    env.up.releases.save({**env.up.releases.state(), "booting": None})
    monkeypatch.setattr(env.L.client, "download", lambda b: (_ for _ in ()).throw(Offline()))
    with pytest.raises(updater.TryAgain):
        env.up.prepare({"available": False})
    st = env.up.releases.state()
    assert 1 not in st["bad"] and st["current"] == 1                           # once the internet is back it simply works
    monkeypatch.undo()
    m, _files = env.up.prepare({"available": False})
    assert m["build"] == 1


def test_the_key_of_a_release_is_given_to_this_computer_only(env, tmp_path, live):
    env.publish(1)
    env.L.refresh()
    env.up.stage(env.L.update)
    k2 = live.cloud.create_licence("Other", credits=1, actor="boss")
    v2 = Vault(tmp_path / "v2.bin")
    L2 = lic.Licence(CloudClient(DeviceKey(v2), 0, base_url=live.url, server_public=live.public), v2, "t")
    L2.activate(k2["key"])
    thief = Updater(L2.client, Releases(tmp_path / "r2"), trusted=env.up.trusted)
    (tmp_path / "r2" / "1.bfr").write_bytes(env.up.releases.file(1).read_bytes())         # the same bundle on another computer ...
    # ... opens with the key the server gives THAT computer (it works for it), but the key meant for the first one is useless there:
    from cloud import new_ephemeral, unwrap
    priv, epk = new_ephemeral()
    wrapped = env.L.client.call("/api/v1/update/key", {"build": 1, "epk": epk})
    with pytest.raises(CloudError):
        unwrap(priv, wrapped, f"release/1/{L2.client.device_id}".encode())              # (wrapped for device 1: another device's context fails)
    assert unwrap(priv, wrapped, f"release/1/{env.L.client.device_id}".encode())


def test_a_blocked_user_gets_no_key_and_so_cannot_open_the_program(env):
    env.publish(1)
    env.L.refresh()
    env.up.stage(env.L.update)
    env.live.cloud.set_status(env.k["id"], "blocked", "no", "boss")
    with pytest.raises(CloudError) as e:
        env.up.open(1)
    assert e.value.code in ("unauthorized", "blocked")


def test_a_download_changed_on_the_way_is_refused(env, monkeypatch):
    env.publish(1)
    env.L.refresh()
    real = env.L.client.download
    monkeypatch.setattr(env.L.client, "download", lambda b: real(b)[:-3] + b"XYZ")
    with pytest.raises(UpdateError):
        env.up.stage(env.L.update)
    assert not env.up.releases.file(1).exists()


def test_nonsense_update_information_is_refused(env):
    for bad in ({"build": 0}, {"build": -1}, {"build": "2"}, {"build": True}, {"build": None}, {}):
        with pytest.raises(UpdateError):
            env.up.stage(bad)


def test_a_zip_with_dangerous_file_names_is_refused(dev_key):
    signer, _ = dev_key
    for name in ("../evil.py", "/abs.py", "a/../../b.py", "C:/x.py", "..\\x.py", "x" * 200 + ".py", "", "a b.py", ".hidden"):
        with pytest.raises(bundle.BundleError):
            bundle.build({name: b"x"}, build_no=1, version="1", channel="stable", notes="", signing_key=signer._k)
    data, key, m = bundle.build({"ok.py": b"x"}, build_no=1, version="1", channel="stable", notes="", signing_key=signer._k)
    manifest, payload = bundle.read_bundle(data, [signer.public_b64])
    assert bundle.decrypt_files(manifest, payload, key) == {"ok.py": b"x"}
    with pytest.raises(bundle.BundleError):
        bundle.decrypt_files(manifest, payload, b"k" * 32)                                  # the wrong key


# ── what the build tool puts in a release ───────────────────────────────────────────────────────────────────────────
def test_a_release_built_from_the_workspace_has_no_secret_no_shell_and_no_tests(dev_key):
    signer, path = dev_key
    data, key, manifest = rt.build_release(7, "9.9.9", "test", path)
    m, payload = bundle.read_bundle(data, [signer.public_b64])
    files = bundle.decrypt_files(m, payload, key)
    assert m["build"] == 7 and files["buildinfo.py"] == b'BUILD = 7\nVERSION = "9.9.9"\n'
    assert not set(files) & set(rt.SHELL_FILES) and not any(n.startswith(("tests", "tools")) for n in files)
    assert {"app.py", "engine.py", "indigo_client.py", "psp_flow.py", "ext/core.js", "stations.json", "gate.py", "speed.py"} <= set(files)
    blob = b"".join(files.values())
    assert b"6ETST001" not in blob                                                              # the Defence offer's code is nowhere in the program
    assert b"PRIVATE KEY" not in blob and b"BF-" not in b"".join(d for n, d in files.items() if n.endswith(".py") and n != "gate.py")
    core = files["ext/core.js"]
    assert core != (Path(__file__).resolve().parents[1] / "ext" / "core.js").read_bytes() and core.replace(b"OFFER-1-CODE", b"6ETST001") == (Path(__file__).resolve().parents[1] / "ext" / "core.js").read_bytes()


def test_the_build_tool_refuses_to_ship_a_secret(tmp_path, monkeypatch, dev_key):
    _s, path = dev_key
    real = rt.collect

    def dirty():
        f = real()
        f["engine.py"] += b"\nCODE = '6ETST001'\n"
        return f
    monkeypatch.setattr(rt, "collect", dirty)
    with pytest.raises(rt.ReleaseError):
        rt.build_release(1, "1", "x", path)


def test_the_shell_and_the_release_do_not_overlap_and_the_bundle_module_is_the_servers_copy():
    here = Path(__file__).resolve().parents[1]
    assert not set(rt.SHELL_FILES) & set(rt.APP_MODULES) and not set(rt.SHELL_FILES) & set(rt.ENGINE_MODULES)
    assert (here / "bundle.py").read_bytes() == (here.parent / "bestfare-cloud" / "bfcloud" / "bundle.py").read_bytes()    # one format, three users
    for name in rt.SHELL_FILES:
        assert (here / name).exists(), name
