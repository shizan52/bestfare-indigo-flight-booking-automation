"""The HTTP side: one aiohttp application with three faces.

  /api/v1/...      what the Best Fare APP talks to. Every request is signed by the device's own key (and carries a session token);
                   every answer is signed by the server's identity key. Nothing is trusted because of where it came from.
  /admin/api/...   what the ADMIN PANEL talks to (cookie session + CSRF token + optional second factor).
  /admin/          the admin panel itself (static files).
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from . import bundle, crypto
from .service import Cloud, CloudError, clean

log = logging.getLogger("bfcloud")
WEB_DIR = Path(__file__).resolve().parent / "web"
CLOCK_SKEW_S = 120
NONCE_KEEP_S = 600
ADMIN_IDLE_S = 2 * 3600
ADMIN_MAX_S = 12 * 3600
MAX_BODY = 256 * 1024
MAX_BUNDLE_BODY = 46 * 1024 * 1024
COOKIE = "bf_admin"


@dataclass
class Config:
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8780
    behind_proxy: bool = False                 # True when nginx is in front: trust X-Real-IP from 127.0.0.1 only
    secure_cookies: bool = False               # True when the panel is served over https
    release_keys: list[str] = field(default_factory=list)
    public_url: str = ""                       # where the world reaches this server (https://host:port): the panel shows the install link built from it


# ── rate limits ─────────────────────────────────────────────────────────────────────────────────────────────────────
class Limiter:
    """A sliding window per key. `hit()` counts a try; `blocked()` tells whether a key has used its tries up."""

    def __init__(self, now: Callable[[], float] = time.monotonic):
        self.now = now
        self._hits: dict[str, deque] = {}
        self._locked: dict[str, float] = {}

    def _prune(self, key: str, window: float) -> deque:
        q = self._hits.setdefault(key, deque())
        cut = self.now() - window
        while q and q[0] < cut:
            q.popleft()
        if not q:
            self._hits.pop(key, None)
            q = self._hits.setdefault(key, deque())
        if len(self._hits) > 20000:                       # (a flood of different keys must not eat the memory)
            for k in list(self._hits)[:5000]:
                self._hits.pop(k, None)
        return q

    def hit(self, key: str, limit: int, window: float) -> bool:
        """Count one try. False = over the limit (the try is not allowed)."""
        q = self._prune(key, window)
        if len(q) >= limit:
            return False
        q.append(self.now())
        return True

    def lock(self, key: str, seconds: float) -> None:
        self._locked[key] = self.now() + seconds

    def locked(self, key: str) -> float:
        until = self._locked.get(key, 0.0)
        if until <= self.now():
            self._locked.pop(key, None)
            return 0.0
        return until - self.now()

    def count(self, key: str, window: float) -> int:
        return len(self._prune(key, window))


def err_response(e: CloudError) -> web.Response:
    return web.json_response({"ok": False, "code": e.code, "message": e.message, **e.extra}, status=e.status)


def _hdr(extra: Optional[dict] = None) -> dict:
    h = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    h.update(extra or {})
    return h


class App:
    def __init__(self, cloud: Cloud, cfg: Config):
        self.cloud, self.cfg = cloud, cfg
        self.limiter = Limiter()
        self._nonces: dict[str, float] = {}
        self._dummy_hash = crypto.password_hash("not-a-real-password-0")   # (checked against when a username does not exist: one scrypt either way)
        self.app = web.Application(client_max_size=MAX_BUNDLE_BODY, middlewares=[self._security, self._errors])
        self._routes()
        self.app.on_startup.append(self._start_tasks)
        self.app.on_cleanup.append(self._stop_tasks)

    # ── plumbing ───────────────────────────────────────────────────────────────────────────────────────────────────
    def ip(self, request: web.Request) -> str:
        if self.cfg.behind_proxy and request.remote in ("127.0.0.1", "::1"):
            return clean(request.headers.get("X-Real-IP") or request.remote or "", 64)
        return clean(request.remote or "", 64)

    def https(self, request: web.Request) -> bool:
        """Did this request arrive over https? Configured for a server that terminates https itself, or - behind nginx on this machine - what nginx says it was
        (a request from anywhere else cannot claim it: the header is believed only from 127.0.0.1). The panel is reachable over https on the internet AND over the
        plain-http SSH tunnel on this computer: its cookie is marked Secure only in the first case, so the tunnel keeps working."""
        if self.cfg.secure_cookies:
            return True
        return bool(self.cfg.behind_proxy and request.remote in ("127.0.0.1", "::1") and request.headers.get("X-Forwarded-Proto") == "https")

    @web.middleware
    async def _security(self, request: web.Request, handler):
        upload = request.path in ("/admin/api/releases", "/admin/api/installers") and request.method == "POST"
        if not upload and (request.content_length or 0) > MAX_BODY:
            return web.json_response({"ok": False, "code": "too_large", "message": "That request is too large."}, status=413)
        refused: Optional[web.HTTPException] = None
        try:
            resp = await handler(request)
        except web.HTTPException as e:                              # (a 404 or a 405 is an answer too: it gets the same headers)
            refused = resp = e
        resp.headers["Server"] = "bestfare"                         # (not the software and its version: that only helps an attacker)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'self'")
        if self.https(request):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.path.startswith(("/api/", "/admin/api/")):
            resp.headers["Cache-Control"] = "no-store"
        if refused is not None:
            raise refused
        return resp

    @web.middleware
    async def _errors(self, request: web.Request, handler):
        try:
            return await handler(request)
        except CloudError as e:
            return err_response(e)
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001 - never show a traceback to the outside
            log.exception("unexpected error on %s %s", request.method, request.path)
            return web.json_response({"ok": False, "code": "server_error", "message": "Something went wrong. Please try again."}, status=500)

    async def _start_tasks(self, _app) -> None:
        self._task = asyncio.ensure_future(self._housekeeping())

    async def _stop_tasks(self, _app) -> None:
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                now = time.time()
                self._nonces = {n: t for n, t in self._nonces.items() if now - t < NONCE_KEEP_S}
                self.cloud.expire_reservations()
                self.cloud.db.run("DELETE FROM admin_sessions WHERE expires < ?", (now,))
            except Exception:  # noqa: BLE001
                log.exception("housekeeping failed")

    # ── signed answers ─────────────────────────────────────────────────────────────────────────────────────────────
    def signed(self, request: web.Request, data: dict, ok: bool = True, status: int = 200) -> web.Response:
        nonce = request.headers.get("X-BF-Nonce", "")
        env = {"ok": ok, "d": data, "ts": int(time.time() * 1000), "n": nonce, "p": request.path}
        env["sig"] = self.cloud.identity.sign(crypto.canonical({k: env[k] for k in ("ok", "d", "ts", "n", "p")}))
        return web.json_response(env, status=status, headers=_hdr())

    def signed_error(self, request: web.Request, e: CloudError) -> web.Response:
        return self.signed(request, {"code": e.code, "message": e.message, **e.extra}, ok=False, status=e.status)

    # ── the app API ────────────────────────────────────────────────────────────────────────────────────────────────
    def _routes(self) -> None:
        r = self.app.router
        r.add_get("/healthz", self.healthz)
        r.add_post("/api/v1/hello", self.api_hello)
        r.add_post("/api/v1/activate", self.api_activate)
        r.add_post("/api/v1/session", self.api_session)
        r.add_post("/api/v1/booking/begin", self.api_begin)
        r.add_post("/api/v1/booking/finish", self.api_finish)
        r.add_post("/api/v1/booking/open", self.api_open)
        r.add_post("/api/v1/update/check", self.api_update_check)
        r.add_post("/api/v1/update/download", self.api_update_download)
        r.add_post("/api/v1/update/key", self.api_update_key)
        # admin
        a = "/admin/api"
        r.add_post(f"{a}/login", self.ad_login)
        r.add_post(f"{a}/logout", self.ad_logout)
        r.add_get(f"{a}/me", self.ad_me)
        r.add_get(f"{a}/stats", self.ad_stats)
        r.add_get(f"{a}/licences", self.ad_list)
        r.add_post(f"{a}/licences", self.ad_create)
        r.add_get(f"{a}/licences/{{lid}}", self.ad_detail)
        r.add_patch(f"{a}/licences/{{lid}}", self.ad_edit)
        r.add_delete(f"{a}/licences/{{lid}}", self.ad_delete)
        r.add_post(f"{a}/licences/{{lid}}/credits", self.ad_credits)
        r.add_post(f"{a}/licences/{{lid}}/block", self.ad_block)
        r.add_post(f"{a}/licences/{{lid}}/unblock", self.ad_unblock)
        r.add_post(f"{a}/licences/{{lid}}/reset-device", self.ad_reset_device)
        r.add_post(f"{a}/licences/{{lid}}/reveal-key", self.ad_reveal)
        r.add_post(f"{a}/licences/{{lid}}/regenerate-key", self.ad_regen)
        r.add_post(f"{a}/bookings/{{bid}}/refund", self.ad_refund)
        r.add_get(f"{a}/events", self.ad_events)
        r.add_get(f"{a}/settings", self.ad_settings)
        r.add_put(f"{a}/settings", self.ad_settings_put)
        r.add_get(f"{a}/releases", self.ad_releases)
        r.add_post(f"{a}/releases", self.ad_release_upload)
        r.add_patch(f"{a}/releases/{{build}}", self.ad_release_edit)
        r.add_delete(f"{a}/releases/{{build}}", self.ad_release_delete)
        r.add_get(f"{a}/admins", self.ad_admins)
        r.add_post(f"{a}/admins", self.ad_admin_create)
        r.add_patch(f"{a}/admins/{{aid}}", self.ad_admin_edit)
        r.add_post(f"{a}/me/password", self.ad_password)
        r.add_post(f"{a}/me/totp/setup", self.ad_totp_setup)
        r.add_post(f"{a}/me/totp/enable", self.ad_totp_enable)
        r.add_post(f"{a}/me/totp/disable", self.ad_totp_disable)
        r.add_get(f"{a}/installers", self.ad_installers)
        r.add_post(f"{a}/installers", self.ad_installer_upload)
        r.add_patch(f"{a}/installers/{{iid}}", self.ad_installer_edit)
        r.add_delete(f"{a}/installers/{{iid}}", self.ad_installer_delete)
        r.add_get("/", self.pg_root)
        r.add_get("/install", self.pg_install)
        r.add_get("/download/BestFare-Setup.exe", self.pg_download)
        r.add_get("/download/install.css", self.pg_css)
        r.add_get("/admin", self.ad_index)
        r.add_get("/admin/", self.ad_index)
        r.add_get("/admin/static/{name}", self.ad_static)

    async def healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    @staticmethod
    async def _read_body(request: web.Request, limit: int = MAX_BODY) -> bytes:
        """The body, never more than `limit` bytes: a client that does not say how long its body is (chunked) cannot make the server swallow it."""
        if request.content_length is not None and request.content_length > limit:
            raise CloudError("too_large", "That request is too large.", 413)
        buf = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            buf += chunk
            if len(buf) > limit:
                raise CloudError("too_large", "That request is too large.", 413)
        return bytes(buf)

    async def _json(self, request: web.Request) -> tuple[dict, bytes]:
        body = await self._read_body(request)
        if not body:
            return {}, body
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise CloudError("bad_request", "The request could not be read.")
        if not isinstance(data, dict):
            raise CloudError("bad_request", "The request could not be read.")
        return data, body

    def _remember_nonce(self, nonce: str) -> None:
        if len(self._nonces) > 200_000:                                       # (a flood must not eat the memory: the oldest half goes)
            for n, _t in sorted(self._nonces.items(), key=lambda kv: kv[1])[:100_000]:
                self._nonces.pop(n, None)
        self._nonces[nonce] = time.time()

    def _check_envelope(self, request: web.Request) -> tuple[str, str, str, int]:
        nonce = request.headers.get("X-BF-Nonce", "")
        sig = request.headers.get("X-BF-Sig", "")
        try:
            ts = int(request.headers.get("X-BF-Ts", ""))
        except ValueError:
            raise CloudError("bad_request", "The request is not valid.")
        if not (8 <= len(nonce) <= 40) or not nonce.replace("-", "").replace("_", "").isalnum() or not sig:
            raise CloudError("bad_request", "The request is not valid.")
        now_ms = int(time.time() * 1000)
        if abs(now_ms - ts) > CLOCK_SKEW_S * 1000:
            raise CloudError("clock", "The clock of this computer is wrong. Please correct the date and time.", 400, server_time=now_ms)
        if nonce in self._nonces:
            raise CloudError("replay", "That request was already used.", 409)
        return nonce, sig, "", ts

    @staticmethod
    def signing_message(method: str, path: str, ts: int, nonce: str, body: bytes, token: str) -> bytes:
        return "\n".join(["BF1", method, path, str(ts), nonce, hashlib.sha256(body).hexdigest(),
                          hashlib.sha256(token.encode()).hexdigest()]).encode()

    async def _device(self, request: web.Request):
        """-> (licence row, parsed body). Everything that proves the caller is the activated device happens here."""
        ip = self.ip(request)
        if self.limiter.locked(f"bad:{ip}"):
            raise CloudError("rate_limited", "Too many requests. Please wait a moment.", 429)
        nonce, sig, _x, ts = self._check_envelope(request)
        token = request.headers.get("X-BF-Token", "")
        if not self.limiter.hit(f"ip:{ip}", 600, 60):
            raise CloudError("rate_limited", "Too many requests. Please wait a moment.", 429)
        try:
            row, _payload = self.cloud.authorize(token)
        except CloudError:
            self.limiter.hit(f"bad:{ip}", 30, 600)
            if self.limiter.count(f"bad:{ip}", 600) >= 30:                   # thirty answers of "who are you?" in ten minutes: this address waits five
                self.limiter.lock(f"bad:{ip}", 300)
            raise
        if not self.limiter.hit(f"lic:{row['id']}", 240, 60):
            raise CloudError("rate_limited", "Too many requests. Please wait a moment.", 429)
        data, body = await self._json(request)
        msg = self.signing_message(request.method, request.path, ts, nonce, body, token)
        if not crypto.verify(row["device_pub"], msg, sig):
            self.cloud.event("bad_signature", licence_id=row["id"], ip=ip, detail=request.path, level="alert")
            raise CloudError("unauthorized", "Please sign in again.", 401)
        self._remember_nonce(nonce)
        return row, data

    async def api_hello(self, request: web.Request) -> web.Response:
        """Who am I talking to? Signed server time + identity: lets the app check it reaches the REAL server before it sends a key."""
        ip = self.ip(request)
        if not self.limiter.hit(f"hello:{ip}", 60, 60):
            raise CloudError("rate_limited", "Too many requests. Please wait a moment.", 429)
        nonce = request.headers.get("X-BF-Nonce", "")
        if not (8 <= len(nonce) <= 40):
            raise CloudError("bad_request", "The request is not valid.")
        return self.signed(request, {"server": "bestfare", "pub": self.cloud.identity.public_b64})

    async def api_activate(self, request: web.Request) -> web.Response:
        ip = self.ip(request)
        if self.limiter.locked(f"act:{ip}"):
            raise CloudError("rate_limited", "Too many wrong keys. Please wait a few minutes and try again.", 429)
        data, body = await self._json(request)
        nonce, sig, _x, ts = self._check_envelope(request)
        pub = data.get("device_pub")
        if not isinstance(pub, str) or not crypto.valid_public_key(pub):
            raise CloudError("bad_request", "The request is not valid.")
        msg = self.signing_message(request.method, request.path, ts, nonce, body, "")
        if not crypto.verify(pub, msg, sig):                       # proof that the caller really holds the device key it names
            raise CloudError("bad_request", "The request is not valid.")
        if not self.limiter.hit(f"actall:{ip}", 20, 600):
            raise CloudError("rate_limited", "Too many tries. Please wait a few minutes and try again.", 429)
        self._remember_nonce(nonce)
        build = data.get("build", 0)
        try:
            out = self.cloud.activate(data.get("key", ""), pub, data.get("device"), ip, build if isinstance(build, int) else 0, data.get("auto") is True)
        except CloudError as e:
            if e.code in ("bad_key", "device_mismatch", "mac_lock"):
                self.limiter.hit(f"actfail:{ip}", 5, 900)
                if self.limiter.count(f"actfail:{ip}", 900) >= 5:
                    self.limiter.lock(f"act:{ip}", 900)           # five wrong keys: this address waits 15 minutes
            return self.signed_error(request, e)
        return self.signed(request, out)

    async def api_session(self, request: web.Request) -> web.Response:
        row, data = await self._device(request)
        build = data.get("build", 0)
        try:
            out = self.cloud.session(row, self.ip(request), build if isinstance(build, int) else 0)
        except CloudError as e:
            return self.signed_error(request, e)
        out["update"] = self.cloud.update_for(self.cloud.db.one("SELECT * FROM licences WHERE id=?", (row["id"],)),
                                              build if isinstance(build, int) else 0, clean(data.get("channel"), 20) or "stable")
        out["installer"] = self.cloud.installer_for(data.get("shell", 0))
        out["open"] = self.cloud.open_bookings(row)
        return self.signed(request, out)

    async def api_begin(self, request: web.Request) -> web.Response:
        row, d = await self._device(request)
        if not self.limiter.hit(f"begin:{row['id']}", int(self.cloud.setting("begin_per_hour")), 3600):
            self.cloud.event("rate_limited", licence_id=row["id"], ip=self.ip(request), detail="too many bookings started in an hour", level="warn")
            return self.signed_error(request, CloudError("rate_limited", "Too many bookings were started in the last hour. Please wait a while.", 429))
        try:
            out = self.cloud.begin_booking(row, d.get("n"), d.get("offer"), d.get("route"), d.get("date"), d.get("ref"), self.ip(request),
                                           d.get("build") if isinstance(d.get("build"), int) else 0, d.get("infants", 0))
        except CloudError as e:
            return self.signed_error(request, e)
        return self.signed(request, out)

    async def api_finish(self, request: web.Request) -> web.Response:
        row, d = await self._device(request)
        try:
            out = self.cloud.finish_booking(row, d.get("booking_id"), d.get("outcome"), d.get("pnr"), d.get("reason"), self.ip(request))
        except CloudError as e:
            return self.signed_error(request, e)
        return self.signed(request, out)

    async def api_open(self, request: web.Request) -> web.Response:
        row, _d = await self._device(request)
        return self.signed(request, {"open": self.cloud.open_bookings(row)})

    async def api_update_check(self, request: web.Request) -> web.Response:
        row, d = await self._device(request)
        build = d.get("build") if isinstance(d.get("build"), int) else 0
        try:
            self.cloud.check_usable(row)
        except CloudError as e:
            return self.signed_error(request, e)
        out = self.cloud.update_for(row, build, clean(d.get("channel"), 20) or "stable")
        out["installer"] = self.cloud.installer_for(d.get("shell", 0))
        return self.signed(request, out)

    async def api_update_download(self, request: web.Request) -> web.Response:
        row, d = await self._device(request)
        build = d.get("build")
        if isinstance(build, bool) or not isinstance(build, int):
            raise CloudError("bad_request", "The request is not valid.")
        try:
            self.cloud.check_usable(row)
            path, sha = self.cloud.release_file(row, build)
            data = await asyncio.get_running_loop().run_in_executor(None, path.read_bytes)
        except CloudError as e:
            return self.signed_error(request, e)
        except OSError:
            return self.signed_error(request, CloudError("not_found", "That release is not available.", 404))
        nonce = request.headers.get("X-BF-Nonce", "")
        sig = self.cloud.identity.sign(b"BF1dl\n" + request.path.encode() + b"\n" + nonce.encode() + b"\n" + hashlib.sha256(data).hexdigest().encode())
        return web.Response(body=data, content_type="application/octet-stream",
                            headers=_hdr({"X-BF-Sig": sig, "X-BF-Sha256": sha, "X-BF-Nonce": nonce}))

    async def api_update_key(self, request: web.Request) -> web.Response:
        row, d = await self._device(request)
        build = d.get("build")
        if isinstance(build, bool) or not isinstance(build, int):
            raise CloudError("bad_request", "The request is not valid.")
        try:
            self.cloud.check_usable(row)
            out = self.cloud.release_key(row, build, d.get("epk"))
        except CloudError as e:
            return self.signed_error(request, e)
        return self.signed(request, out)

    # ── the admin API ──────────────────────────────────────────────────────────────────────────────────────────────
    async def _admin(self, request: web.Request, *, write: bool = False, owner: bool = False):
        sid = request.cookies.get(COOKIE, "")
        if not sid:
            raise CloudError("unauthorized", "Please sign in.", 401)
        h = hashlib.sha256(sid.encode()).hexdigest()
        db, now = self.cloud.db, time.time()
        s = db.one("SELECT s.*, a.username, a.role, a.disabled, a.must_change, a.totp_on FROM admin_sessions s JOIN admins a ON a.id=s.admin_id WHERE s.sid=?", (h,))
        if s is None or s["expires"] < now or s["disabled"]:
            raise CloudError("unauthorized", "Please sign in.", 401)
        if write:                                                  # a change needs the CSRF token that only the panel's own page knows
            if not crypto.equal(request.headers.get("X-CSRF", ""), s["csrf"]):
                raise CloudError("csrf", "The page is out of date. Please reload it.", 403)
        if owner and s["role"] != "owner":
            raise CloudError("forbidden", "Only an owner can do this.", 403)
        if not self.limiter.hit(f"adm:{h}", 600, 60):
            raise CloudError("rate_limited", "Too many requests. Please wait a moment.", 429)
        db.run("UPDATE admin_sessions SET expires=? WHERE sid=?", (min(now + ADMIN_IDLE_S, s["created"] + ADMIN_MAX_S), h))
        return s

    async def _body(self, request: web.Request) -> dict:
        data, _b = await self._json(request)
        return data

    def ok(self, **data: Any) -> web.Response:
        return web.json_response({"ok": True, **data}, headers=_hdr())

    async def ad_login(self, request: web.Request) -> web.Response:
        ip = self.ip(request)
        d = await self._body(request)
        user, pw, code = clean(d.get("username"), 40), d.get("password"), str(d.get("totp") or "")
        key = f"adlogin:{ip}:{user.lower()}"
        acct = f"adacct:{user.lower()}"
        if self.limiter.locked(key) or self.limiter.locked(f"adlogin:{ip}") or self.limiter.locked(acct):
            raise CloudError("rate_limited", "Too many wrong attempts. Please wait 15 minutes.", 429)
        if not isinstance(pw, str) or not user or len(pw) > 200:
            raise CloudError("bad_login", "Wrong username or password.", 401)
        row = self.cloud.db.one("SELECT * FROM admins WHERE username=?", (user,))
        loop = asyncio.get_running_loop()
        # the same work is done whether or not the user exists (no timing difference tells which usernames are real)
        ok = await loop.run_in_executor(None, crypto.password_check, pw, row["pw_hash"] if row else self._dummy_hash)
        good = bool(row) and ok and not row["disabled"]
        if good and row["totp_on"] and not self.cloud.totp_verify(row["id"], code):
            if not code:
                return web.json_response({"ok": False, "code": "totp_needed", "message": "Enter the 6-digit code from your authenticator app."}, status=401)
            good = False
        if not good:
            self.limiter.hit(key, 5, 600)
            self.limiter.hit(f"adlogin:{ip}", 20, 600)
            self.limiter.hit(acct, 12, 600)
            for k, n in ((key, 5), (f"adlogin:{ip}", 20), (acct, 12)):
                if self.limiter.count(k, 600) >= n:
                    self.limiter.lock(k, 900)
            self.cloud.event("admin_login_failed", actor=user, ip=ip, level="warn")
            raise CloudError("bad_login", "Wrong username or password.", 401)
        sid, csrf, now = crypto.random_token(32), crypto.random_token(24), time.time()
        self.cloud.db.run("INSERT INTO admin_sessions (sid, admin_id, csrf, created, expires, ip, ua) VALUES (?,?,?,?,?,?,?)",
                          (hashlib.sha256(sid.encode()).hexdigest(), row["id"], csrf, now, now + ADMIN_IDLE_S, ip, clean(request.headers.get("User-Agent"), 150)))
        self.cloud.db.run("UPDATE admins SET last_login=? WHERE id=?", (now, row["id"]))
        self.cloud.event("admin_login", actor=user, ip=ip)
        resp = self.ok(username=row["username"], role=row["role"], csrf=csrf, must_change=bool(row["must_change"]))
        resp.set_cookie(COOKIE, sid, httponly=True, samesite="Strict", secure=self.https(request), path="/admin", max_age=ADMIN_MAX_S)
        return resp

    async def ad_logout(self, request: web.Request) -> web.Response:
        sid = request.cookies.get(COOKIE, "")
        if sid:
            self.cloud.db.run("DELETE FROM admin_sessions WHERE sid=?", (hashlib.sha256(sid.encode()).hexdigest(),))
        resp = self.ok()
        resp.del_cookie(COOKIE, path="/admin")
        return resp

    async def ad_me(self, request: web.Request) -> web.Response:
        s = await self._admin(request)
        return self.ok(username=s["username"], role=s["role"], csrf=s["csrf"], must_change=bool(s["must_change"]), totp_on=bool(s["totp_on"]))

    def _actor(self, s) -> str:
        return s["username"]

    async def ad_stats(self, request: web.Request) -> web.Response:
        await self._admin(request)
        return self.ok(stats=self.cloud.stats())

    async def ad_list(self, request: web.Request) -> web.Response:
        await self._admin(request)
        q = request.query
        try:
            limit, offset = int(q.get("limit", 100)), int(q.get("offset", 0))
        except ValueError:
            raise CloudError("bad_request", "The request is not valid.")
        return self.ok(**self.cloud.list_licences(q.get("search", ""), q.get("filter", "all"), limit, offset))

    @staticmethod
    def _id(request: web.Request, name: str, what: str) -> int:
        try:
            n = int(request.match_info[name])
        except ValueError:
            raise CloudError("not_found", f"No such {what}.", 404)
        if not 0 < n < 2 ** 62:                                          # (a number the database cannot hold is not an id either)
            raise CloudError("not_found", f"No such {what}.", 404)
        return n

    def _lid(self, request: web.Request) -> int:
        return self._id(request, "lid", "user")

    async def ad_create(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        d = await self._body(request)
        return self.ok(**self.cloud.create_licence(d.get("label", ""), d.get("credits", 0), d.get("note", ""), d.get("expires_days"),
                                                   d.get("mac_lock", ""), self._actor(s), d.get("offers")))

    async def ad_detail(self, request: web.Request) -> web.Response:
        await self._admin(request)
        return self.ok(licence=self.cloud.licence_detail(self._lid(request)))

    async def ad_edit(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        d = await self._body(request)
        self.cloud.update_licence(self._lid(request), label=d.get("label"), note=d.get("note"), expires_days=d.get("expires_days"),
                                  clear_expiry=bool(d.get("clear_expiry")), mac_lock=d.get("mac_lock"), actor=self._actor(s), offers=d.get("offers"))
        return self.ok()

    async def ad_delete(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True, owner=True)
        self.cloud.delete_licence(self._lid(request), self._actor(s))
        return self.ok()

    async def ad_credits(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        d = await self._body(request)
        return self.ok(credits=self.cloud.adjust_credits(self._lid(request), d.get("delta"), d.get("note", ""), self._actor(s)))

    async def ad_block(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        d = await self._body(request)
        self.cloud.set_status(self._lid(request), "blocked", d.get("reason", ""), self._actor(s))
        return self.ok()

    async def ad_unblock(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        self.cloud.set_status(self._lid(request), "active", "", self._actor(s))
        return self.ok()

    async def ad_reset_device(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        self.cloud.reset_device(self._lid(request), self._actor(s))
        return self.ok()

    async def ad_reveal(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        return self.ok(key=self.cloud.reveal_key(self._lid(request), self._actor(s)))

    async def ad_regen(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        return self.ok(key=self.cloud.regenerate_key(self._lid(request), self._actor(s)))

    async def ad_refund(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        return self.ok(**self.cloud.admin_refund_booking(clean(request.match_info["bid"], 40), self._actor(s)))

    async def ad_events(self, request: web.Request) -> web.Response:
        await self._admin(request)
        q = request.query
        try:
            limit = int(q.get("limit", 200))
        except ValueError:
            raise CloudError("bad_request", "The request is not valid.")
        return self.ok(events=self.cloud.events(q.get("level", ""), q.get("kind", ""), limit))

    async def ad_settings(self, request: web.Request) -> web.Response:
        await self._admin(request)
        return self.ok(settings=self.cloud.all_settings())

    async def ad_settings_put(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        d = await self._body(request)
        self.cloud.set_settings(d, self._actor(s))
        return self.ok(settings=self.cloud.all_settings())

    async def ad_releases(self, request: web.Request) -> web.Response:
        await self._admin(request)
        return self.ok(releases=self.cloud.list_releases(), trusted=len(self.cloud.release_keys))

    async def ad_release_upload(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True, owner=True)
        data = await request.read()
        out = self.cloud.add_release(data, request.headers.get("X-Release-Key", ""), clean(request.headers.get("X-Release-Notes", ""), 2000), self._actor(s))
        return self.ok(**out)

    async def ad_release_edit(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True, owner=True)
        d = await self._body(request)
        build = self._id(request, "build", "release")
        self.cloud.edit_release(build, state=d.get("state"), mandatory=d.get("mandatory"), rollout=d.get("rollout"), notes=d.get("notes"), actor=self._actor(s))
        return self.ok()

    async def ad_release_delete(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True, owner=True)
        build = self._id(request, "build", "release")
        self.cloud.delete_release(build, self._actor(s))
        return self.ok()

    async def ad_admins(self, request: web.Request) -> web.Response:
        await self._admin(request, owner=True)
        return self.ok(admins=self.cloud.list_admins())

    async def ad_admin_create(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True, owner=True)
        d = await self._body(request)
        pw = d.get("password") if isinstance(d.get("password"), str) and d.get("password") else crypto.random_password()
        aid = self.cloud.create_admin(d.get("username", ""), pw, d.get("role", "admin"), self._actor(s), must_change=True)
        return self.ok(id=aid, password=pw)

    async def ad_admin_edit(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True, owner=True)
        d = await self._body(request)
        aid = self._id(request, "aid", "admin")
        pw = d.get("password")
        reset = None
        if d.get("reset_password"):
            reset = crypto.random_password()
            pw = reset
        self.cloud.set_admin(aid, disabled=d.get("disabled"), password=pw, role=d.get("role"), actor=self._actor(s))
        if reset:
            self.cloud.db.run("UPDATE admins SET must_change=1 WHERE id=?", (aid,))
        return self.ok(**({"password": reset} if reset else {}))

    async def _reconfirm(self, s, password: Any, what: str) -> Any:
        """The signed-in admin proves they know their password again (a stolen session is not enough to change how login works)."""
        key = f"pw:{s['username'].lower()}"
        if self.limiter.locked(key):
            raise CloudError("rate_limited", "Too many wrong passwords. Please wait 15 minutes.", 429)
        row = self.cloud.db.one("SELECT * FROM admins WHERE username=?", (s["username"],))
        loop = asyncio.get_running_loop()
        if not isinstance(password, str) or not await loop.run_in_executor(None, crypto.password_check, password, row["pw_hash"]):
            self.limiter.hit(key, 5, 600)
            if self.limiter.count(key, 600) >= 5:
                self.limiter.lock(key, 900)
            self.cloud.event("password_reconfirm_failed", actor=s["username"], detail=what, level="warn")
            raise CloudError("bad_login", "That password is not right.", 401)
        return row

    async def ad_password(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        d = await self._body(request)
        row = await self._reconfirm(s, d.get("old"), "change password")
        if not isinstance(d.get("new"), str):
            raise CloudError("bad_request", "The request is not valid.")
        self.cloud.set_admin(row["id"], password=d["new"], actor=s["username"])
        return self.ok(relogin=True)

    async def ad_totp_setup(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        d = await self._body(request)
        row = await self._reconfirm(s, d.get("password"), "set up two-step login")
        secret = self.cloud.totp_start(row["id"])
        return self.ok(secret=secret, uri=f"otpauth://totp/BestFare:{s['username']}?secret={secret}&issuer=BestFare")

    async def ad_totp_enable(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        d = await self._body(request)
        row = self.cloud.db.one("SELECT id FROM admins WHERE username=?", (s["username"],))
        if self.limiter.locked(f"totp:{s['username'].lower()}"):
            raise CloudError("rate_limited", "Too many wrong codes. Please wait 15 minutes.", 429)
        if not row or not self.cloud.totp_verify(row["id"], d.get("code"), consume=False):          # (not consumed: the first real login may use the same code)
            self.limiter.hit(f"totp:{s['username'].lower()}", 5, 600)
            if self.limiter.count(f"totp:{s['username'].lower()}", 600) >= 5:
                self.limiter.lock(f"totp:{s['username'].lower()}", 900)
            raise CloudError("bad_code", "That code is not right. Check the time on your phone and try again.", 400)
        self.cloud.db.run("UPDATE admins SET totp_on=1 WHERE username=?", (s["username"],))
        self.cloud.event("totp_on", actor=s["username"])
        return self.ok()

    async def ad_totp_disable(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True)
        d = await self._body(request)
        await self._reconfirm(s, d.get("password"), "switch off two-step login")
        self.cloud.db.run("UPDATE admins SET totp_on=0, totp_secret=NULL, totp_last=0 WHERE username=?", (s["username"],))
        self.cloud.event("totp_off", actor=s["username"], level="warn")
        return self.ok()

    async def ad_installers(self, request: web.Request) -> web.Response:
        await self._admin(request)
        live = self.cloud.live_installer()
        base = self.cfg.public_url.rstrip("/")
        return self.ok(installers=self.cloud.list_installers(), live=live["version"] if live else None, link=base + "/install" if base else "",
                       direct=base + "/download/BestFare-Setup.exe" if base else "")

    async def ad_installer_upload(self, request: web.Request) -> web.Response:
        """The setup program, streamed to disk (it is large; it is never held in memory) and checked against the developer's signature."""
        s = await self._admin(request, write=True, owner=True)
        version = clean(request.headers.get("X-Installer-Version", ""), 20)
        sig = clean(request.headers.get("X-Installer-Sig", ""), 200)
        notes = clean(request.headers.get("X-Installer-Notes", ""), 2000)
        folder = self.cloud.dir / "installers"
        folder.mkdir(exist_ok=True)
        tmp = folder / f".upload-{crypto.random_token(8)}"
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    size += len(chunk)
                    if size > bundle.MAX_INSTALLER:
                        raise CloudError("too_large", "That file is too large.", 413)
                    f.write(chunk)
            out = await asyncio.get_running_loop().run_in_executor(None, self.cloud.add_installer, tmp, version, sig, notes, self._actor(s))
        finally:
            try:
                tmp.unlink()                                                  # (gone already when it was accepted: it was moved into place)
            except OSError:
                pass
        return self.ok(**out)

    async def ad_installer_edit(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True, owner=True)
        d = await self._body(request)
        self.cloud.edit_installer(self._id(request, "iid", "installer"), state=d.get("state"), mandatory=d.get("mandatory"), notes=d.get("notes"), actor=self._actor(s))
        return self.ok()

    async def ad_installer_delete(self, request: web.Request) -> web.Response:
        s = await self._admin(request, write=True, owner=True)
        self.cloud.delete_installer(self._id(request, "iid", "installer"), self._actor(s))
        return self.ok()

    # ── the public page and the download (the LINK to give people) ─────────────────────────────────────────────────────
    async def pg_root(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/install")

    async def pg_install(self, request: web.Request) -> web.Response:
        r = self.cloud.live_installer()
        if r is None:
            button, meta, sha = '<span class="btn off">Coming soon</span>', "The installer is being prepared. Please check again a little later.", ""
        else:
            mb = r["size"] / 1024 / 1024
            button = '<a class="btn" href="/download/BestFare-Setup.exe">Download for Windows</a>'
            meta = f"Version {html.escape(r['version'])} · {mb:.0f} MB · Windows 10 / 11"
            sha = f"SHA-256 fingerprint of the file: {r['sha256']}"
        page = (WEB_DIR / "install.html").read_text(encoding="utf-8").replace("{{button}}", button).replace("{{meta}}", meta).replace("{{sha}}", html.escape(sha))
        return web.Response(text=page, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-cache"})

    async def pg_css(self, request: web.Request) -> web.Response:
        return web.Response(body=(WEB_DIR / "install.css").read_bytes(), content_type="text/css", headers={"Cache-Control": "public, max-age=300"})

    async def pg_download(self, request: web.Request) -> web.StreamResponse:
        ip = self.ip(request)
        if not self.limiter.hit(f"dl:{ip}", 30, 3600):
            raise CloudError("rate_limited", "Too many downloads. Please try again later.", 429)
        r = self.cloud.live_installer()
        path = self.cloud.installer_path(r) if r is not None else None
        if r is None or path is None or not path.exists():
            raise web.HTTPNotFound(text="The installer is not available yet.")
        return web.FileResponse(path, headers={"Content-Disposition": 'attachment; filename="BestFare-Setup.exe"', "X-Setup-SHA256": r["sha256"],
                                               "X-Setup-Version": r["version"], "Cache-Control": "no-cache", "Content-Type": "application/octet-stream"})

    # ── the panel's own files ──────────────────────────────────────────────────────────────────────────────────────
    async def ad_index(self, request: web.Request) -> web.Response:
        return web.Response(body=(WEB_DIR / "index.html").read_bytes(), content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-store"})

    async def ad_static(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        allowed = {"app.js": "text/javascript", "style.css": "text/css", "logo.svg": "image/svg+xml"}
        if name not in allowed:                                    # (a fixed list: no path the caller makes up is ever opened)
            raise web.HTTPNotFound()
        return web.Response(body=(WEB_DIR / name).read_bytes(), content_type=allowed[name], headers={"Cache-Control": "no-cache"})


def create_app(cloud: Cloud, cfg: Optional[Config] = None) -> web.Application:
    return App(cloud, cfg or Config()).app
