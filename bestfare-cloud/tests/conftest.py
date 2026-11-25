"""Fixtures: a real server (in-process, on a free port) and a stand-in for the app that speaks the protocol with its own device key."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bfcloud import crypto  # noqa: E402
from bfcloud.service import Cloud  # noqa: E402
from bfcloud.web import App, Config  # noqa: E402

OWNER_PW = "Correct-Horse-9-Battery"


@pytest.fixture()
def release_signer():
    return crypto.Signer.generate()


@pytest.fixture()
def cloud(tmp_path, release_signer):
    c = Cloud(tmp_path / "data", release_keys=[release_signer.public_b64])
    c.create_admin("boss", OWNER_PW, "owner", actor="test")
    yield c
    c.close()


@pytest.fixture()
async def server(cloud):
    cfg = Config(data_dir=cloud.dir)
    app = App(cloud, cfg)
    srv = TestServer(app.app)
    client = TestClient(srv)
    await client.start_server()
    client.bf = app
    yield client
    await client.close()


class Device:
    """What the app does, written independently of the app: a device key pair, signed requests, verified answers."""

    def __init__(self, client: TestClient, server_pub: str, name: str = "TEST-PC", macs=("3c:52:82:aa:bb:cc",)):
        self.c, self.server_pub = client, server_pub
        self.signer = crypto.Signer.generate()
        self.token = ""
        self.name, self.macs = name, list(macs)
        self.build = 1

    def info(self) -> dict:
        return {"macs": self.macs, "name": self.name, "os": "Windows 11", "guid": hashlib.sha256(self.name.encode()).hexdigest()[:32], "app": "1.0"}

    async def call(self, path: str, data: dict | None = None, *, token: str | None = None, skew: int = 0, nonce: str | None = None,
                   sign_with=None, tamper_body: bytes | None = None, verify: bool = True):
        body = json.dumps(data or {}).encode()
        tok = self.token if token is None else token
        nonce = nonce or crypto.random_token(12)
        ts = int(time.time() * 1000) + skew
        msg = "\n".join(["BF1", "POST", path, str(ts), nonce, hashlib.sha256(body).hexdigest(), hashlib.sha256(tok.encode()).hexdigest()]).encode()
        sig = (sign_with or self.signer).sign(msg)
        headers = {"X-BF-Nonce": nonce, "X-BF-Ts": str(ts), "X-BF-Sig": sig, "Content-Type": "application/json"}
        if tok:
            headers["X-BF-Token"] = tok
        r = await self.c.post(path, data=tamper_body if tamper_body is not None else body, headers=headers)
        if r.content_type == "application/octet-stream":
            return r.status, await r.read(), r
        j = await r.json()
        if verify and "sig" in j:
            core = {k: j[k] for k in ("ok", "d", "ts", "n", "p")}
            assert crypto.verify(self.server_pub, crypto.canonical(core), j["sig"]), "the answer is not signed by the server"
            assert j["n"] == nonce and j["p"] == path
        return r.status, j, r

    async def activate(self, key: str, **kw):
        st, j, _ = await self.call("/api/v1/activate", {"key": key, "device_pub": self.signer.public_b64, "device": self.info(), "build": self.build}, token="", **kw)
        if st == 200 and j.get("ok"):
            self.token = j["d"]["token"]
        return st, j

    async def session(self):
        return await self.call("/api/v1/session", {"build": self.build})

    async def begin(self, n=1, offer="offer1", route="DEL-BOM", date="2026-12-01", ref=None):
        return await self.call("/api/v1/booking/begin", {"n": n, "offer": offer, "route": route, "date": date, "ref": ref or crypto.random_token(8), "build": self.build})

    async def finish(self, booking_id, outcome="done", pnr="ABC123", reason=""):
        return await self.call("/api/v1/booking/finish", {"booking_id": booking_id, "outcome": outcome, "pnr": pnr, "reason": reason})


@pytest.fixture()
def make_device(server, cloud):
    def make(**kw):
        return Device(server, cloud.identity.public_b64, **kw)
    return make


@pytest.fixture()
async def user(cloud, make_device):
    """An activated device with 5 credits."""
    lic = cloud.create_licence("Rahul Sharma", credits=5, actor="test")
    dev = make_device()
    st, j = await dev.activate(lic["key"])
    assert st == 200, j
    dev.lic = lic
    return dev


async def admin_login(client: TestClient, user="boss", pw=OWNER_PW):
    r = await client.post("/admin/api/login", json={"username": user, "password": pw})
    j = await r.json()
    return r.status, j


@pytest.fixture()
async def admin(server):
    st, j = await admin_login(server)
    assert st == 200, j
    csrf = j["csrf"]

    class A:
        async def get(self, path, **kw):
            r = await server.get("/admin/api" + path, **kw)
            return r.status, await r.json()

        async def post(self, path, data=None, **kw):
            r = await server.post("/admin/api" + path, json=data or {}, headers={"X-CSRF": csrf}, **kw)
            return r.status, await r.json()

        async def patch(self, path, data=None):
            r = await server.patch("/admin/api" + path, json=data or {}, headers={"X-CSRF": csrf})
            return r.status, await r.json()

        async def put(self, path, data=None):
            r = await server.put("/admin/api" + path, json=data or {}, headers={"X-CSRF": csrf})
            return r.status, await r.json()

        async def delete(self, path):
            r = await server.delete("/admin/api" + path, headers={"X-CSRF": csrf})
            return r.status, await r.json()

    a = A()
    a.csrf = csrf
    return a
