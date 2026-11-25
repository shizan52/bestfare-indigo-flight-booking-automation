"""The installer: signed by the developer, hosted by the server, offered to the apps, and given out by one public link."""
import hashlib
import json
import os
import time

import pytest
from aiohttp import ClientSession, DummyCookieJar

from bfcloud import bundle, crypto
from bfcloud.service import CloudError
from conftest import OWNER_PW, admin_login

SIZE = 1_200_000


def make_exe(tmp_path, name="setup.exe", size=SIZE, head=b"MZ"):
    p = tmp_path / name
    p.write_bytes(head + os.urandom(size - len(head)))
    return p


def sign(signer, version, path):
    return bundle.sign_installer(version, hashlib.sha256(path.read_bytes()).hexdigest(), signer._k)


# ── the signature and the version ─────────────────────────────────────────────────────────────────────────────────────
def test_the_version_is_a_number_that_only_goes_up():
    assert bundle.version_number("1.2.3") == 1002003 and bundle.version_number("1.10.0") > bundle.version_number("1.9.9") > bundle.version_number("1.9.8")
    for bad in ("1.2", "1.2.3.4", "a.b.c", "", "1.2.x", "1000.0.0", "-1.0.0", "1.2.3 ", None):
        with pytest.raises(ValueError):
            bundle.version_number(bad)


def test_only_the_developers_signature_over_this_version_and_this_file_counts(release_signer):
    other = crypto.Signer.generate()
    h = "ab" * 32
    good = bundle.sign_installer("1.0.0", h, release_signer._k)
    keys = [release_signer.public_b64]
    assert bundle.verify_installer("1.0.0", h, good, keys)
    assert not bundle.verify_installer("1.0.1", h, good, keys)                       # another version
    assert not bundle.verify_installer("1.0.0", "cd" * 32, good, keys)               # another file
    assert not bundle.verify_installer("1.0.0", h, bundle.sign_installer("1.0.0", h, other._k), keys)    # another signer
    assert not bundle.verify_installer("1.0.0", h, "", keys) and not bundle.verify_installer("nope", h, good, keys) and not bundle.verify_installer("1.0.0", h, "!!!", keys)


# ── the server's store ────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_signed_installer_is_accepted_and_goes_live(cloud, release_signer, tmp_path):
    p = make_exe(tmp_path)
    out = cloud.add_installer(p, "1.0.0", sign(release_signer, "1.0.0", p), "First", "boss")
    assert out["version"] == "1.0.0" and len(out["sha256"]) == 64 and not p.exists()                  # (moved into the server's folder)
    assert cloud.live_installer() is None                                                             # a draft is not offered to anybody
    cloud.edit_installer(out["id"], state="live", actor="boss")
    live = cloud.live_installer()
    assert live["version"] == "1.0.0" and cloud.installer_path(live).read_bytes()[:2] == b"MZ"
    assert cloud.installer_for(0)["available"] and not cloud.installer_for(1_000_000)["available"]
    assert cloud.events(kind="installer_uploaded")


@pytest.mark.parametrize("why", ["unsigned", "other signer", "wrong version", "not a program", "too small", "old version"])
def test_what_is_not_the_developers_exact_file_is_refused(cloud, release_signer, tmp_path, why):
    ok = make_exe(tmp_path, "ok.exe")
    cloud.add_installer(ok, "2.0.0", sign(release_signer, "2.0.0", ok), "", "boss")
    p = make_exe(tmp_path, "x.exe", head=b"ZZ" if why == "not a program" else b"MZ", size=500 if why == "too small" else SIZE)
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    if why == "unsigned":
        sig = ""
    elif why == "other signer":
        sig = bundle.sign_installer("2.1.0", digest, crypto.Signer.generate()._k)
    elif why == "wrong version":
        sig = bundle.sign_installer("9.9.9", digest, release_signer._k)
    else:
        sig = sign(release_signer, "1.0.0" if why == "old version" else "2.1.0", p)
    version = "1.0.0" if why == "old version" else "2.1.0"
    with pytest.raises(CloudError):
        cloud.add_installer(p, version, sig, "", "boss")
    assert cloud.db.scalar("SELECT COUNT(*) FROM installers") == 1
    if why in ("unsigned", "other signer", "wrong version"):
        assert cloud.events(kind="installer_refused")


def test_a_live_installer_cannot_be_deleted_and_a_withdrawn_one_is_not_offered(cloud, release_signer, tmp_path):
    p = make_exe(tmp_path)
    iid = cloud.add_installer(p, "1.0.0", sign(release_signer, "1.0.0", p), "", "boss")["id"]
    cloud.edit_installer(iid, state="live", actor="boss")
    with pytest.raises(CloudError):
        cloud.delete_installer(iid, "boss")
    cloud.edit_installer(iid, state="withdrawn", actor="boss")
    assert cloud.live_installer() is None and not cloud.installer_for(0)["available"]
    cloud.delete_installer(iid, "boss")
    assert cloud.list_installers() == []


def test_the_app_is_told_about_a_newer_installer_with_what_it_needs_to_check_it(cloud, release_signer, tmp_path):
    p = make_exe(tmp_path)
    iid = cloud.add_installer(p, "1.2.0", sign(release_signer, "1.2.0", p), "Faster start", "boss")["id"]
    cloud.edit_installer(iid, state="live", mandatory=True, actor="boss")
    i = cloud.installer_for(1_001_000)
    assert i["available"] and i["version"] == "1.2.0" and i["mandatory"] and i["notes"] == "Faster start" and i["path"] == "/download/BestFare-Setup.exe"
    assert bundle.verify_installer(i["version"], i["sha256"], i["sig"], [release_signer.public_b64])      # the app can check it itself
    for junk in (None, "1", True, 1.5, [1], {"a": 1}):
        assert cloud.installer_for(junk)["available"] is False                                           # nonsense is no news, never a crash


# ── over HTTP ─────────────────────────────────────────────────────────────────────────────────────────────────────────
async def upload(server, admin, path, version, sig, notes="n"):
    return await server.post("/admin/api/installers", data=path.read_bytes(), headers={"X-CSRF": admin.csrf, "X-Installer-Version": version, "X-Installer-Sig": sig,
                                                                                     "X-Installer-Notes": notes, "Content-Type": "application/octet-stream"})


async def test_the_owner_uploads_makes_it_live_and_the_public_link_serves_exactly_that_file(server, admin, release_signer, tmp_path):
    p = make_exe(tmp_path, size=3_000_000)
    r = await upload(server, admin, p, "1.0.0", sign(release_signer, "1.0.0", p))
    j = await r.json()
    assert r.status == 200 and j["ok"] and j["size"] == 3_000_000
    s = await ClientSession(base_url=str(server.make_url("/")), cookie_jar=DummyCookieJar()).__aenter__()
    try:
        page = await s.get("/install")
        assert page.status == 200 and "Coming soon" in await page.text()                              # a draft is not given out
        assert (await s.get("/download/BestFare-Setup.exe")).status == 404
        st, _ = await admin.patch(f"/installers/{j['id']}", {"state": "live"})
        assert st == 200
        page = await s.get("/install")
        text = await page.text()
        assert page.status == 200 and "Download for Windows" in text and "Version 1.0.0" in text and j["sha256"] in text and "<script" not in text
        assert "default-src 'self'" in page.headers["Content-Security-Policy"] and page.headers["X-Content-Type-Options"] == "nosniff"
        dl = await s.get("/download/BestFare-Setup.exe")
        body = await dl.read()
        assert dl.status == 200 and body == p.read_bytes() and hashlib.sha256(body).hexdigest() == j["sha256"]
        assert 'attachment; filename="BestFare-Setup.exe"' in dl.headers["Content-Disposition"] and dl.headers["X-Setup-Version"] == "1.0.0"
        css = await s.get("/download/install.css")
        assert css.status == 200 and css.content_type == "text/css"
        root = await s.get("/", allow_redirects=False)
        assert root.status == 302 and root.headers["Location"] == "/install"
        listing = await (await server.get("/admin/api/installers")).json()
        assert listing["live"] == "1.0.0" and listing["installers"][0]["version"] == "1.0.0"
    finally:
        await s.close()


async def test_an_unsigned_or_foreign_installer_is_refused_over_the_wire_and_leaves_nothing_behind(server, admin, release_signer, tmp_path, cloud):
    p = make_exe(tmp_path)
    r = await upload(server, admin, p, "1.0.0", bundle.sign_installer("1.0.0", "00" * 32, release_signer._k))
    assert r.status == 400 and (await r.json())["code"] == "bad_signature"
    r = await upload(server, admin, p, "1.0.0", "")
    assert r.status == 400
    assert list((cloud.dir / "installers").glob("*")) == [] and cloud.db.scalar("SELECT COUNT(*) FROM installers") == 0


async def test_only_an_owner_may_upload_or_change_installers(server, admin, release_signer, tmp_path, cloud):
    st, j = await admin.post("/admins", {"username": "helper", "role": "admin"})
    await admin.post("/logout")
    st, login = await admin_login(server, "helper", j["password"])
    h = {"X-CSRF": login["csrf"]}
    p = make_exe(tmp_path)
    r = await server.post("/admin/api/installers", data=p.read_bytes(), headers={**h, "X-Installer-Version": "1.0.0", "X-Installer-Sig": sign(release_signer, "1.0.0", p)})
    assert r.status == 403
    assert (await server.patch("/admin/api/installers/1", json={"state": "live"}, headers=h)).status == 403
    assert (await server.delete("/admin/api/installers/1", headers=h)).status == 403
    assert cloud.db.scalar("SELECT COUNT(*) FROM installers") == 0


async def test_the_session_and_the_update_check_carry_the_installer_news(server, admin, release_signer, tmp_path, user, cloud):
    p = make_exe(tmp_path)
    iid = cloud.add_installer(p, "1.1.0", sign(release_signer, "1.1.0", p), "news", "boss")["id"]
    cloud.edit_installer(iid, state="live", actor="boss")
    st, j, _ = await user.call("/api/v1/session", {"build": 1, "shell": 1_000_000})
    assert j["d"]["installer"]["available"] and j["d"]["installer"]["version"] == "1.1.0"
    st, j, _ = await user.call("/api/v1/update/check", {"build": 1, "shell": 1_001_000})
    assert j["d"]["installer"] == {"available": False}
    st, j, _ = await user.call("/api/v1/session", {"build": 1})                                         # an app that does not say its shell version is simply told
    assert j["d"]["installer"]["available"]
    st, j, _ = await user.call("/api/v1/session", {"build": 1, "shell": "x"})
    assert st == 200 and j["d"]["installer"]["available"] in (True, False)


async def test_the_download_is_limited_per_address_and_never_serves_other_files(server, admin, release_signer, tmp_path, cloud):
    p = make_exe(tmp_path)
    iid = cloud.add_installer(p, "1.0.0", sign(release_signer, "1.0.0", p), "", "boss")["id"]
    cloud.edit_installer(iid, state="live", actor="boss")
    s = ClientSession(base_url=str(server.make_url("/")), cookie_jar=DummyCookieJar())
    try:
        for bad in ("/download/../identity.pem", "/download/identity.pem", "/download/BestFare-Setup-1.0.0.exe", "/download/cloud.db", "/download/%2e%2e/master.key"):
            r = await s.get(bad)
            assert r.status in (404, 400) and b"BEGIN" not in await r.read(), bad
        codes = [(await s.get("/download/BestFare-Setup.exe")).status for _ in range(33)]
        assert codes[0] == 200 and 429 in codes and codes.index(429) <= 31
    finally:
        await s.close()
