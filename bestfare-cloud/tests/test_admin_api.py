"""The admin panel's API: who may do what, and that every action really does it."""
import base64

import pytest

from bfcloud import bundle, crypto
from conftest import OWNER_PW, admin_login


async def test_login_gives_a_session_and_wrong_passwords_give_nothing(server):
    r = await server.get("/admin/api/me")
    assert r.status == 401
    st, j = await admin_login(server, "boss", "wrong-password-1")
    assert st == 401 and j["code"] == "bad_login"
    st, j = await admin_login(server, "nobody", OWNER_PW)
    assert st == 401 and j["message"] == "Wrong username or password."            # the same words whether the user exists or not
    st, j = await admin_login(server)
    assert st == 200 and j["role"] == "owner" and j["csrf"]
    r = await server.get("/admin/api/me")
    assert r.status == 200
    cookie = [c for c in server.session.cookie_jar][0]
    assert cookie["httponly"] and cookie["samesite"] == "Strict" and cookie["path"] == "/admin"


async def test_five_wrong_passwords_lock_the_login_even_for_the_right_one(server):
    for _ in range(5):
        st, _j = await admin_login(server, "boss", "wrong-password-1")
        assert st == 401
    st, j = await admin_login(server)
    assert st == 429 and "wait" in j["message"].lower()


async def test_changes_need_the_csrf_token_and_reading_does_not(server, admin):
    r = await server.post("/admin/api/licences", json={"label": "X", "credits": 1})              # logged in, but no token: another site cannot do this
    assert r.status == 403 and (await r.json())["code"] == "csrf"
    r = await server.post("/admin/api/licences", json={"label": "X", "credits": 1}, headers={"X-CSRF": "wrong"})
    assert r.status == 403
    st, j = await admin.post("/licences", {"label": "X", "credits": 1})
    assert st == 200 and j["key"].startswith("BF-")
    st, j = await admin.get("/licences")
    assert st == 200 and j["total"] == 1


async def test_logging_out_really_ends_the_session(server, admin):
    st, _ = await admin.post("/logout")
    assert st == 200
    r = await server.get("/admin/api/me")
    assert r.status == 401


async def test_the_whole_user_lifecycle_through_the_panel(server, admin, make_device):
    st, j = await admin.post("/licences", {"label": "Rahul Sharma", "credits": 5, "note": "NCC officer", "expires_days": 30})
    assert st == 200
    lid, key = j["id"], j["key"]
    st, j = await admin.post(f"/licences/{lid}/credits", {"delta": 10, "note": "bought 10"})
    assert j["credits"] == 15
    st, j = await admin.post(f"/licences/{lid}/credits", {"delta": -20})
    assert st == 400 and j["code"] == "no_credits"
    dev = make_device(name="RAHUL-PC", macs=("3c:52:82:aa:bb:cc",))
    st, _ = await dev.activate(key)
    assert st == 200
    st, j = await admin.get(f"/licences/{lid}")
    d = j["licence"]
    assert d["bound"] and d["device"]["name"] == "RAHUL-PC" and d["device"]["macs"] == ["3c:52:82:aa:bb:cc"] and d["credits"] == 15 and d["online"]
    other = make_device(name="OTHER", macs=("00:11:22:33:44:55",))
    st, _ = await other.activate(key)
    assert st == 403
    st, j = await admin.get(f"/licences/{lid}")
    assert j["licence"]["flagged"] and any(e["kind"] == "device_refused" for e in j["licence"]["events"])
    st, j = await admin.post(f"/licences/{lid}/block", {"reason": "shared the key"})
    st, r, _ = await dev.session()
    assert st == 401
    st, j = await admin.post(f"/licences/{lid}/unblock")
    st, j = await admin.post(f"/licences/{lid}/reset-device")
    st, _ = await other.activate(key)
    assert st == 200                                                               # the key now belongs to the new computer
    st, j = await admin.post(f"/licences/{lid}/reveal-key")
    assert j["key"] == key
    st, j = await admin.patch(f"/licences/{lid}", {"label": "Rahul S.", "mac_lock": "00:11:22:33:44:55", "note": "moved"})
    st, j = await admin.get(f"/licences/{lid}")
    assert j["licence"]["label"] == "Rahul S." and j["licence"]["mac_lock"] == "00:11:22:33:44:55"
    st, j = await admin.post(f"/licences/{lid}/regenerate-key")
    assert j["key"] != key
    st, j = await admin.delete(f"/licences/{lid}")
    assert st == 200
    st, j = await admin.get(f"/licences/{lid}")
    assert st == 404


async def test_the_booking_flow_shows_up_for_the_admin_who_can_refund_and_see_the_ledger(server, admin, user):
    st, j, _ = await user.begin(n=2)
    bid = j["d"]["booking_id"]
    await user.finish(bid, "done", "PNR111")
    lid = user.lic["id"]
    st, j = await admin.get(f"/licences/{lid}")
    d = j["licence"]
    assert d["credits"] == 3 and d["bookings_list"][0]["pnr"] == "PNR111" and d["ledger"][0]["delta"] == -2
    st, j = await admin.post(f"/bookings/{bid}/refund")
    assert st == 200 and j["credits"] == 5
    st, j = await admin.post(f"/bookings/{bid}/refund")
    assert st == 409
    st, j = await admin.get("/stats")
    assert j["stats"]["users"] == 1 and j["stats"]["bookings_total"] == 1 and j["stats"]["online"] == 1
    st, j = await admin.get("/events?level=info")
    assert any(e["kind"] == "admin_refund" for e in j["events"])


async def test_search_and_filters_work_through_the_api(server, admin):
    for n, c in (("Anita", 0), ("Rahul", 3), ("Meera", 8)):
        await admin.post("/licences", {"label": n, "credits": c})
    st, j = await admin.get("/licences?search=meer")
    assert [x["label"] for x in j["items"]] == ["Meera"]
    st, j = await admin.get("/licences?filter=nocredits")
    assert [x["label"] for x in j["items"]] == ["Anita"]
    st, j = await admin.get("/licences?limit=abc")
    assert st == 400


async def test_settings_can_be_changed_and_reach_the_app(server, admin, user):
    st, j = await admin.put("/settings", {"broadcast": "IndiGo is slow today", "maintenance": False})
    assert j["settings"]["broadcast"] == "IndiGo is slow today"
    st, j, _ = await user.session()
    assert j["d"]["account"]["broadcast"] == "IndiGo is slow today"
    st, j = await admin.put("/settings", {"maintenance": True, "maintenance_msg": "Back at 6 pm"})
    st, j, _ = await user.begin()
    assert st == 503 and j["d"]["message"] == "Back at 6 pm"
    for bad in ({"nonsense": 1}, {"min_build": -1}, {"maintenance": "yes"}, {"offers": {"offer1": {"code": "x y", "title": "A", "who": "B"}}}, {"broadcast": "x" * 700}):
        st, j = await admin.put("/settings", bad)
        assert st == 400, bad


async def test_an_admin_who_is_not_an_owner_cannot_manage_admins_releases_or_delete_users(server, cloud, admin):
    st, j = await admin.post("/admins", {"username": "helper", "role": "admin"})
    assert st == 200 and j["password"]
    helper_pw = j["password"]
    # a second client with its own cookies
    from aiohttp.test_utils import TestClient, TestServer
    c2 = TestClient(TestServer(server.server.app))
    await c2.start_server()
    try:
        st, j = await admin_login(c2, "helper", helper_pw)
        assert st == 200 and j["role"] == "admin" and j["must_change"]
        h = {"X-CSRF": j["csrf"]}
        r = await c2.get("/admin/api/admins")
        assert r.status == 403
        r = await c2.post("/admin/api/admins", json={"username": "x1x", "role": "owner"}, headers=h)
        assert r.status == 403
        r = await c2.post("/admin/api/releases", data=b"x", headers=h)
        assert r.status == 403
        r = await c2.post("/admin/api/licences", json={"label": "Made by helper", "credits": 2}, headers=h)
        assert r.status == 200
        lid = (await r.json())["id"]
        r = await c2.delete(f"/admin/api/licences/{lid}", headers=h)
        assert r.status == 403
        r = await c2.post(f"/admin/api/licences/{lid}/credits", json={"delta": 3}, headers=h)
        assert r.status == 200                                                     # an admin can do the daily work
    finally:
        await c2.close()


async def test_the_last_owner_cannot_be_removed_and_passwords_must_be_strong(server, admin):
    st, j = await admin.get("/admins")
    boss = j["admins"][0]["id"]
    st, j = await admin.patch(f"/admins/{boss}", {"disabled": True})
    assert st == 409 and j["code"] == "last_owner"
    st, j = await admin.patch(f"/admins/{boss}", {"role": "admin"})
    assert st == 409
    st, j = await admin.post("/admins", {"username": "weak1", "password": "password"})
    assert st == 400 and j["code"] == "weak_password"
    st, j = await admin.post("/admins", {"username": "a b", "password": "Correct-Horse-9"})
    assert st == 400 and j["code"] == "bad_username"
    st, j = await admin.post("/me/password", {"old": "wrong", "new": "Another-Strong-Pass-1"})
    assert st == 401
    st, j = await admin.post("/me/password", {"old": OWNER_PW, "new": "short"})
    assert st == 400
    st, j = await admin.post("/me/password", {"old": OWNER_PW, "new": "Another-Strong-Pass-1"})
    assert st == 200
    st, j = await admin.get("/stats")
    assert st == 401                                                               # changing the password ends every session


async def test_two_factor_login(server, admin):
    st, j = await admin.post("/me/totp/setup", {"password": OWNER_PW})
    secret = j["secret"]
    st, j = await admin.post("/me/totp/enable", {"code": "000000"})
    assert st == 400
    st, j = await admin.post("/me/totp/enable", {"code": crypto.totp_code(secret)})
    assert st == 200
    await admin.post("/logout")
    st, j = await admin_login(server)
    assert st == 401 and j["code"] == "totp_needed"
    r = await server.post("/admin/api/login", json={"username": "boss", "password": OWNER_PW, "totp": "123456"})
    assert r.status == 401 and (await r.json())["code"] in ("bad_login",)
    r = await server.post("/admin/api/login", json={"username": "boss", "password": OWNER_PW, "totp": crypto.totp_code(secret)})
    assert r.status == 200


async def test_publishing_a_release_through_the_panel(server, admin, cloud, release_signer, user):
    data, key, manifest = bundle.build({"app.py": b"print(2)\n"}, build_no=7, version="1.0.7", channel="stable", notes="n", signing_key=release_signer._k)
    h = {"X-CSRF": admin.csrf, "X-Release-Key": crypto.b64e(key), "X-Release-Notes": "Fixes the payment step"}
    r = await server.post("/admin/api/releases", data=data, headers=h)
    j = await r.json()
    assert r.status == 200 and j["build"] == 7
    r = await server.post("/admin/api/releases", data=data, headers=h)
    assert r.status == 409
    evil, ekey, _ = bundle.build({"app.py": b"evil"}, build_no=8, version="9", channel="stable", notes="", signing_key=crypto.Signer.generate()._k)
    r = await server.post("/admin/api/releases", data=evil, headers={**h, "X-Release-Key": crypto.b64e(ekey)})
    assert r.status == 400 and "signature" in (await r.json())["message"]
    st, j = await admin.get("/releases")
    assert [x["build"] for x in j["releases"]] == [7] and j["releases"][0]["state"] == "draft"
    st, j, _ = await user.call("/api/v1/update/check", {"build": 1})
    assert j["d"]["available"] is False
    st, j = await admin.patch("/releases/7", {"state": "live", "mandatory": True, "rollout": 100})
    st, j, _ = await user.call("/api/v1/update/check", {"build": 1})
    assert j["d"]["available"] and j["d"]["mandatory"]
    st, j = await admin.delete("/releases/7")
    assert st == 409                                                               # a live release must be withdrawn first
    await admin.patch("/releases/7", {"state": "withdrawn"})
    st, j, _ = await user.call("/api/v1/update/check", {"build": 1})
    assert j["d"]["available"] is False
    st, j = await admin.delete("/releases/7")
    assert st == 200
