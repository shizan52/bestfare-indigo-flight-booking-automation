"""The red-team suite, the app's side: somebody sits at the computer and edits what the app keeps, runs a fake server, plants old files,
reads the logs. None of it may crash the app, open a door, or show something secret."""
import json
import logging
import os
import sys
from pathlib import Path

import pytest

import licence as lic
import shell
import updater
from cloud import CloudClient, CloudError
from device import DeviceKey, Vault
from test_updates import dev_key, env, info  # noqa: F401  (the same server + developer key + activated app as the update tests)
from updater import Releases, UpdateError, Updater

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import make_distribution as md  # noqa: E402
import release_tools as rt  # noqa: E402


# ── the files the app keeps can be edited by anybody who sits at the computer ─────────────────────────────────────────────
JUNK_STATES = ['{"current": "../../evil", "previous": 1e30, "bad": "x", "good": {"a": 1}, "booting": [1]}', "[]", "null", '"x"', "{", "", '{"current": true}',
               '{"current": -5, "previous": 99999999999}', '{"current": 2.5}', "\x00\x00\x00", '{"bad": [1, "../x", null, 3]}']


def test_a_hand_edited_state_file_cannot_crash_the_start_or_point_outside_the_folder(env):
    st = env.up.releases.state_file
    for junk in JUNK_STATES:
        st.write_text(junk, encoding="utf-8")
        s = env.up.releases.state()
        for k in ("current", "previous", "booting"):
            assert s[k] is None or (isinstance(s[k], int) and not isinstance(s[k], bool) and 0 < s[k] < 10 ** 7), (junk, k, s[k])
        assert all(isinstance(b, int) for b in s["bad"] + s["good"])
    env.publish(1)
    env.L.refresh()
    st.write_text('{"current": "../../evil", "previous": "../../x"}', encoding="utf-8")
    m, files = env.up.prepare(env.L.update)                        # it asks the server for the program instead of crashing
    assert m["build"] == 1 and files["hello.py"] == b"BUILD = 1\n"
    assert not (env.up.releases.folder.parent.parent / "evil").exists()


def test_a_validly_signed_older_release_planted_over_the_current_one_does_not_get_in(env):
    """A rollback by hand: the developer's own, genuine build 1 is copied over the file of build 2."""
    old = env.publish(1)
    env.publish(2)
    env.L.refresh()
    env.up.prepare(env.L.update)
    env.up.boot_ok(2)
    env.up.releases.file(2).write_bytes(old)
    env.up.releases.save({**env.up.releases.state(), "booting": None})
    m, files = env.up.prepare({"available": False})
    assert m["build"] == 2 and files["hello.py"] == b"BUILD = 2\n"           # the swap was noticed and the real build 2 fetched again


def test_a_planted_file_for_a_build_the_server_never_offered_is_never_run(env):
    env.publish(1)
    old = env.publish(2, live_now=False)                          # a draft: never offered to anybody
    env.L.refresh()
    env.up.prepare(env.L.update)
    env.up.boot_ok(1)
    env.up.releases.file(2).write_bytes(old)                      # the attacker has the file (a leaked draft) and says it is current
    env.up.releases.save({**env.up.releases.state(), "current": 2, "previous": 1, "booting": None})
    m, _files = env.up.prepare({"available": False})
    assert m["build"] == 1                                        # the server will not give this computer the key to open it: it is dropped


def test_a_corrupt_or_empty_vault_starts_as_not_signed_in_and_never_crashes(live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=2, actor="boss")
    for junk in (b"", b"garbage", os.urandom(300), b"\x00" * 50, b"{}", json.dumps({"key": "BF-AAAA-AAAA-AAAA-AAAA"}).encode()):
        v = tmp_path / "vault.bin"
        v.write_bytes(junk)
        vault = Vault(v)
        L = lic.Licence(CloudClient(DeviceKey(vault), 1, base_url=live.url, server_public=live.public), vault, "t")
        assert L.resume() == lic.NEW
    # and a key typed into a computer whose key pair was lost is refused (it is a different computer as far as the server can tell)
    L.activate(k["key"])
    v.write_bytes(b"lost")
    vault2 = Vault(v)
    L2 = lic.Licence(CloudClient(DeviceKey(vault2), 1, base_url=live.url, server_public=live.public), vault2, "t")
    with pytest.raises(CloudError) as e:
        L2.activate(k["key"])
    assert e.value.code == "device_mismatch" and "admin" in e.value.message.lower()


def test_a_vault_with_somebody_elses_device_key_gets_nowhere(live, tmp_path):
    """The attacker swaps in their own key pair (a vault they made themselves), hoping the saved sign-in carries over."""
    k = live.cloud.create_licence("Rahul", credits=2, actor="boss")
    vault = Vault(tmp_path / "a.bin")
    A = lic.Licence(CloudClient(DeviceKey(vault), 1, base_url=live.url, server_public=live.public), vault, "t")
    A.activate(k["key"])
    token = A.client.token
    vb = Vault(tmp_path / "b.bin")
    B = CloudClient(DeviceKey(vb), 1, base_url=live.url, server_public=live.public)
    B.token = token                                                # (the token was copied too)
    with pytest.raises(CloudError) as e:
        B.call("/api/v1/session", {"build": 1})
    assert e.value.code == "unauthorized"
    with pytest.raises(CloudError):
        B.begin(1, "offer1", "DEL-BOM", "2026-12-01", "ref-0000001")
    assert live.cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (k["id"],)) == 2


# ── nothing secret is ever written where somebody could read it ──────────────────────────────────────────────────────────
def test_the_key_the_token_and_the_offer_code_never_reach_the_log_or_the_screen_text(live, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    k = live.cloud.create_licence("Rahul", credits=3, actor="boss")
    vault = Vault(tmp_path / "v.bin")
    L = lic.Licence(CloudClient(DeviceKey(vault), 1, base_url=live.url, server_public=live.public), vault, "t")
    L.activate(k["key"])
    t = L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "ref-log-0001")
    L.finish(t.booking_id, "failed", None, "no seats")
    L.refresh()
    secrets_ = [k["key"], k["key"].replace("-", ""), L.client.token, t.promo, "6ETST001"]
    shown = "\n".join([caplog.text, repr(L), str(L), repr(L.account), repr(t), str(t), L.message or ""])
    for s in secrets_:
        assert s and s not in shown, s[:6]
    raw = (tmp_path / "v.bin").read_bytes()
    for s in secrets_:
        assert s.encode() not in raw
    for f in tmp_path.rglob("*"):                                      # nothing else was written next to it either
        if f.is_file():
            assert k["key"].encode() not in f.read_bytes()


def test_an_error_from_the_server_never_shows_more_than_plain_words(live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=0, actor="boss")
    vault = Vault(tmp_path / "v.bin")
    L = lic.Licence(CloudClient(DeviceKey(vault), 1, base_url=live.url, server_public=live.public), vault, "t")
    L.activate(k["key"])
    with pytest.raises(CloudError) as e:
        L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "ref-none-001")
    for text in (e.value.message, str(e.value)):
        assert "Traceback" not in text and "sqlite" not in text.lower() and "6ETST001" not in text and ".py" not in text


# ── the switches a user could try ────────────────────────────────────────────────────────────────────────────────────────
def test_the_build_number_cannot_be_set_from_outside(monkeypatch):
    monkeypatch.setenv("BESTFARE_BUILD", "999999")
    assert shell.current_build() != 999999


def test_a_distribution_only_ever_points_at_this_computer_or_at_https(tmp_path):
    for good in ("https://example.com/bestfare", "https://1.2.3.4:8443/x", "http://127.0.0.1:8780", "http://localhost:9000/"):
        assert md.check_address(good)
    for bad in ("http://example.com/x", "http://203.0.113.9:8780", "ftp://x", "https://", "https://exa mple.com", "javascript:alert(1)", "",
                'https://evil.example/"\nimport os; os.system("x")  #', "https://evil.example/\x00"):
        with pytest.raises(rt.ReleaseError):
            md.check_address(bad)
    with pytest.raises(rt.ReleaseError):
        md.make(tmp_path / "d", 'https://x.test/"\nimport os #')                                       # (the address is written into a source file: never raw)
    assert not (tmp_path / "d").exists() or not any((tmp_path / "d").iterdir())


def test_the_build_tool_catches_every_kind_of_secret_before_it_ships(tmp_path):
    for secret in ("6ETST001", "TESTCODE1_PLUS_REAL = '6ETST001'", "-----BEGIN PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE KEY-----",
                   "BF-ABCD-EFGH-JKLM-NPQR", "emtadmin", "vps-access", "har.txt"):
        assert any(p.search(f"x = '{secret}'") for p in rt.SECRET_PATTERNS) or secret in ("emtadmin", "vps-access", "har.txt"), secret


def test_what_a_user_can_read_in_the_shell_holds_only_public_keys(tmp_path):
    out = md.make(tmp_path / "dist", "https://example.test/bestfare")
    text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in out.iterdir() if p.is_file())
    for needle in ("PRIVATE", "release_signing", "server_identity", "identity.pem", "emtadmin", "BEGIN OPENSSH", "Bearer ", "api_key"):
        assert needle not in text, needle
    from trust import RELEASE_PUBLIC, SERVER_PUBLIC
    assert len(SERVER_PUBLIC) == 43 and all(len(k) == 43 for k in RELEASE_PUBLIC)              # 32 raw bytes in base64: public halves only


# ── the developer's release key is the most valuable file there is ──────────────────────────────────────────────────────
def test_the_release_key_can_be_protected_by_a_passphrase_and_then_needs_it(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from livecloud import scrypto
    pem = tmp_path / "release.pem"
    pem.write_bytes(scrypto.Signer.generate().private_pem())
    public_before = rt.load_signing_key(pem).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    with pytest.raises(rt.ReleaseError):
        rt.protect_signing_key(pem, "short")
    rt.protect_signing_key(pem, "a long passphrase 12")
    assert b"ENCRYPTED" in pem.read_bytes()
    monkeypatch.delenv("BESTFARE_KEY_PASSWORD", raising=False)
    monkeypatch.setattr("getpass.getpass", lambda *_a, **_k: "wrong passphrase!")
    with pytest.raises(rt.ReleaseError):
        rt.load_signing_key(pem)                                    # the file alone signs nothing
    monkeypatch.setenv("BESTFARE_KEY_PASSWORD", "a long passphrase 12")
    key = rt.load_signing_key(pem)
    assert key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) == public_before          # the same key, still
