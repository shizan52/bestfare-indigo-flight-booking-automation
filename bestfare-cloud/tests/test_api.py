"""The app protocol over real HTTP: signed requests, signed answers, replay, clock, stolen tokens, the booking gate, updates."""
import hashlib
import json
import time

import pytest

from bfcloud import bundle, crypto
from conftest import Device


async def test_hello_is_signed_so_the_app_knows_it_reached_the_real_server(server, cloud):
    r = await server.post("/api/v1/hello", headers={"X-BF-Nonce": "nonce-12345678"})
    j = await r.json()
    assert j["ok"] and j["d"]["pub"] == cloud.identity.public_b64
    core = {k: j[k] for k in ("ok", "d", "ts", "n", "p")}
    assert crypto.verify(cloud.identity.public_b64, crypto.canonical(core), j["sig"]) and j["n"] == "nonce-12345678"
    assert not crypto.verify(crypto.Signer.generate().public_b64, crypto.canonical(core), j["sig"])    # a fake server cannot sign this


async def test_activation_returns_a_session_and_the_account_but_never_an_offer_code(user):
    st, j, _ = await user.session()
    assert st == 200 and j["ok"]
    a = j["d"]["account"]
    assert a["credits"] == 5 and a["label"] == "Rahul Sharma" and a["status"] == "active"
    assert "6ETST001" not in json.dumps(j) and "PROMO2" not in json.dumps(j)         # codes only ever travel inside a booking ticket
    assert set(a["offers"]) == {"offer1", "offer2"} and "code" not in a["offers"]["offer1"]


async def test_a_wrong_key_gets_one_vague_answer_and_five_in_a_row_lock_the_address(make_device, cloud):
    d = make_device()
    for _ in range(5):
        st, j = await d.activate("BF-AAAA-BBBB-CCCC-DDDD")
        assert st == 400 and j["d"]["code"] == "bad_key"
    st, j = await d.activate("BF-AAAA-BBBB-CCCC-DDDD")
    assert st == 429 and "wait" in j["message"].lower()
    lic = cloud.create_licence("Rahul", credits=1)
    st, j = await d.activate(lic["key"])                                           # even the RIGHT key waits: the guessing is stopped
    assert st == 429


async def test_a_second_computer_with_the_right_key_is_refused_with_a_clear_message(user, make_device):
    other = make_device(name="OTHER-PC", macs=("11:22:33:44:55:66",))
    st, j = await other.activate(user.lic["key"])
    assert st == 403 and j["d"]["code"] == "device_mismatch" and "another computer" in j["d"]["message"]
    st, j, _ = await user.session()
    assert st == 200                                                               # the owner is not disturbed


async def test_a_request_can_be_used_only_once(user):
    nonce = "fixed-nonce-abcdef"
    st1, _j, _ = await user.call("/api/v1/session", {"build": 1}, nonce=nonce)
    assert st1 == 200
    st2, j2, _ = await user.call("/api/v1/session", {"build": 1}, nonce=nonce, verify=False)
    assert st2 == 409 and j2["code"] == "replay"


async def test_a_request_with_a_wrong_clock_is_refused_and_the_server_time_is_given(user):
    st, j, _ = await user.call("/api/v1/session", {}, skew=-10 * 60 * 1000, verify=False)
    assert st == 400 and j["code"] == "clock" and abs(j["server_time"] - time.time() * 1000) < 5000
    st, j, _ = await user.call("/api/v1/session", {}, skew=10 * 60 * 1000, verify=False)
    assert st == 400 and j["code"] == "clock"


async def test_a_stolen_token_is_useless_without_the_devices_own_key(user, make_device):
    thief = make_device(name="THIEF")
    st, j, _ = await thief.call("/api/v1/session", {"build": 1}, token=user.token, verify=False)       # right token, wrong signing key
    assert st == 401 and j["code"] == "unauthorized"
    st, j, _ = await user.call("/api/v1/booking/begin", {"n": 1, "offer": "offer1", "route": "DEL-BOM", "date": "2026-12-01"},
                               sign_with=thief.signer, verify=False)
    assert st == 401
    assert user.lic and user.lic["id"]


async def test_a_changed_body_or_path_breaks_the_signature(user):
    body = json.dumps({"n": 1, "offer": "offer1", "route": "DEL-BOM", "date": "2026-12-01", "ref": "x"}).encode()
    evil = json.dumps({"n": 1, "offer": "offer1", "route": "DEL-BOM", "date": "2026-12-01", "ref": "x", "extra": 1}).encode()
    st, j, _ = await user.call("/api/v1/booking/begin", json.loads(body), tamper_body=evil, verify=False)     # signed one body, sent another
    assert st == 401
    st, j, _ = await user.call("/api/v1/session", {}, token="not.a.token", verify=False)
    assert st == 401
    st, j, _ = await user.call("/api/v1/session", {}, token="", verify=False)
    assert st == 401


async def test_the_whole_booking_round_trip_charges_and_reports(user, cloud):
    st, j, _ = await user.begin(n=2, offer="offer1")
    assert st == 200 and j["d"]["promo"] == "6ETST001" and j["d"]["credits"] == 3 and j["d"]["charged"] == 2
    bid = j["d"]["booking_id"]
    st, j, _ = await user.finish(bid, "done", "XYZ789")
    assert st == 200 and j["d"]["status"] == "done" and j["d"]["credits"] == 3
    st, j, _ = await user.begin(n=1, offer="offer2")
    bid2 = j["d"]["booking_id"]
    st, j, _ = await user.finish(bid2, "failed", reason="no seats")
    assert j["d"]["refunded"] is True and j["d"]["credits"] == 3
    st, j, _ = await user.session()
    assert j["d"]["account"]["credits"] == 3 and j["d"]["open"] == []


async def test_no_credits_means_no_booking_and_a_clear_message(user):
    st, j, _ = await user.begin(n=6)
    assert st == 402 and j["d"]["code"] == "no_credits" and j["d"]["needed"] == 6 and j["d"]["have"] == 5
    assert "credit" in j["d"]["message"] and "6ETST001" not in json.dumps(j)


async def test_a_blocked_user_is_stopped_at_the_next_request(user, cloud):
    cloud.set_status(user.lic["id"], "blocked", "misuse", "boss")
    st, j, _ = await user.session()
    assert st == 401                                                               # the session itself ended
    st, j = await user.activate(user.lic["key"])
    assert st == 403 and j["d"]["code"] == "blocked" and j["d"]["message"] == "misuse"


async def test_garbage_requests_are_answered_politely_and_never_crash(server, user):
    for body in (b"", b"not json", b"[1,2]", b"null", b"\xff\xfe", b"{" * 5000, b'{"n":' + b"9" * 400 + b"}"):
        r = await server.post("/api/v1/booking/begin", data=body, headers={"X-BF-Nonce": "abcdefgh1234", "X-BF-Ts": str(int(time.time() * 1000)),
                                                                          "X-BF-Sig": "x", "X-BF-Token": user.token})
        assert r.status in (400, 401, 413), (body[:20], r.status)
        assert (await r.json())["ok"] is False
    r = await server.post("/api/v1/activate", data=b"{}", headers={"X-BF-Nonce": "abcdefgh1234", "X-BF-Ts": "abc", "X-BF-Sig": "x"})
    assert r.status == 400
    r = await server.post("/api/v1/session", data=b"x" * (300 * 1024))
    assert r.status == 413


async def test_unknown_paths_and_methods_leak_nothing(server):
    for method, path in (("get", "/api/v1/session"), ("get", "/admin/api/licences"), ("post", "/nope"), ("get", "/data/cloud.db"), ("get", "/../etc/passwd"),
                         ("get", "/admin/static/../../bfcloud/service.py"), ("get", "/admin/static/service.py")):
        r = await getattr(server, method)(path)
        assert r.status in (401, 404, 405), (path, r.status)
        assert b"Traceback" not in await r.read()


def release(signer, build_no, files=None):
    data, key, manifest = bundle.build(files or {"app.py": b"print(1)\n"}, build_no=build_no, version=f"1.0.{build_no}", channel="stable", notes="fix", signing_key=signer._k)
    return data, crypto.b64e(key), manifest


async def test_an_update_is_offered_downloaded_verified_and_opened_with_a_key_only_this_device_can_read(user, cloud, release_signer):
    data, key, manifest = release(release_signer, 2, {"app.py": b"print('v2')\n", "ext/core.js": b"//x"})
    cloud.add_release(data, key)
    st, j, _ = await user.call("/api/v1/update/check", {"build": 1})
    assert j["d"]["available"] is False                                            # not live yet
    cloud.edit_release(2, state="live", mandatory=True)
    st, j, _ = await user.call("/api/v1/update/check", {"build": 1})
    assert j["d"]["available"] and j["d"]["build"] == 2 and j["d"]["mandatory"] and j["d"]["sha256"] == hashlib.sha256(data).hexdigest()
    st, blob, resp = await user.call("/api/v1/update/download", {"build": 2})
    assert st == 200 and blob == data
    sig = resp.headers["X-BF-Sig"]
    nonce = resp.headers["X-BF-Nonce"]
    assert crypto.verify(cloud.identity.public_b64, b"BF1dl\n/api/v1/update/download\n" + nonce.encode() + b"\n" + hashlib.sha256(blob).hexdigest().encode(), sig)
    m, payload = bundle.read_bundle(blob, [release_signer.public_b64])
    priv, epk = crypto.new_ephemeral()
    st, j, _ = await user.call("/api/v1/update/key", {"build": 2, "epk": epk})
    assert st == 200
    ckey = crypto.unwrap_with(priv, j["d"], f"release/2/{cloud.db.one('SELECT device_id FROM licences')['device_id']}".encode())
    files = bundle.decrypt_files(m, payload, ckey)
    assert files["app.py"] == b"print('v2')\n"


async def test_nobody_without_a_session_can_download_a_release_or_get_its_key(server, cloud, release_signer):
    data, key, _ = release(release_signer, 2)
    cloud.add_release(data, key)
    cloud.edit_release(2, state="live")
    for path in ("/api/v1/update/download", "/api/v1/update/key", "/api/v1/update/check"):
        r = await server.post(path, json={"build": 2, "epk": "x"})
        assert r.status in (400, 401)


async def test_a_release_that_is_not_live_cannot_be_downloaded_even_with_a_session(user, cloud, release_signer):
    data, key, _ = release(release_signer, 2)
    cloud.add_release(data, key)
    st, j, _ = await user.call("/api/v1/update/download", {"build": 2}, verify=False)
    assert st == 404
    st, j, _ = await user.call("/api/v1/update/key", {"build": 2, "epk": crypto.new_ephemeral()[1]}, verify=False)
    assert st == 404
    for b in (-1, 10**12, "2", None, True, 2.5):
        st, j, _ = await user.call("/api/v1/update/download", {"build": b}, verify=False)
        assert st in (400, 404)
