"""The app's licence, against a REAL server over real HTTP: activation, one computer per key, credits, the booking gate, a fake server,
a wrong clock, no connection."""
import json
from pathlib import Path

import pytest

import cloud
import licence as lic
from cloud import CloudClient, CloudError, Offline
from device import DeviceKey, Vault


def make_licence(live, tmp_path, name="a", build=1, public=None, url=None):
    vault = Vault(tmp_path / f"vault-{name}.bin")
    client = CloudClient(DeviceKey(vault), build, base_url=url or live.url, server_public=public or live.public)
    return lic.Licence(client, vault, "test")


def test_a_key_activates_this_computer_and_the_credits_come_from_the_server(live, tmp_path):
    k = live.cloud.create_licence("Rahul Sharma", credits=7, actor="boss")
    L = make_licence(live, tmp_path)
    assert L.state == lic.NEW and L.resume() == lic.NEW
    L.activate(k["key"])
    assert L.state == lic.OK and L.usable and L.account.credits == 7 and L.account.label == "Rahul Sharma"
    assert set(L.account.offers) == {"offer1", "offer2"} and "code" not in json.dumps(L.account.offers)


def test_what_is_kept_on_disk_is_sealed_and_a_restart_signs_in_again_by_itself(live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=3, actor="boss")
    L = make_licence(live, tmp_path)
    L.activate(k["key"])
    raw = (tmp_path / "vault-a.bin").read_bytes()
    assert k["key"].encode() not in raw and b"BF-" not in raw and b"PRIVATE KEY" not in raw and b"token" not in raw      # nothing readable
    L2 = make_licence(live, tmp_path)                                  # "the app was closed and opened again"
    assert L2.resume() == lic.OK and L2.account.credits == 3


def test_a_vault_copied_to_another_computer_or_user_is_not_readable():
    """DPAPI seals the vault to this Windows user on this machine - a stolen copy is garbage (checked by tampering here)."""
    from device import protect, unprotect
    sealed = protect(b"secret")
    assert unprotect(sealed) == b"secret"
    if __import__("os").name == "nt":
        with pytest.raises(OSError):
            unprotect(sealed[:-5] + b"XXXXX")
        with pytest.raises(OSError):
            unprotect(sealed, entropy=b"another purpose")


def test_a_wrong_key_says_so_in_plain_words(live, tmp_path):
    L = make_licence(live, tmp_path)
    for bad in ("", "BF-AAAA-BBBB-CCCC-DDDD", "hello"):
        with pytest.raises(CloudError) as e:
            L.activate(bad)
        assert e.value.code == "bad_key" and "key" in e.value.message.lower()
    assert L.state == lic.NEW


def test_a_second_computer_cannot_use_the_same_key(live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=3, actor="boss")
    a, b = make_licence(live, tmp_path, "a"), make_licence(live, tmp_path, "b")
    a.activate(k["key"])
    with pytest.raises(CloudError) as e:
        b.activate(k["key"])
    assert e.value.code == "device_mismatch" and "another computer" in e.value.message
    assert a.refresh() == lic.OK                                        # the first computer is untouched
    ev = live.cloud.events(level="alert")
    assert ev and ev[0]["kind"] == "device_refused"


def test_blocking_stops_the_app_at_the_next_refresh_and_unblocking_restores_it(live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=3, actor="boss")
    L = make_licence(live, tmp_path)
    L.activate(k["key"])
    live.cloud.set_status(k["id"], "blocked", "shared the key", "boss")
    assert L.refresh() == lic.BLOCKED and "shared the key" in L.message and not L.usable
    with pytest.raises(CloudError):
        L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "r1")
    live.cloud.set_status(k["id"], "active", "", "boss")
    assert L.resume() == lic.OK and L.usable


def test_moving_the_key_to_a_new_computer_by_the_admin(live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=3, actor="boss")
    a, b = make_licence(live, tmp_path, "a"), make_licence(live, tmp_path, "b")
    a.activate(k["key"])
    live.cloud.reset_device(k["id"], "boss")
    assert a.refresh() == lic.NEW and not a.usable                      # the old computer stops AT ONCE and does not quietly take the key back ...
    assert "type your key again" in a.message
    b.activate(k["key"])                                                # ... so the new computer gets it, whenever the person gets to it
    assert b.usable and b.account.credits == 3
    assert a.resume() == lic.BLOCKED and "another computer" in a.message   # the old one is told the key now belongs to another computer
    with pytest.raises(CloudError) as e:                                # (and if somebody types it on the old one now, it is refused: it is the other computer's)
        a.activate(k["key"])
    assert e.value.code == "device_mismatch" and "another computer" in e.value.message


def test_a_booking_takes_credits_first_and_gives_the_promo_code_only_then(live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=5, actor="boss")
    L = make_licence(live, tmp_path)
    L.activate(k["key"])
    t = L.begin(2, "offer1", "DEL-BOM", "2026-12-01", "ref-1")
    assert t.promo == "6ETST001" and t.charged == 2 and t.credits == 3 and L.account.credits == 3
    again = L.begin(2, "offer1", "DEL-BOM", "2026-12-01", "ref-1")            # a retry after a lost answer: the same booking, no second charge
    assert again.booking_id == t.booking_id and L.account.credits == 3
    d = L.finish(t.booking_id, "failed", None, "IndiGo had no seats")
    assert d["refunded"] and L.account.credits == 5


def test_with_no_credits_left_nothing_can_be_booked_until_the_admin_adds_more(live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=1, actor="boss")
    L = make_licence(live, tmp_path)
    L.activate(k["key"])
    L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "a")
    with pytest.raises(CloudError) as e:
        L.begin(1, "offer1", "DEL-BOM", "2026-12-02", "b")
    assert e.value.code == "no_credits" and "credit" in e.value.message and e.value.extra["have"] == 0
    assert not L.can_afford(1)
    live.cloud.adjust_credits(k["id"], 5, "top up", "boss")
    assert L.refresh() == lic.OK and L.account.credits == 5 and L.can_afford(2)
    assert L.begin(2, "offer2", "DEL-BOM", "2026-12-02", "c").promo == "PROMO2"


def test_a_fake_server_is_recognised_and_refused(live, tmp_path):
    from livecloud import LiveServer
    fake = LiveServer(tmp_path / "fake")                                  # a different server with its own (untrusted) identity
    try:
        fake_key = fake.cloud.create_licence("Rahul", credits=999, actor="x")
        L = make_licence(live, tmp_path, url=fake.url)                    # points at the fake but trusts the REAL identity
        with pytest.raises(CloudError) as e:
            L.activate(fake_key["key"])
        assert e.value.code == "untrusted" and "not the real" in e.value.message.lower()
        assert L.state == lic.NEW
    finally:
        fake.stop()


def test_an_answer_changed_on_the_way_is_not_believed(live, tmp_path, monkeypatch):
    k = live.cloud.create_licence("Rahul", credits=3, actor="boss")
    L = make_licence(live, tmp_path)
    L.activate(k["key"])
    real_send = L.client._send

    def tamper(path, body, headers, timeout):
        status, hdrs, raw = real_send(path, body, headers, timeout)
        env = json.loads(raw)
        if "d" in env and isinstance(env["d"], dict) and "account" in env["d"]:
            env["d"]["account"]["credits"] = 99999                           # "give myself credits" by editing the answer
        return status, hdrs, json.dumps(env).encode()
    monkeypatch.setattr(L.client, "_send", tamper)
    with pytest.raises(CloudError) as e:
        L.client.session()
    assert e.value.code == "untrusted"
    assert L.account.credits == 3


def test_an_answer_to_another_request_cannot_be_replayed(live, tmp_path, monkeypatch):
    k = live.cloud.create_licence("Rahul", credits=3, actor="boss")
    L = make_licence(live, tmp_path)
    L.activate(k["key"])
    recorded = {}
    real_send = L.client._send

    def record(path, body, headers, timeout):
        out = real_send(path, body, headers, timeout)
        recorded.setdefault(path, out)
        return out
    monkeypatch.setattr(L.client, "_send", record)
    L.client.session()
    old = recorded["/api/v1/session"]
    monkeypatch.setattr(L.client, "_send", lambda *a, **kw: old)           # an attacker replays an old, genuinely signed answer
    with pytest.raises(CloudError) as e:
        L.client.session()
    assert e.value.code == "untrusted"


def test_a_wrong_clock_is_corrected_automatically(live, tmp_path, monkeypatch):
    import time as real
    k = live.cloud.create_licence("Rahul", credits=3, actor="boss")
    L = make_licence(live, tmp_path)
    original = real.time
    L.client.clock = lambda: original() - 45 * 60                            # this computer's clock is 45 minutes slow
    L.activate(k["key"])                                                     # hello learns the server's time ...
    assert abs(L.client.skew_ms - 45 * 60 * 1000) < 60_000
    L.client.skew_ms = 0                                                     # ... forget it: the next call is refused for its time ...
    assert L.refresh() == lic.OK                                             # ... and the app learns the correction from that answer and repeats it
    assert abs(L.client.skew_ms - 45 * 60 * 1000) < 60_000


def test_without_a_connection_the_app_says_so_and_does_not_crash(tmp_path):
    vault = Vault(tmp_path / "v.bin")
    client = CloudClient(DeviceKey(vault), 1, base_url="http://127.0.0.1:9", server_public="x" * 43)
    L = lic.Licence(client, vault, "test")
    with pytest.raises(Offline) as e:
        L.activate("BF-AAAA-BBBB-CCCC-DDDD")
    assert "internet" in e.value.message.lower()
    vault.update(licence_key="BF-AAAA-BBBB-CCCC-DDDD")
    assert L.resume() == lic.OFFLINE and not L.usable


def test_a_maintenance_pause_and_a_required_update_stop_bookings_with_clear_words(live, tmp_path):
    k = live.cloud.create_licence("Rahul", credits=5, actor="boss")
    L = make_licence(live, tmp_path, build=3)
    L.activate(k["key"])
    live.cloud.set_setting("maintenance", True, "boss")
    with pytest.raises(CloudError) as e:
        L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "m")
    assert e.value.code == "maintenance"
    live.cloud.set_setting("maintenance", False, "boss")
    live.cloud.set_setting("min_build", 9, "boss")
    with pytest.raises(CloudError) as e:
        L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "m")
    assert e.value.code == "update_required" and "update" in e.value.message.lower()
    assert live.cloud.db.scalar("SELECT credits FROM licences") == 5        # nothing was charged for any of it
