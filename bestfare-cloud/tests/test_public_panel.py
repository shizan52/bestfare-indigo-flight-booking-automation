"""The admin panel reachable on the internet (over https through nginx) AND through the SSH tunnel (plain http on the owner's computer): its cookie and headers follow how the request really arrived."""
import pytest
from aiohttp.test_utils import TestClient, TestServer

from bfcloud.web import App, Config
from conftest import OWNER_PW


async def login(client, headers=None):
    return await client.post("/admin/api/login", json={"username": "boss", "password": OWNER_PW}, headers=headers or {})


@pytest.fixture()
async def behind_nginx(cloud):
    app = App(cloud, Config(data_dir=cloud.dir, behind_proxy=True, public_url="https://bestfare.example.com"))
    client = TestClient(TestServer(app.app))
    await client.start_server()
    yield client
    await client.close()


async def test_over_https_through_nginx_the_cookie_is_secure_and_hsts_is_sent(behind_nginx):
    r = await login(behind_nginx, {"X-Forwarded-Proto": "https", "X-Real-IP": "203.0.113.7"})
    assert r.status == 200
    cookie = r.headers["Set-Cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "path=/admin" in cookie
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")


async def test_through_the_plain_http_tunnel_the_cookie_still_works_without_secure(behind_nginx):
    r = await login(behind_nginx)                                         # (no X-Forwarded-Proto: it came through the tunnel, not through nginx)
    assert r.status == 200 and "secure" not in r.headers["Set-Cookie"].lower() and "Strict-Transport-Security" not in r.headers


async def test_a_server_that_is_not_behind_nginx_does_not_believe_the_header(server):
    r = await login(server, {"X-Forwarded-Proto": "https"})                # (anybody can send that header: it counts only from nginx on this machine)
    assert r.status == 200 and "secure" not in r.headers["Set-Cookie"].lower() and "Strict-Transport-Security" not in r.headers


async def test_the_panel_page_and_its_files_are_served_and_carry_the_security_headers(behind_nginx):
    for path in ("/admin/", "/admin/static/app.js", "/admin/static/style.css"):
        r = await behind_nginx.get(path, headers={"X-Forwarded-Proto": "https"})
        assert r.status == 200, path
        assert "default-src 'self'" in r.headers["Content-Security-Policy"] and r.headers["X-Frame-Options"] == "DENY" and r.headers["Server"] == "bestfare"
    assert (await behind_nginx.get("/admin/static/../../identity.pem")).status == 404
