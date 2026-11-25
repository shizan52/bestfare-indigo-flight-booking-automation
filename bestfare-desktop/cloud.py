"""The conversation with the Best Fare server. Every request is signed with this computer's own key; every answer is checked against
the server's identity key before ANY of it is believed. A wrong signature, a wrong path, a stale time, an answer to a different request:
all of them end in `CloudError("untrusted")` - the app never acts on something it cannot verify.

Built for slow and unreliable connections: patient timeouts that grow with the measured round-trip time, a few quick retries for calls
that are safe to repeat, and an `Offline` error (not a crash) when the server cannot be reached.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import bundle
import trust
from device import DeviceKey

MAX_ANSWER = 48 * 1024 * 1024


class CloudError(Exception):
    def __init__(self, code: str, message: str, status: int = 0, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


class Offline(CloudError):
    def __init__(self, message: str = "Cannot reach the Best Fare server. Please check the internet connection."):
        super().__init__("offline", message)


def shell_number() -> int:
    """This installed program's own version as the number the server compares (1.2.3 -> 1002003)."""
    try:
        return bundle.version_number(trust.SHELL_VERSION)
    except ValueError:
        return 0


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64d(t: str) -> bytes:
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def verify(public_b64: str, data: bytes, sig_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(b64d(public_b64)).verify(b64d(sig_b64), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def new_ephemeral() -> tuple[X25519PrivateKey, str]:
    k = X25519PrivateKey.generate()
    return k, b64e(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def unwrap(private: X25519PrivateKey, wrapped: dict, context: bytes) -> bytes:
    shared = private.exchange(X25519PublicKey.from_public_bytes(b64d(wrapped["spk"])))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"bestfare/wrap/" + context).derive(shared)
    try:
        return AESGCM(key).decrypt(b64d(wrapped["n"]), b64d(wrapped["ct"]), context)
    except InvalidTag as e:
        raise CloudError("untrusted", "A key from the server could not be opened.") from e


def spki_pin(der_certificate: bytes) -> str:
    """The SHA-256 of a certificate's public key (it survives a renewed certificate that keeps the key)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    spki = x509.load_der_x509_certificate(der_certificate).public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()


class _PinnedHTTPS(urllib.request.HTTPSHandler):
    """https that trusts exactly ONE public key (the server's own, made without a domain name) - no authority, no name check, nothing else."""

    def __init__(self, pin: str):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        super().__init__(context=ctx)
        self.pin = pin.lower()

    def https_open(self, req):
        pin = self.pin

        class Connection(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                try:
                    same = hmac.compare_digest(spki_pin(self.sock.getpeercert(binary_form=True)), pin)
                except Exception:  # noqa: BLE001
                    same = False
                if not same:
                    self.close()
                    raise ssl.SSLError("CERTIFICATE_PIN_MISMATCH")

        return self.do_open(Connection, req, context=self._context)


def _read_all(r, limit: int, progress=None) -> bytes:
    """The whole answer, read in pieces so a download can show how far it is. At most limit+1 bytes (one more than allowed: the caller refuses it)."""
    total = int(r.headers.get("Content-Length") or 0)
    buf = bytearray()
    while len(buf) <= limit:
        chunk = r.read(min(256 * 1024, limit + 1 - len(buf)))
        if not chunk:
            break
        buf += chunk
        if progress:
            progress(len(buf), total)
    return bytes(buf)


class CloudClient:
    def __init__(self, device: DeviceKey, build: int, *, base_url: Optional[str] = None, server_public: Optional[str] = None,
                 tls_pin: Optional[str] = None):
        self.device = device
        self.build = build
        self.base = (base_url or trust.server_url()).rstrip("/")
        self.server_public = server_public or trust.SERVER_PUBLIC
        self.tls_pin = (trust.TLS_PIN if tls_pin is None else tls_pin) or ""
        self.token = ""
        self.clock = time.time                 # (this computer's clock; tests replace it to simulate a wrong one)
        self.skew_ms = 0                       # (server clock - this computer's clock): requests are stamped with the server's idea of time
        self.rtt = 1.0                         # smoothed round-trip seconds: timeouts follow it

    @property
    def device_id(self) -> str:
        """The id the server derives from this computer's public key (it is part of the context a release key is wrapped for)."""
        return hashlib.sha256(b64e(self.device.public_raw).encode()).hexdigest()[:24]

    # ── transport ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _now_ms(self) -> int:
        return int(self.clock() * 1000) + self.skew_ms

    def _timeout(self, base: float = 20.0) -> float:
        return min(120.0, max(base, 10 * self.rtt))

    def _message(self, method: str, path: str, ts: int, nonce: str, body: bytes, token: str) -> bytes:
        return "\n".join(["BF1", method, path, str(ts), nonce, hashlib.sha256(body).hexdigest(), hashlib.sha256(token.encode()).hexdigest()]).encode()

    def _open_url(self, req: urllib.request.Request, timeout: float):
        https = self.base.startswith("https")
        if https and self.tls_pin:
            return urllib.request.build_opener(_PinnedHTTPS(self.tls_pin)).open(req, timeout=timeout)
        return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context() if https else None)

    @staticmethod
    def _transport_error(e: Exception):
        if "CERTIFICATE_PIN_MISMATCH" in str(e) or "CERTIFICATE_VERIFY_FAILED" in str(e):
            return CloudError("untrusted", "This is not the real Best Fare server (its certificate is not the expected one).")
        return Offline()

    def _send(self, path: str, body: bytes, headers: dict, timeout: float, progress=None) -> tuple[int, dict, bytes]:
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method="POST")
        try:
            with self._open_url(req, timeout) as r:
                data = _read_all(r, MAX_ANSWER, progress)
                status, hdrs = r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            data, status, hdrs = e.read(MAX_ANSWER + 1), e.code, dict(e.headers)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as e:
            raise self._transport_error(e) from e
        if len(data) > MAX_ANSWER:
            raise CloudError("untrusted", "The server's answer is too large.")
        return status, hdrs, data

    def fetch_file(self, path: str, dest, *, limit: int, progress=None, timeout: float = 60.0) -> str:
        """A plain GET of a public file of the server (the setup program) into `dest`, in pieces. Returns its SHA-256. The CALLER checks that hash and the
        developer's signature: nothing here is trusted for what it is. (The file is never held in memory - it is large.)"""
        if not path.startswith("/") or "//" in path or ".." in path or any(c in path for c in "?#\\ "):
            raise CloudError("bad_request", "That download address is not valid.")
        req = urllib.request.Request(self.base + path, headers={"User-Agent": "BestFare/" + str(self.build)}, method="GET")
        h = hashlib.sha256()
        done = 0
        try:
            with self._open_url(req, timeout) as r, open(dest, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                if r.status != 200 or total > limit:
                    raise CloudError("bad_answer", "The download is not available right now.", r.status)
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > limit:
                        raise CloudError("untrusted", "The download is larger than announced.")
                    h.update(chunk)
                    f.write(chunk)
                    if progress:
                        progress(done, total)
        except urllib.error.HTTPError as e:
            raise CloudError("bad_answer", "The download is not available right now.", e.code) from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as e:
            if isinstance(e, CloudError):
                raise
            raise self._transport_error(e) from e
        return h.hexdigest()

    def call(self, path: str, data: Optional[dict] = None, *, signed_in: bool = True, retries: int = 2, binary: bool = False, timeout: float = 20.0, progress=None):
        """One signed request. Returns the verified `d` part of the answer (or the verified bytes of a download)."""
        body = json.dumps(data or {}).encode()
        last: Optional[Exception] = None
        for attempt in range(retries + 1):
            nonce = b64e(os.urandom(12))
            ts = self._now_ms()
            token = self.token if signed_in else ""
            sig = b64e(self.device.sign(self._message("POST", path, ts, nonce, body, token)))
            headers = {"Content-Type": "application/json", "X-BF-Nonce": nonce, "X-BF-Ts": str(ts), "X-BF-Sig": sig, "User-Agent": "BestFare/" + str(self.build)}
            if token:
                headers["X-BF-Token"] = token
            t0 = time.monotonic()
            try:
                if progress is None:
                    status, hdrs, raw = self._send(path, body, headers, self._timeout(timeout))
                else:
                    status, hdrs, raw = self._send(path, body, headers, self._timeout(timeout), progress=progress)
            except Offline as e:
                last = e
                time.sleep(min(2.0 * (attempt + 1), 5.0))
                continue
            self.rtt = 0.7 * self.rtt + 0.3 * (time.monotonic() - t0)
            if binary and status == 200 and hdrs.get("Content-Type", "").startswith("application/octet-stream"):
                lowered = {k.lower(): v for k, v in hdrs.items()}
                want = b"BF1dl\n" + path.encode() + b"\n" + nonce.encode() + b"\n" + hashlib.sha256(raw).hexdigest().encode()
                if lowered.get("x-bf-nonce") != nonce or not verify(self.server_public, want, lowered.get("x-bf-sig", "")):
                    raise CloudError("untrusted", "A download from the server could not be verified.")
                return raw
            try:
                env = json.loads(raw)
            except ValueError:
                if status >= 500 and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise CloudError("bad_answer", "The server gave an answer that could not be read.", status)
            try:
                return self._open(env, path, nonce, status)
            except CloudError as e:
                if e.code == "clock" and e.extra.get("retry") and attempt < retries:       # the clock was off: it is learned now, try once more
                    continue
                raise
        raise last if last else Offline()

    def _open(self, env: dict, path: str, nonce: str, status: int) -> dict:
        if not isinstance(env, dict):
            raise CloudError("untrusted", "The server's answer could not be verified.")
        if "sig" not in env:                                              # an unsigned error (rate limit, size, a proxy): never trusted for data
            code = str(env.get("code") or "error")
            if code == "clock" and isinstance(env.get("server_time"), int):
                self.skew_ms = int(env["server_time"]) - int(self.clock() * 1000)
                raise CloudError("clock", str(env.get("message") or "The clock is wrong."), status, retry=True)
            raise CloudError(code, str(env.get("message") or "The server could not do that."), status)
        core = {k: env.get(k) for k in ("ok", "d", "ts", "n", "p")}
        if not verify(self.server_public, canonical(core), str(env["sig"])) or env.get("n") != nonce or env.get("p") != path:
            raise CloudError("untrusted", "The server's answer could not be verified. Please check that this is the real Best Fare.")
        if abs(int(env.get("ts") or 0) - self._now_ms()) > 5 * 60 * 1000:
            raise CloudError("untrusted", "The server's answer is out of date.")
        d = env.get("d") or {}
        if not env.get("ok"):
            raise CloudError(str(d.get("code") or "error"), str(d.get("message") or "The server could not do that."), status,
                             **{k: v for k, v in d.items() if k not in ("code", "message")})
        return d

    # ── the calls ──────────────────────────────────────────────────────────────────────────────────────────────────
    def hello(self) -> dict:
        """Is this the real server? (Also learns its clock.)"""
        nonce = b64e(os.urandom(12))
        t0 = time.monotonic()
        status, hdrs, raw = self._send("/api/v1/hello", b"{}", {"X-BF-Nonce": nonce, "Content-Type": "application/json"}, self._timeout())
        self.rtt = 0.7 * self.rtt + 0.3 * (time.monotonic() - t0)
        try:
            env = json.loads(raw)
        except ValueError:
            raise CloudError("bad_answer", "The server gave an answer that could not be read.", status)
        core = {k: env.get(k) for k in ("ok", "d", "ts", "n", "p")}
        if not verify(self.server_public, canonical(core), str(env.get("sig", ""))) or env.get("n") != nonce:
            raise CloudError("untrusted", "This is not the real Best Fare server.")
        self.skew_ms = int(env["ts"]) - int(self.clock() * 1000)
        return env["d"]

    def activate(self, key: str, device_info: dict, auto: bool = False) -> dict:
        """`auto`: signing in again by itself with a saved key (not typed by a person): the server will not hand an unbound key to it."""
        body = {"key": key, "device_pub": b64e(self.device.public_raw), "device": device_info, "build": self.build}
        if auto:
            body["auto"] = True
        d = self.call("/api/v1/activate", body, signed_in=False)
        self.token = d["token"]
        return d

    def session(self) -> dict:
        d = self.call("/api/v1/session", {"build": self.build, "channel": trust.CHANNEL, "shell": shell_number()})
        self.token = d["token"]
        return d

    def begin(self, n: int, offer: str, route: str, date: str, ref: str, infants: int = 0) -> dict:
        body = {"n": n, "offer": offer, "route": route, "date": date, "ref": ref, "build": self.build}
        if infants:
            body["infants"] = infants
        return self.call("/api/v1/booking/begin", body, retries=3)

    def finish(self, booking_id: str, outcome: str, pnr: Optional[str] = None, reason: str = "") -> dict:
        return self.call("/api/v1/booking/finish", {"booking_id": booking_id, "outcome": outcome, "pnr": pnr, "reason": reason[:80]}, retries=3)

    def open_bookings(self) -> list[dict]:
        return self.call("/api/v1/booking/open")["open"]

    def update_check(self) -> dict:
        return self.call("/api/v1/update/check", {"build": self.build, "channel": trust.CHANNEL, "shell": shell_number()})

    def download(self, build: int, progress=None) -> bytes:
        return self.call("/api/v1/update/download", {"build": build}, binary=True, timeout=60.0, progress=progress)

    def release_key(self, build: int) -> bytes:
        priv, epk = new_ephemeral()
        d = self.call("/api/v1/update/key", {"build": build, "epk": epk})
        return unwrap(priv, d, f"release/{build}/{self.device_id}".encode())
