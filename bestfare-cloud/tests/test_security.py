"""The red-team suite: every test is an ATTACK on the real server (a stranger, a curious user, a user with a valid key, a stolen
session, a hostile admin) and asserts that the attack gets nowhere.

Written the way an attacker thinks: send what the app would never send, send it twice, send it at the same time, send it as somebody
else, send it too big, send it hoping the server trusts something it should not.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from aiohttp import ClientError, ClientSession, DummyCookieJar
from yarl import URL

from bfcloud import bundle, crypto
from conftest import OWNER_PW, Device, admin_login

SQLI = ["' OR 1=1 --", "'; DROP TABLE licences; --", "\" OR \"\"=\"", "x'); DELETE FROM ledger; --", "%' UNION SELECT key_enc,1,1,1,1,1,1,1 FROM licences --",
        "\\", "\x00", "a\x00b", "' || (SELECT key_hash FROM licences) || '", "1; ATTACH DATABASE '/tmp/x' AS x; --", "𝕏" * 10, "%", "_"]
XSS = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "javascript:alert(1)", "<svg/onload=alert(1)>", "{{7*7}}", "${7*7}"]


async def bare(server) -> ClientSession:
    """A visitor with no cookies at all."""
    return ClientSession(base_url=str(server.make_url("/")), cookie_jar=DummyCookieJar())


def tables_intact(cloud) -> None:
    for t in ("admins", "licences", "ledger", "bookings", "events", "releases", "settings"):
        assert cloud.db.scalar(f"SELECT COUNT(*) FROM {t}") is not None


# ═══ 1. the admin panel as a stranger ══════════════════════════════════════════════════════════════════════════════════
def all_routes(server):
    out = []
    for res in server.bf.app.router.resources():
        info = res.get_info()
        path = info.get("formatter") or info.get("path") or ""
        for r in res:
            out.append((r.method, re.sub(r"\{[a-z]+\}", "1", path)))
    return out


async def test_every_admin_route_refuses_a_stranger(server):
    s = await bare(server)
    try:
        n = 0
        for method, path in all_routes(server):
            if not path.startswith("/admin/api/") or method == "HEAD" or path.endswith("/login") or path.endswith("/logout"):
                continue
            r = await s.request(method, path, json={}, headers={"X-CSRF": "x"})
            assert r.status == 401, (method, path, r.status)
            n += 1
        assert n >= 25                                         # (the loop really covered the panel)
    finally:
        await s.close()


async def test_every_change_refuses_a_logged_in_page_without_the_csrf_token(server, admin):
    """A hostile web page open in the admin's other tab can make the browser send the cookie - but it cannot send this header."""
    n = 0
    for method, path in all_routes(server):
        if not path.startswith("/admin/api/") or method in ("GET", "HEAD") or path.endswith("/login") or path.endswith("/logout"):
            continue
        for hdrs in ({}, {"X-CSRF": "wrong"}, {"X-CSRF": ""}):
            r = await server.request(method, path, json={}, headers=hdrs)
            assert r.status == 403, (method, path, hdrs, r.status)
        n += 1
    assert n >= 15


async def test_a_plain_admin_cannot_do_owner_things_and_cannot_make_themselves_owner(server, admin, cloud):
    st, j = await admin.post("/admins", {"username": "helper", "role": "admin"})
    assert st == 200
    helper_pw = j["password"]
    helper_id = j["id"]
    await admin.post("/logout")
    st, j = await admin_login(server, "helper", helper_pw)
    assert st == 200
    csrf = j["csrf"]
    h = {"X-CSRF": csrf}
    assert (await server.get("/admin/api/admins")).status == 403
    assert (await server.post("/admin/api/admins", json={"username": "x" * 5, "role": "owner"}, headers=h)).status == 403
    assert (await server.patch(f"/admin/api/admins/{helper_id}", json={"role": "owner"}, headers=h)).status == 403      # not even for themselves
    assert (await server.post("/admin/api/releases", data=b"x", headers=h)).status == 403
    assert (await server.delete("/admin/api/releases/1", headers=h)).status == 403
    assert (await server.delete("/admin/api/licences/1", headers=h)).status == 403
    assert cloud.db.one("SELECT role FROM admins WHERE id=?", (helper_id,))["role"] == "admin"


async def test_a_logged_out_cookie_and_an_invented_cookie_are_worth_nothing(server):
    st, j = await admin_login(server)
    sid = [c for c in server.session.cookie_jar if c.key == "bf_admin"][0].value
    await server.post("/admin/api/logout")
    s = await bare(server)
    try:
        for cookie in (sid, "A" * 43, "", "' OR 1=1 --"):
            r = await s.get("/admin/api/me", headers={"Cookie": f"bf_admin={cookie}"})
            assert r.status == 401
    finally:
        await s.close()


async def test_logging_in_always_gives_a_new_session_id(server, cloud):
    """Session fixation: an id the attacker planted before the login must not become the logged-in session."""
    s = await bare(server)
    try:
        r = await s.post("/admin/api/login", json={"username": "boss", "password": OWNER_PW}, headers={"Cookie": "bf_admin=PLANTED-BY-ATTACKER"})
        assert r.status == 200
        new = r.cookies["bf_admin"].value
        assert new != "PLANTED-BY-ATTACKER" and len(new) >= 40
        r = await s.get("/admin/api/me", headers={"Cookie": "bf_admin=PLANTED-BY-ATTACKER"})
        assert r.status == 401
    finally:
        await s.close()


async def test_the_session_id_is_never_stored_in_clear(server, cloud):
    await admin_login(server)
    sid = [c for c in server.session.cookie_jar if c.key == "bf_admin"][0].value
    raw = b"".join(p.read_bytes() for p in Path(cloud.dir).glob("*.db*"))
    assert len(raw) > 1000 and sid.encode() not in raw


async def test_unknown_and_known_usernames_cost_the_same_work_and_say_the_same_words(server, monkeypatch):
    calls = []
    real = crypto.password_check
    monkeypatch.setattr(crypto, "password_check", lambda pw, h: calls.append(1) or real(pw, h))
    a = await admin_login(server, "boss", "wrong-password-1")
    b = await admin_login(server, "ghost", "wrong-password-1")
    assert len(calls) == 2                                      # a password hash was checked in both cases: no timing tells who exists
    assert a[0] == b[0] == 401 and a[1] == b[1]


async def test_guessing_one_admin_password_from_many_addresses_is_stopped_too(server, cloud):
    """A botnet: every guess from a different address, so the per-address limit never triggers."""
    server.bf.cfg.behind_proxy = True                            # (the proxy tells the address; the test plays the proxy)
    codes = []
    for i in range(30):
        r = await server.post("/admin/api/login", json={"username": "boss", "password": f"wrong-password-{i}"}, headers={"X-Real-IP": f"203.0.113.{i + 1}"})
        codes.append(r.status)
    assert 429 in codes and codes.index(429) <= 14, codes       # the account itself locks after a handful of failures, whoever sends them
    r = await server.post("/admin/api/login", json={"username": "boss", "password": OWNER_PW}, headers={"X-Real-IP": "198.51.100.9"})
    assert r.status == 429                                       # and then even the right password waits


async def test_a_faked_address_header_does_not_reset_the_lockout_when_there_is_no_proxy(server):
    for _ in range(5):
        await server.post("/admin/api/login", json={"username": "boss", "password": "nope-nope-1A"}, headers={"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"})
    r = await server.post("/admin/api/login", json={"username": "boss", "password": "nope-nope-1A"}, headers={"X-Real-IP": "9.9.9.9", "X-Forwarded-For": "8.8.8.8"})
    assert r.status == 429


async def test_login_survives_hostile_input(server, cloud):
    for bad in SQLI + XSS + [None, 5, [], {}, True, "A" * 5000]:
        r = await server.post("/admin/api/login", json={"username": bad, "password": bad})
        assert r.status in (401, 429), (bad, r.status)
    tables_intact(cloud)
    for body in (b"", b"not json", b"[]", b'"x"', b"null", b"\xff\xfe\x00", b"{" * 5000, b"[" * 200000):
        r = await server.post("/admin/api/login", data=body, headers={"Content-Type": "application/json"})
        assert r.status in (400, 401, 413, 429), (body[:20], r.status)
        assert "Traceback" not in await r.text()


# ═══ 2. second factor and passwords ════════════════════════════════════════════════════════════════════════════════════
async def test_switching_the_second_factor_off_or_replacing_it_needs_the_password(server, admin, cloud):
    """A stolen session (cookie + page) must not be enough to remove or swap the 2FA."""
    st, j = await admin.post("/me/totp/setup", {})
    assert st == 400 or st == 401, "setting up a new second factor without the password must be refused"
    st, j = await admin.post("/me/totp/setup", {"password": "wrong-password-1"})
    assert st == 401
    st, j = await admin.post("/me/totp/setup", {"password": OWNER_PW})
    assert st == 200
    secret = j["secret"]
    st, _ = await admin.post("/me/totp/enable", {"code": crypto.totp_code(secret)})
    assert st == 200
    st, _ = await admin.post("/me/totp/disable", {})
    assert st in (400, 401)
    st, _ = await admin.post("/me/totp/disable", {"password": "wrong-password-1"})
    assert st == 401
    st, _ = await admin.post("/me/totp/setup", {})                # (while it is on, a swap without the password is refused too)
    assert st in (400, 401)


async def _with_totp(server, admin):
    st, j = await admin.post("/me/totp/setup", {"password": OWNER_PW})
    secret = j["secret"]
    await admin.post("/me/totp/enable", {"code": crypto.totp_code(secret)})
    await admin.post("/logout")
    return secret


async def test_a_second_factor_code_works_once_only(server, admin, cloud):
    secret = await _with_totp(server, admin)
    code = crypto.totp_code(secret)
    r = await server.post("/admin/api/login", json={"username": "boss", "password": OWNER_PW, "totp": code})
    assert r.status == 200
    await server.post("/admin/api/logout")
    r = await server.post("/admin/api/login", json={"username": "boss", "password": OWNER_PW, "totp": code})      # somebody who watched the code
    assert r.status == 401
    nxt = crypto.totp_code(secret, time.time() + 30)
    r = await server.post("/admin/api/login", json={"username": "boss", "password": OWNER_PW, "totp": nxt})
    assert r.status == 200                                                                                          # the next code is fine


async def test_the_second_factor_secret_is_not_in_the_database_in_clear(server, admin, cloud):
    secret = await _with_totp(server, admin)
    raw = b"".join(p.read_bytes() for p in Path(cloud.dir).glob("*.db*"))
    assert len(raw) > 1000 and secret.encode() not in raw


async def test_the_right_password_with_a_wrong_code_is_counted_as_a_failure(server, admin):
    secret = await _with_totp(server, admin)
    statuses = []
    for i in range(8):
        r = await server.post("/admin/api/login", json={"username": "boss", "password": OWNER_PW, "totp": f"{i:06d}"})
        statuses.append(r.status)
    assert statuses[0] == 401 and 429 in statuses                  # six digits cannot be guessed at leisure


async def test_changing_the_password_ends_every_other_session(server, admin, cloud):
    st, _ = await admin.post("/me/password", {"old": OWNER_PW, "new": "Another-Long-Pass-77"})
    assert st == 200
    assert (await server.get("/admin/api/me")).status == 401
    assert cloud.db.scalar("SELECT COUNT(*) FROM admin_sessions") == 0


async def test_a_stolen_session_cannot_guess_the_current_password_at_leisure(server, admin):
    codes = []
    for i in range(12):
        st, j = await admin.post("/me/password", {"old": f"guess-{i}-Aa", "new": "Another-Long-Pass-77"})
        codes.append(st)
    assert codes[0] == 401 and 429 in codes


async def test_weak_and_junk_passwords_are_refused(server, admin):
    for i, pw in enumerate(("short", "alllowercaseletters", "12345678901234", "A" * 10, "abcdefghijkl")):
        st, _ = await admin.post("/admins", {"username": f"weakling{i}", "password": pw, "role": "admin"})
        assert st == 400, pw


# ═══ 3. injection and hostile text everywhere ══════════════════════════════════════════════════════════════════════════
async def test_sql_injection_in_every_text_field_changes_nothing(server, admin, cloud, make_device):
    st, j = await admin.post("/licences", {"label": "Honest Person", "credits": 3})
    lid, key = j["id"], j["key"]
    before = {t: cloud.db.scalar(f"SELECT COUNT(*) FROM {t}") for t in ("admins", "licences", "ledger", "bookings", "releases")}
    for p in SQLI:
        # the admin side: every field that takes text
        await admin.post("/licences", {"label": p or "x", "note": p, "mac_lock": p})
        await admin.patch(f"/licences/{lid}", {"label": p or "x", "note": p, "mac_lock": p})
        await admin.post(f"/licences/{lid}/credits", {"delta": 1, "note": p})
        await admin.post(f"/licences/{lid}/block", {"reason": p})
        await admin.post(f"/licences/{lid}/unblock", {})
        await admin.get(f"/licences?search={p}&filter={p}&limit=5")
        await admin.get(f"/events?level={p}&kind={p}")
        await admin.post("/bookings/" + p.replace("/", "_").replace("?", "_").replace("#", "_") + "/refund", {})
        await admin.put("/settings", {"broadcast": p})
        await admin.put("/settings", {"offers": {"offer1": {"code": p, "title": p, "who": p}}})
    # the app side: every field the app sends
    dev = make_device(name="PC'; DROP TABLE licences; --", macs=("3c:52:82:aa:bb:cc", "' OR 1=1"))
    st, _ = await dev.activate(key)
    assert st == 200
    for p in SQLI:
        await dev.begin(1, "offer1", p, p, p)
        await dev.begin(1, p, "DEL-BOM", "2026-12-01", p)
        await dev.begin(p, "offer1", "DEL-BOM", "2026-12-01", "ref-" + str(abs(hash(p))))
        await dev.call("/api/v1/booking/finish", {"booking_id": p, "outcome": p, "pnr": p, "reason": p})
        await dev.call("/api/v1/update/key", {"build": p, "epk": p})
        await dev.call("/api/v1/update/check", {"build": p, "channel": p})
    await dev.activate(p)
    tables_intact(cloud)
    assert cloud.db.scalar("SELECT COUNT(*) FROM admins") == before["admins"]
    assert cloud.db.scalar("SELECT COUNT(*) FROM releases") == before["releases"]
    assert cloud.db.one("SELECT label FROM licences WHERE id=?", (lid,)) is not None


async def test_a_percent_or_underscore_in_the_search_box_is_text_not_a_wildcard(server, admin):
    for label in ("Alpha", "Bravo", "Charlie"):
        await admin.post("/licences", {"label": label, "credits": 1})
    for wild in ("%", "_", "%%", "A_pha"):
        st, j = await admin.get("/licences?search=" + wild)
        assert st == 200 and j["total"] == 0, wild


async def test_hostile_text_is_stored_as_plain_text_and_the_panel_cannot_run_it(server, admin):
    for i, p in enumerate(XSS):
        await admin.post("/licences", {"label": p, "note": p, "credits": 1})
    st, j = await admin.get("/licences?limit=50")
    labels = [x["label"] for x in j["items"]]
    assert all(p[:60] in labels for p in XSS)                    # kept exactly as typed: it is the page that must treat it as text
    js = (Path(__file__).resolve().parents[1] / "bfcloud" / "web" / "app.js").read_text(encoding="utf-8")
    html = (Path(__file__).resolve().parents[1] / "bfcloud" / "web" / "index.html").read_text(encoding="utf-8")
    for bad in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(\"", "setInterval(\"", "javascript:"):
        assert bad not in js, bad
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and not re.search(r"\son[a-z]+\s*=", html) and "style=" not in html


async def test_every_answer_carries_the_security_headers_even_a_404(server):
    for method, path in (("GET", "/nothing-here"), ("POST", "/healthz"), ("GET", "/admin/static/evil.js"), ("GET", "/healthz"), ("GET", "/admin/"),
                         ("POST", "/api/v1/session"), ("GET", "/admin/api/me")):
        r = await server.request(method, path)
        h = r.headers
        assert h.get("X-Content-Type-Options") == "nosniff", (path, r.status)
        assert "default-src 'self'" in h.get("Content-Security-Policy", ""), (path, r.status)
        assert h.get("X-Frame-Options") == "DENY" and h.get("Referrer-Policy") == "no-referrer", path
        assert h.get("Server") == "bestfare", (path, h.get("Server"))        # not "Python/3.x aiohttp/3.x"
        assert "X-Powered-By" not in h


async def test_no_answer_ever_shows_a_traceback_a_path_or_sql(server, admin, user):
    texts = []
    for path in ("/admin/api/licences/abc", "/admin/api/licences/99999999999999999999", "/admin/api/licences/-1", "/admin/api/releases/x",
                 "/admin/api/admins/x", "/admin/api/licences?limit=x", "/admin/api/events?limit=%00"):
        r = await server.get(path)
        texts.append(await r.text())
    st, j, r = await user.call("/api/v1/booking/begin", {"n": {"a": 1}, "offer": [], "route": 5, "date": None, "ref": {}})
    texts.append(json.dumps(j))
    for t in texts:
        assert not re.search(r"Traceback|File \"|\.py\"|sqlite|SELECT |INSERT |bfcloud|site-packages", t), t[:200]


# ═══ 4. files the server must never hand out ═══════════════════════════════════════════════════════════════════════════
async def test_no_way_to_fetch_a_file_that_is_not_on_the_fixed_list(server, cloud):
    s = await bare(server)
    try:
        paths = ["/admin/static/..%2f..%2fdata%2fidentity.pem", "/admin/static/%2e%2e%2f%2e%2e%2fservice.py", "/admin/static/..\\..\\service.py",
                 "/admin/static/app.js%00.png", "/admin/static/app.js/", "/admin/static/.%2e/crypto.py", "/admin/static/%252e%252e%252fcrypto.py",
                 "/identity.pem", "/data/identity.pem", "/server.db", "/bfcloud/crypto.py", "/admin/../identity.pem", "/admin/static/",
                 "/admin/static/index.html", "/admin/static/__init__.py", "/admin/static/app.js.map", "/.env", "/.git/config"]
        for p in paths:
            r = await s.get(URL(p, encoded=True))
            body = await r.read()
            assert r.status in (400, 404, 405), (p, r.status)
            assert b"BEGIN" not in body and b"PRIVATE" not in body and b"def " not in body, p
    finally:
        await s.close()
    for name in ("app.js", "style.css", "logo.svg"):                                   # the three real files still work
        r = await server.get(f"/admin/static/{name}")
        assert r.status == 200


async def test_the_data_folder_is_not_inside_the_served_folder(server, cloud):
    from bfcloud import web as w
    assert Path(cloud.dir).resolve() not in Path(w.WEB_DIR).resolve().parents and Path(w.WEB_DIR).resolve() not in Path(cloud.dir).resolve().parents
    names = {p.name for p in Path(w.WEB_DIR).iterdir()}
    assert not any(n.endswith((".pem", ".db", ".key", ".env", ".py")) for n in names), names


@pytest.mark.skipif(sys.platform == "win32", reason="file modes only mean something on Linux")
def test_the_secret_files_are_readable_by_the_server_user_only(cloud):
    for p in Path(cloud.dir).iterdir():
        if p.suffix in (".pem", ".key") or p.name in ("identity.pem", "secrets.bin"):
            assert (p.stat().st_mode & 0o077) == 0, p


def test_secrets_are_not_in_the_database_in_clear(cloud):
    lic = cloud.create_licence("Secret Sam", credits=2, actor="t")
    cloud.db._db.execute("PRAGMA wal_checkpoint(FULL)")
    raw = b"".join(p.read_bytes() for p in Path(cloud.dir).glob("*.db*"))
    assert len(raw) > 1000 and lic["key"].encode() not in raw and lic["key"].replace("-", "").encode() not in raw
    assert OWNER_PW.encode() not in raw


# ═══ 5. sizes and shapes ═══════════════════════════════════════════════════════════════════════════════════════════════
async def test_a_too_big_body_is_refused_whether_or_not_it_admits_its_size(server, user):
    r = await server.post("/api/v1/session", data=b"x" * (300 * 1024), headers={"Content-Type": "application/json"})
    assert r.status == 413

    async def chunks():                                   # a body that never says how long it is (chunked)
        for _ in range(40):
            yield b"y" * 65536

    try:
        r = await server.post("/admin/api/login", data=chunks(), headers={"Content-Type": "application/json"})     # (a route that reads its body before knowing who asks)
        status = r.status
    except (ClientError, ConnectionError):                 # the server may also simply hang up on a client that sends 2.6 MB of rubbish
        status = 413
    assert status == 413


async def test_numbers_that_are_not_ordinary_numbers_are_refused(server, admin, cloud):
    st, j = await admin.post("/licences", {"label": "Num", "credits": 5})
    lid = j["id"]
    for bad in (0, -1000000, 2 ** 70, 10 ** 30, 1.5, "5", True, None, [5], {"a": 1}, 1e308):
        st, j = await admin.post(f"/licences/{lid}/credits", {"delta": bad})
        assert st == 400, (bad, st)
    st, j = await admin.get("/licences/%d" % lid)
    assert j["licence"]["credits"] == 5
    for bad in (-1, 2 ** 70, 1.5, "5", True, None):
        st, _ = await admin.post("/licences", {"label": "N2", "credits": bad})
        assert st == 400, (bad, st)
    for q in ("limit=-1", "limit=0", "limit=99999999", "offset=-5", "limit=1e3", "limit=%27", "offset=9999999999999999999999"):
        st, j = await admin.get("/licences?" + q)
        assert st in (200, 400), (q, st)
        if st == 200:
            assert len(j["items"]) <= 500, q


async def test_ids_that_do_not_exist_are_a_clean_404_never_a_crash(server, admin):
    for lid in ("0", "-1", "99999", "1.5", "abc", "99999999999999999999", "%00", "1%20OR%201=1"):
        for method, suffix in (("get", ""), ("post", "/credits"), ("post", "/block"), ("post", "/unblock"), ("post", "/reset-device"),
                               ("post", "/reveal-key"), ("post", "/regenerate-key")):
            r = await getattr(server, method)(f"/admin/api/licences/{lid}{suffix}", **({"json": {"delta": 1}, "headers": {"X-CSRF": admin.csrf}} if method == "post" else {}))
            assert r.status in (400, 404), (lid, suffix, r.status)


async def test_settings_are_validated_all_or_nothing(server, admin, cloud):
    st, before = await admin.get("/settings")
    st, j = await admin.put("/settings", {"broadcast": "hello", "no_such_setting": 1})
    assert st == 400
    st, after = await admin.get("/settings")
    assert after["settings"]["broadcast"] == before["settings"]["broadcast"]          # the valid half was not applied either
    for k, v in (("min_build", -1), ("min_build", "x"), ("refund_daily_limit", 10 ** 9), ("maintenance", "yes"), ("refund_ratio_limit", 7),
                 ("offers", {}), ("offers", {"offer1": {"code": "has space", "title": "t", "who": "w"}}), ("offers", {"x": 1}), ("broadcast", "A" * 700)):
        st, _ = await admin.put("/settings", {k: v})
        assert st == 400, (k, v)


# ═══ 6. the app API: a stranger, a thief, a cheat ══════════════════════════════════════════════════════════════════════
async def test_without_a_session_the_app_api_gives_nothing(server, make_device):
    dev = make_device()
    for path in ("/api/v1/session", "/api/v1/booking/begin", "/api/v1/booking/finish", "/api/v1/booking/open", "/api/v1/update/check",
                 "/api/v1/update/download", "/api/v1/update/key"):
        st, j, _ = await dev.call(path, {"n": 1}, token="", verify=False)
        assert st == 401, (path, st)
    for tok in ("x", "A" * 80, "a.b", "a.b.c", "' OR 1=1"):
        st, j, _ = await dev.call("/api/v1/session", {"n": 1}, token=tok, verify=False)
        assert st == 401, (tok, st)


async def test_a_stolen_token_is_useless_without_the_device_key(server, user, make_device, cloud):
    thief = make_device(name="THIEF-PC")
    thief.token = user.token                                         # the token was copied (a logged request, a memory dump of the shell ...)
    st, j, _ = await thief.session()
    assert st == 401
    st, j, _ = await thief.begin()
    assert st == 401 and cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],)) == 5
    assert cloud.events(kind="bad_signature")                        # and the admin is told


async def test_a_request_cannot_be_changed_on_the_way_or_used_twice(server, user, cloud):
    st, j, _ = await user.call("/api/v1/session", {"build": 1}, tamper_body=json.dumps({"build": 2}).encode())
    assert st == 401                                                   # the body no longer matches what was signed
    st, j, _ = await user.call("/api/v1/session", {"build": 1}, skew=-3600 * 1000, verify=False)
    assert st == 400 and j["code"] == "clock"
    st, j, _ = await user.call("/api/v1/session", {"build": 1}, skew=3600 * 1000, verify=False)
    assert st == 400
    st, j, _ = await user.call("/api/v1/booking/begin", {"n": 1, "offer": "offer1", "route": "DEL-BOM", "date": "2026-12-01", "ref": "once-0001"}, nonce="nonce-for-one-use")
    assert st == 200 and j["d"]["promo"]
    st, j, _ = await user.call("/api/v1/booking/begin", {"n": 1, "offer": "offer1", "route": "DEL-BOM", "date": "2026-12-01", "ref": "once-0001"}, nonce="nonce-for-one-use", verify=False)
    assert st == 409                                                   # the very same message again (a replay) is refused
    assert cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],)) == 4


async def test_a_session_token_dies_when_the_user_is_blocked_reset_or_given_a_new_key(server, user, cloud, admin):
    lid = user.lic["id"]
    st, _, _ = await user.session()
    assert st == 200
    await admin.post(f"/licences/{lid}/block", {"reason": "misuse"})
    st, _, _ = await user.session()
    assert st in (401, 403)
    await admin.post(f"/licences/{lid}/unblock", {})
    st, _, _ = await user.session()
    assert st == 401                                                   # unblocking does not bring the old session back
    st, _ = await user.activate(user.lic["key"])
    assert st == 200
    await admin.post(f"/licences/{lid}/reset-device", {})
    assert (await user.session())[0] == 401
    st, j = await admin.post(f"/licences/{lid}/regenerate-key", {})
    new_key = j["key"]
    st, _ = await user.activate(user.lic["key"])
    assert st == 400 and _["d"]["code"] == "bad_key"                 # the old key is gone
    st, _ = await user.activate(new_key)
    assert st == 200


async def test_a_token_that_has_run_out_cannot_be_used(server, user, cloud):
    cloud.now = lambda: time.time() + 8 * 86400
    assert (await user.session())[0] == 401


async def test_a_forged_token_is_refused(server, user, make_device, cloud):
    lid = user.lic["id"]
    forged = crypto.token_make(b"\x00" * 32, {"l": lid, "d": cloud.db.one("SELECT device_id FROM licences WHERE id=?", (lid,))["device_id"], "e": 1, "exp": time.time() + 99999})
    me = make_device()
    me.signer = user.signer
    me.token = forged
    assert (await me.session())[0] == 401
    flip = list(user.token)
    flip[-3] = "A" if flip[-3] != "A" else "B"
    me.token = "".join(flip)
    assert (await me.session())[0] == 401


async def test_guessing_keys_is_stopped_after_five_wrong_ones(server, cloud, make_device):
    lic = cloud.create_licence("Victim", credits=5, actor="t")
    dev = make_device()
    seen = []
    for i in range(8):
        st, j = await dev.activate("BF-AAAA-BBBB-CCCC-%04d" % i)
        seen.append(st)
    assert 429 in seen and seen.index(429) <= 6
    st, j = await dev.activate(lic["key"])                              # even the RIGHT key waits: the attacker learns nothing from the lock
    assert st == 429


async def test_one_key_binds_to_one_computer_even_when_two_try_at_the_same_moment(server, cloud, make_device):
    lic = cloud.create_licence("Shared", credits=5, actor="t")
    a, b = make_device(name="PC-A", macs=("3c:52:82:00:00:01",)), make_device(name="PC-B", macs=("3c:52:82:00:00:02",))
    (sa, ja), (sb, jb) = await asyncio.gather(a.activate(lic["key"]), b.activate(lic["key"]))
    assert sorted([sa, sb]) == [200, 403], (sa, sb)
    winner = a if sa == 200 else b
    loser = b if winner is a else a
    assert (await winner.session())[0] == 200
    assert (await loser.session())[0] == 401
    row = cloud.db.one("SELECT flagged FROM licences WHERE id=?", (lic["id"],))
    assert row["flagged"] == 1 and cloud.events(kind="device_refused")


async def test_a_mac_locked_key_refuses_every_other_computer(server, cloud, make_device):
    lic = cloud.create_licence("Locked", credits=5, mac_lock="3C:52:82:AA:BB:CC", actor="t")
    other = make_device(name="OTHER", macs=("11:22:33:44:55:66",))
    st, j = await other.activate(lic["key"])
    assert st == 403 and j["d"]["code"] == "mac_lock"
    liar = make_device(name="LIAR", macs=("3c:52:82:aa:bb:cc",))           # says the right address: that part cannot be proven (see the threat model)
    assert (await liar.activate(lic["key"]))[0] == 200


async def test_the_same_key_seen_from_two_places_at_once_is_flagged(server, user, cloud):
    """The key was shared: the device key was copied to another computer, so the server cannot tell them apart - but it can see two addresses taking turns."""
    ips = ["198.51.100.1", "203.0.113.7"] * 4
    server.bf.cfg.behind_proxy = True
    for ip in ips:
        body = {"build": 1}
        import hashlib
        nonce = crypto.random_token(12)
        ts = int(time.time() * 1000)
        raw = json.dumps(body).encode()
        msg = "\n".join(["BF1", "POST", "/api/v1/session", str(ts), nonce, hashlib.sha256(raw).hexdigest(), hashlib.sha256(user.token.encode()).hexdigest()]).encode()
        r = await server.post("/api/v1/session", data=raw, headers={"X-BF-Nonce": nonce, "X-BF-Ts": str(ts), "X-BF-Sig": user.signer.sign(msg), "X-BF-Token": user.token,
                                                                       "X-Real-IP": ip, "Content-Type": "application/json"})
        assert r.status == 200
    row = cloud.db.one("SELECT flagged, flag_reason FROM licences WHERE id=?", (user.lic["id"],))
    assert row["flagged"] == 1 and "address" in row["flag_reason"].lower()
    assert cloud.events(kind="sharing_suspected")


# ═══ 7. credits: the thing worth stealing ══════════════════════════════════════════════════════════════════════════════
async def test_a_hundred_bookings_at_once_can_never_spend_more_than_the_balance(server, user, cloud):
    results = await asyncio.gather(*[user.begin(1, ref=f"race-{i:03d}") for i in range(40)])
    ok = [r for r in results if r[0] == 200]
    assert len(ok) <= 3 or cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],)) >= 0
    spent = cloud.db.scalar("SELECT COALESCE(-SUM(delta),0) FROM ledger WHERE licence_id=? AND kind='booking'", (user.lic["id"],))
    bal = cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],))
    assert spent <= 5 and bal == 5 - spent and bal >= 0
    assert cloud.db.scalar("SELECT COUNT(*) FROM bookings WHERE licence_id=?", (user.lic["id"],)) == len(ok)


async def test_the_same_booking_sent_many_times_at_once_is_charged_once(server, user, cloud):
    results = await asyncio.gather(*[user.begin(1, ref="same-ref-1") for _ in range(10)])
    assert all(r[0] in (200, 409) for r in results)
    assert cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],)) == 4


async def test_odd_traveller_counts_never_add_credits_or_get_a_code(server, user, cloud):
    for bad in (0, -1, -9, 10, 100, 2 ** 70, 1.0, 1.5, "1", "9", True, False, None, [1], {"n": 1}, 1e9):
        st, j, _ = await user.begin(bad)
        assert st in (400, 409), (bad, st)
        assert "promo" not in json.dumps(j), bad
    assert cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],)) == 5


async def test_a_reference_cannot_be_used_again_to_get_the_code_for_free(server, user, cloud):
    """The classic: pay once, then send the same reference over and over to be handed the offer code again without paying."""
    st, j, _ = await user.begin(1, ref="my-ref-0001")
    assert st == 200 and j["d"]["promo"]
    bid = j["d"]["booking_id"]
    await user.finish(bid, "done", "ABC123")
    for _ in range(3):
        st, j, _ = await user.begin(1, ref="my-ref-0001")
        assert "promo" not in json.dumps(j), j                             # no second delivery of the code
    assert cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],)) == 4
    # a retry of a booking that is still open (the answer got lost on the way) is fine - but only a couple of times
    st, j, _ = await user.begin(1, ref="my-ref-0002")
    delivered = 1 if st == 200 else 0
    for _ in range(8):
        st, j, _ = await user.begin(1, ref="my-ref-0002")
        delivered += 1 if st == 200 and j["d"].get("promo") else 0
    assert delivered <= 3


async def test_nobody_can_report_on_somebody_elses_booking(server, user, make_device, cloud):
    other_lic = cloud.create_licence("Other", credits=5, actor="t")
    other = make_device(name="OTHER-PC", macs=("3c:52:82:00:00:09",))
    await other.activate(other_lic["key"])
    st, j, _ = await user.begin(1)
    bid = j["d"]["booking_id"]
    st, j, _ = await other.finish(bid, "failed", None, "x")
    assert st == 404
    assert cloud.db.one("SELECT status FROM bookings WHERE id=?", (bid,))["status"] == "reserved"
    st, j, _ = await other.call("/api/v1/booking/open", {})
    assert bid not in json.dumps(j)


async def test_a_finished_booking_cannot_be_flipped_to_get_a_refund_or_a_second_refund(server, user, cloud):
    st, j, _ = await user.begin(2)
    bid = j["d"]["booking_id"]
    assert (await user.finish(bid, "done", "ABC123"))[0] == 200
    st, j, _ = await user.finish(bid, "failed", None, "changed my mind")
    assert j["d"]["refunded"] is False and cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],)) == 3
    st, j, _ = await user.begin(2, ref="second-one")
    bid2 = j["d"]["booking_id"]
    for _ in range(3):
        await user.finish(bid2, "failed", None, "no seats")
    assert cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],)) == 3        # refunded exactly once (1 + 2 charged, 2 back)
    st, j, _ = await user.finish(bid2, "done", "ZZZ999")
    assert cloud.db.one("SELECT status FROM bookings WHERE id=?", (bid2,))["status"] == "failed"


async def test_failing_on_purpose_to_get_free_codes_runs_out_and_is_flagged(server, cloud, make_device):
    lic = cloud.create_licence("Cheat", credits=60, actor="t")
    cheat = make_device(name="CHEAT-PC", macs=("3c:52:82:00:00:0a",))
    await cheat.activate(lic["key"])
    cloud.set_setting("max_open", 100, "t")
    codes_for_free = 0
    for i in range(20):
        st, j, _ = await cheat.begin(1, ref=f"cheat-{i:03d}")
        if st != 200:
            break
        st, j, _ = await cheat.finish(j["d"]["booking_id"], "failed", None, "no seats")
        codes_for_free += 1 if j["d"].get("refunded") else 0
    assert codes_for_free <= int(cloud.setting("refund_daily_limit"))
    row = cloud.db.one("SELECT flagged FROM licences WHERE id=?", (lic["id"],))
    assert row["flagged"] == 1 and cloud.events(kind="refund_denied")


async def test_too_many_bookings_started_in_an_hour_are_refused(server, cloud, make_device):
    lic = cloud.create_licence("Hammer", credits=500, actor="t")
    dev = make_device(name="HAMMER", macs=("3c:52:82:00:00:0b",))
    await dev.activate(lic["key"])
    cloud.set_setting("max_open", 1000, "t")
    cloud.set_setting("begin_per_hour", 5, "t")
    seen = [(await dev.begin(1, ref=f"h-{i:03d}"))[0] for i in range(9)]
    assert seen.count(200) == 5 and 429 in seen


async def test_the_offer_codes_never_travel_except_in_the_answer_to_a_paid_booking(server, user, cloud, admin):
    codes = [v["code"] for v in cloud.setting("offers").values()]
    texts = []
    for path, data in (("/api/v1/session", {"build": 1}), ("/api/v1/booking/open", {}), ("/api/v1/update/check", {"build": 1})):
        st, j, _ = await user.call(path, data)
        texts.append(json.dumps(j))
    st, j = await admin.get("/licences")
    for t in texts:
        assert not any(c in t for c in codes), t[:200]
    st, j, _ = await user.begin(1)
    assert j["d"]["promo"] in codes                                       # ... and here it is, once, because a credit was taken first
    ev = json.dumps(cloud.events("", "", 500))
    assert not any(c in ev for c in codes)                                # not in the audit trail either


async def test_a_blocked_or_expired_licence_gets_no_code(server, user, cloud, admin):
    lid = user.lic["id"]
    await admin.post(f"/licences/{lid}/block", {"reason": "x"})
    st, j, _ = await user.begin(1)
    assert st in (401, 403) and "promo" not in json.dumps(j)
    await admin.post(f"/licences/{lid}/unblock", {})
    await user.activate(user.lic["key"])
    await admin.patch(f"/licences/{lid}", {"expires_days": 1})
    cloud.now = lambda: time.time() + 3 * 86400
    st, j, _ = await user.begin(1)
    assert st in (401, 403) and "promo" not in json.dumps(j)


async def test_maintenance_and_old_versions_stop_new_bookings_without_charging(server, user, cloud):
    cloud.set_setting("maintenance", True, "t")
    st, j, _ = await user.begin(1)
    assert st == 503 and cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],)) == 5
    cloud.set_setting("maintenance", False, "t")
    cloud.set_setting("min_build", 50, "t")
    st, j, _ = await user.begin(1)
    assert st == 426 and cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (user.lic["id"],)) == 5


# ═══ 8. updates: nobody gets a program they should not ═════════════════════════════════════════════════════════════════
def _release(cloud, signer, build=1, files=None):
    data, key, manifest = bundle.build(files or {"a.py": b"x = 1\n"}, build_no=build, version=f"1.0.{build}", channel="stable", notes="", signing_key=signer._k)
    return data, crypto.b64e(key)


async def test_a_release_not_signed_by_the_developer_is_refused_even_from_an_owner(server, admin, cloud):
    evil = crypto.Signer.generate()
    data, key = _release(cloud, evil)
    r = await server.post("/admin/api/releases", data=data, headers={"X-CSRF": admin.csrf, "X-Release-Key": key})
    assert r.status == 400 and cloud.events(kind="release_refused")
    assert cloud.db.scalar("SELECT COUNT(*) FROM releases") == 0


async def test_release_files_and_keys_go_only_to_activated_computers_and_only_for_published_builds(server, admin, cloud, user, release_signer):
    data, key = _release(cloud, release_signer, 1)
    r = await server.post("/admin/api/releases", data=data, headers={"X-CSRF": admin.csrf, "X-Release-Key": key})
    assert r.status == 200
    st, j, _ = await user.call("/api/v1/update/download", {"build": 1}, verify=False)
    assert st in (403, 404)                                              # still a draft: nobody can fetch it
    st, j, _ = await user.call("/api/v1/update/key", {"build": 1, "epk": crypto.b64e(os.urandom(32))})
    assert st in (403, 404) and "wrapped" not in json.dumps(j) and "ct" not in j.get("d", {})
    await admin.patch("/releases/1", {"state": "live"})
    st, blob, _ = await user.call("/api/v1/update/download", {"build": 1}, verify=False)
    assert st == 200 and blob == data
    ghost = Device(server, cloud.identity.public_b64)                    # a computer that never activated
    st, j, _ = await ghost.call("/api/v1/update/download", {"build": 1}, token="", verify=False)
    assert st == 401
    await admin.post(f"/licences/{user.lic['id']}/block", {"reason": "x"})
    st, j, _ = await user.call("/api/v1/update/key", {"build": 1, "epk": crypto.b64e(os.urandom(32))})
    assert st in (401, 403)                                              # a blocked user's program can no longer be opened


async def test_a_bad_ephemeral_key_is_refused_not_a_crash(server, admin, cloud, user, release_signer):
    data, key = _release(cloud, release_signer, 1)
    await server.post("/admin/api/releases", data=data, headers={"X-CSRF": admin.csrf, "X-Release-Key": key})
    await admin.patch("/releases/1", {"state": "live"})
    for epk in (None, 5, "", "!!!", crypto.b64e(b"short"), crypto.b64e(b"\x00" * 32), crypto.b64e(os.urandom(64)), "A" * 5000):
        st, j, _ = await user.call("/api/v1/update/key", {"build": 1, "epk": epk})
        assert st in (400, 403), (epk, st)


# ═══ 9. the server proves who it is ════════════════════════════════════════════════════════════════════════════════════
async def test_every_signed_answer_is_bound_to_the_request_it_answers(server, user):
    """An old good answer replayed to a later question must not verify: the nonce and the path are inside the signature."""
    st, a, _ = await user.session()
    old = a
    nonce = crypto.random_token(12)
    st, b, _ = await user.call("/api/v1/session", {"build": 1}, nonce=nonce)
    assert b["n"] == nonce and old["n"] != nonce
    core = {k: old[k] for k in ("ok", "d", "ts", "n", "p")}
    core["n"] = nonce                                                      # the attacker swaps in the new nonce
    assert not crypto.verify(user.server_pub, crypto.canonical(core), old["sig"])
    core = {k: b[k] for k in ("ok", "d", "ts", "n", "p")}
    core["d"] = {**core["d"], "account": {**core["d"]["account"], "credits": 999}}
    assert not crypto.verify(user.server_pub, crypto.canonical(core), b["sig"])        # a changed credit count breaks it


async def test_the_server_never_signs_for_a_stranger(server):
    """/hello signs only the caller's nonce: it cannot be abused to make the server sign a message of the attacker's choosing."""
    r = await server.post("/api/v1/hello", headers={"X-BF-Nonce": "n" * 12})
    j = await r.json()
    assert j["p"] == "/api/v1/hello" and j["d"] == {"server": "bestfare", "pub": j["d"]["pub"]}
    r = await server.post("/api/v1/hello", headers={"X-BF-Nonce": "{\"ok\":true}" + "x" * 10})
    assert r.status in (200, 400)
    if r.status == 200:
        j = await r.json()
        assert j["p"] == "/api/v1/hello"                                  # the path inside the signature is the real one, whatever the nonce says


async def test_an_expired_or_blocked_licence_cannot_fetch_the_program_or_its_key(server, admin, cloud, user, release_signer):
    data, key = _release(cloud, release_signer, 1)
    await server.post("/admin/api/releases", data=data, headers={"X-CSRF": admin.csrf, "X-Release-Key": key})
    await admin.patch("/releases/1", {"state": "live"})
    epk = crypto.new_ephemeral()[1]
    assert (await user.call("/api/v1/update/key", {"build": 1, "epk": epk}))[0] == 200
    await admin.patch(f"/licences/{user.lic['id']}", {"expires_days": 1})
    cloud.now = lambda: time.time() + 3 * 86400                     # the licence has run out, the session token has not yet
    for path, body in (("/api/v1/update/key", {"build": 1, "epk": epk}), ("/api/v1/update/download", {"build": 1}), ("/api/v1/update/check", {"build": 0})):
        st, j, _ = await user.call(path, body, verify=False)
        assert st == 403, (path, st)


async def test_a_flood_of_made_up_tokens_shuts_that_address_out_for_a_while(server, make_device):
    dev = make_device()
    seen = []
    for i in range(40):
        st, _j, _ = await dev.call("/api/v1/session", {}, token=f"fake.token{i}", verify=False)
        seen.append(st)
    assert seen[0] == 401 and 429 in seen and seen.index(429) <= 31


async def test_the_server_opens_no_file_the_caller_names_anywhere(server):
    """Everything the web side reads from disk is on a fixed list: no URL, header or body field is ever used as a file name."""
    src = (Path(__file__).resolve().parents[1] / "bfcloud" / "web.py").read_text(encoding="utf-8")
    opens = re.findall(r"(?:read_bytes|read_text|open)\(", src)
    # the panel's page, its fixed static list, the public install page + its css, the upload's own random temp file (+ the "api_open" handler's name): 7
    assert len(opens) <= 7, opens
    lines = [l for l in src.splitlines() if re.search(r"(?:read_bytes|read_text|open)\(", l) and "def " not in l]
    for l in lines:
        assert not re.search(r"match_info|request\.|rel_url|\.query|\.path", l), l                              # no name from the caller reaches a file call
    assert 'open(tmp, "wb")' in src and "tmp = folder / f\".upload-{crypto.random_token(8)}\"" in src            # the upload's file name is made by the server


# ═══ 10. the server's own secrets ═══════════════════════════════════════════════════════════════════════════════════════
def test_a_server_that_lost_its_secrets_refuses_to_start_instead_of_inventing_new_ones(tmp_path):
    from bfcloud.service import Cloud
    c = Cloud(tmp_path / "d")
    lic = c.create_licence("Rahul", credits=1, actor="t")
    identity = c.identity.public_b64
    c.close()
    for name in ("master.key", "identity.pem"):
        keep = (tmp_path / "d" / name).read_bytes()
        (tmp_path / "d" / name).unlink()
        with pytest.raises(RuntimeError) as e:
            Cloud(tmp_path / "d")
        assert name in str(e.value)
        assert not (tmp_path / "d" / name).exists()                     # and nothing was invented while refusing
        (tmp_path / "d" / name).write_bytes(keep)
    c2 = Cloud(tmp_path / "d")                                          # with everything back it is the same server again
    assert c2.identity.public_b64 == identity and c2.reveal_key(lic["id"], "t") == lic["key"]
    c2.close()


def test_a_damaged_master_key_is_never_silently_replaced(tmp_path):
    from bfcloud.service import Cloud
    Cloud(tmp_path / "d").close()
    (tmp_path / "d" / "master.key").write_bytes(b"short")
    with pytest.raises(ValueError):
        Cloud(tmp_path / "d")
    assert (tmp_path / "d" / "master.key").read_bytes() == b"short"


def test_a_backup_is_a_whole_working_server_and_keeps_its_secrets_private(tmp_path):
    from bfcloud import __main__ as cli
    from bfcloud.service import Cloud
    c = Cloud(tmp_path / "live")
    c.create_admin("boss", OWNER_PW, "owner", actor="t")
    lic = c.create_licence("Rahul", credits=4, actor="t")
    assert cli.backup(tmp_path / "live", tmp_path / "bak") == 0                      # (taken while the server still has the database open)
    c.close()
    restored = Cloud(tmp_path / "bak")
    assert restored.identity.public_b64 == c.identity.public_b64                    # the SAME identity: installed apps keep trusting it
    assert restored.reveal_key(lic["id"], "t") == lic["key"] and restored.db.scalar("SELECT credits FROM licences") == 4
    restored.close()
    if sys.platform != "win32":
        for n in ("identity.pem", "master.key"):
            assert ((tmp_path / "bak" / n).stat().st_mode & 0o077) == 0


async def test_a_computer_signing_in_by_itself_cannot_take_back_a_key_the_admin_moved(server, user, cloud, admin):
    """After 'Reset computer' the old computer still remembers the key. Whoever called first used to win - the old one could take it back."""
    lid = user.lic["id"]
    await admin.post(f"/licences/{lid}/reset-device", {})

    async def activate(dev, auto):
        return await dev.call("/api/v1/activate", {"key": user.lic["key"], "device_pub": dev.signer.public_b64, "device": dev.info(), "build": 1, **({"auto": True} if auto else {})}, token="")

    st, j, _ = await activate(user, auto=True)
    assert st == 403 and j["d"]["code"] == "rebind_needed"                       # refused; nothing was bound
    assert cloud.db.one("SELECT device_pub FROM licences WHERE id=?", (lid,))["device_pub"] is None
    st, j, _ = await activate(user, auto=False)                                    # a person typing the key does bind it
    assert st == 200
    st, j, _ = await activate(user, auto=True)                                     # and from then on the SAME computer may sign in again by itself (a token ran out)
    assert st == 200
    other = Device(server, cloud.identity.public_b64, name="OTHER-PC", macs=("3c:52:82:00:00:77",))
    st, j, _ = await activate(other, auto=True)
    assert st == 403 and j["d"]["code"] == "device_mismatch"                       # another computer is still refused, with or without "auto"
