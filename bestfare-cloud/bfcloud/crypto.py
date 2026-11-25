"""Every cryptographic building block of the Best Fare cloud, in one small, reviewable file.

  * Ed25519 signatures   - the server signs EVERY answer (the app trusts the signature, not the network); the developer signs releases;
                           every app signs its requests with its own device key
  * X25519 + HKDF + AES-GCM - delivering a release's decryption key to ONE app, so it can only be read by that app
  * AES-GCM "sealing"    - secrets at rest on the server (licence keys, release keys)
  * HMAC tokens          - the session token an app holds after activation
  * scrypt               - admin passwords (never stored, never logged)
  * TOTP (RFC 6238)      - optional second factor for admins

Nothing here knows about HTTP or the database.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# ── encodings ───────────────────────────────────────────────────────────────────────────────────────────────────────
def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    if not isinstance(text, str):
        raise ValueError("not text")
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def canonical(obj: Any) -> bytes:
    """One and only one byte string for a JSON value: what is signed and what is verified are always the same bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def equal(a: str | bytes, b: str | bytes) -> bool:
    a = a.encode() if isinstance(a, str) else a
    b = b.encode() if isinstance(b, str) else b
    return hmac.compare_digest(a, b)


# ── Ed25519 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class Signer:
    """A private signing key (the server's identity, or the developer's release key)."""

    def __init__(self, private: Ed25519PrivateKey):
        self._k = private

    @classmethod
    def generate(cls) -> "Signer":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path: Path) -> "Signer":
        path = Path(path)
        if path.exists():
            return cls(serialization.load_pem_private_key(path.read_bytes(), password=None))  # type: ignore[arg-type]
        s = cls.generate()
        write_private(path, s.private_pem())
        return s

    def private_pem(self) -> bytes:
        return self._k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    @property
    def public_b64(self) -> str:
        return b64e(self._k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def sign(self, data: bytes) -> str:
        return b64e(self._k.sign(data))


def verify(public_b64: str, data: bytes, sig_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(b64d(public_b64)).verify(b64d(sig_b64), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def valid_public_key(public_b64: str) -> bool:
    try:
        return len(b64d(public_b64)) == 32 and bool(Ed25519PublicKey.from_public_bytes(b64d(public_b64)))
    except (ValueError, TypeError):
        return False


# ── AES-GCM sealing (secrets at rest) ───────────────────────────────────────────────────────────────────────────────
def seal(secret: bytes, plaintext: bytes, aad: bytes = b"") -> str:
    nonce = os.urandom(12)
    return b64e(nonce + AESGCM(secret).encrypt(nonce, plaintext, aad))


def unseal(secret: bytes, sealed: str, aad: bytes = b"") -> bytes:
    raw = b64d(sealed)
    try:
        return AESGCM(secret).decrypt(raw[:12], raw[12:], aad)
    except InvalidTag as e:
        raise ValueError("sealed data was changed") from e


def write_private(path: Path, data: bytes) -> None:
    """A new secret file, private from its very first byte (created with owner-only rights: there is no moment when others could read it)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def load_or_create_secret(path: Path, size: int = 32) -> bytes:
    path = Path(path)
    if path.exists():
        data = path.read_bytes()
        if len(data) < size:                       # (damaged: never silently replaced - every key the server ever issued would stop working)
            raise ValueError(f"{path.name} is damaged. Restore it from the backup.")
        return data[:size]
    data = os.urandom(size)
    write_private(path, data)
    return data


def subkey(master: bytes, purpose: str) -> bytes:
    """A different key for each purpose from one master secret (so a weakness in one use cannot touch another)."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"bestfare/" + purpose.encode()).derive(master)


# ── delivering a key to ONE app (X25519 + HKDF + AES-GCM) ───────────────────────────────────────────────────────────
def new_ephemeral() -> tuple[X25519PrivateKey, str]:
    k = X25519PrivateKey.generate()
    return k, b64e(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def _wrap_key(private: X25519PrivateKey, peer_b64: str, context: bytes) -> bytes:
    shared = private.exchange(X25519PublicKey.from_public_bytes(b64d(peer_b64)))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"bestfare/wrap/" + context).derive(shared)


def wrap_for(client_epk_b64: str, secret: bytes, context: bytes) -> dict:
    """Encrypt `secret` so that only the holder of the private half of `client_epk_b64` can read it."""
    priv, pub = new_ephemeral()
    key = _wrap_key(priv, client_epk_b64, context)
    nonce = os.urandom(12)
    return {"spk": pub, "n": b64e(nonce), "ct": b64e(AESGCM(key).encrypt(nonce, secret, context))}


def unwrap_with(private: X25519PrivateKey, wrapped: dict, context: bytes) -> bytes:
    key = _wrap_key(private, wrapped["spk"], context)
    try:
        return AESGCM(key).decrypt(b64d(wrapped["n"]), b64d(wrapped["ct"]), context)
    except InvalidTag as e:
        raise ValueError("the wrapped key was changed") from e


# ── session tokens ──────────────────────────────────────────────────────────────────────────────────────────────────
def token_make(secret: bytes, payload: dict) -> str:
    body = b64e(canonical(payload))
    mac = b64e(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def token_check(secret: bytes, token: str, now: Optional[float] = None) -> Optional[dict]:
    """The payload of a genuine, unexpired token - or None. (No detail about WHY: a caller learns nothing it can use.)"""
    try:
        body, mac = token.split(".", 1)
        want = b64e(hmac.new(secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, want):
            return None
        payload = json.loads(b64d(body))
        if not isinstance(payload, dict) or float(payload.get("exp", 0)) < (now if now is not None else time.time()):
            return None
        return payload
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


# ── licence keys ────────────────────────────────────────────────────────────────────────────────────────────────────
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I: a key read aloud or typed from a photo is not mistaken


def new_licence_key() -> str:
    """BF-XXXX-XXXX-XXXX-XXXX : 80 bits of randomness (20 symbols of 5 bits)."""
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    return "BF-" + "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def normalize_key(text: str) -> str:
    """What a person typed -> the canonical key ('' when it cannot be a key)."""
    t = "".join(ch for ch in (text or "").upper() if ch.isalnum())
    if len(t) == 18 and t.startswith("BF"):                           # (the 'BF' prefix is optional when typing)
        t = t[2:]
    if len(t) != 16 or any(ch not in _ALPHABET for ch in t):         # (nothing is guessed: an O is not turned into a 0)
        return ""
    return "BF-" + "-".join(t[i:i + 4] for i in range(0, 16, 4))


def key_lookup_hash(pepper: bytes, key: str) -> str:
    """What the database stores to FIND a key (a leaked database does not leak keys)."""
    return hmac.new(pepper, key.encode(), hashlib.sha256).hexdigest()


# ── admin passwords (scrypt) ────────────────────────────────────────────────────────────────────────────────────────
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "dklen": 32}


def password_hash(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=64 * 1024 * 1024, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${b64e(salt)}${b64e(dk)}"


def password_check(password: str, stored: str) -> bool:
    try:
        kind, n, salt, dk = stored.split("$")
        if kind != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode("utf-8"), salt=b64d(salt), n=int(n), r=_SCRYPT["r"], p=_SCRYPT["p"],
                              dklen=_SCRYPT["dklen"], maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(calc, b64d(dk))
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str:
    """'' when the password is acceptable, else what is wrong (in words an admin understands)."""
    if len(password) < 10:
        return "Use at least 10 characters."
    if password.lower() in {"password123", "1234567890", "qwertyuiop", "administrator"}:
        return "That password is too easy to guess."
    classes = sum(any(f(c) for c in password) for f in (str.islower, str.isupper, str.isdigit, lambda c: not c.isalnum()))
    if classes < 3:
        return "Mix upper and lower case letters, digits and symbols (at least three kinds)."
    return ""


def random_password(length: int = 18) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(length - 2)) + secrets.choice("!#$%&*+-=?@") + secrets.choice("23456789")
        if not password_problem(p):
            return p


# ── TOTP (RFC 6238) ─────────────────────────────────────────────────────────────────────────────────────────────────
def totp_new_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def totp_code(secret_b32: str, at: Optional[float] = None, step: int = 30) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    counter = int((at if at is not None else time.time()) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_match(secret_b32: str, code: str, at: Optional[float] = None) -> Optional[int]:
    """The 30-second step the code belongs to (this one or a neighbour: clocks differ a little) - or None when it is not right."""
    code = (code or "").strip().replace(" ", "") if isinstance(code, str) else ""
    if len(code) != 6 or not code.isdigit():
        return None
    now = at if at is not None else time.time()
    found = None
    for d in (-1, 0, 1):                                   # (all three are always computed: the time taken does not tell which one matched)
        if equal(totp_code(secret_b32, now + d * 30), code):
            found = int((now + d * 30) // 30)
    return found


def totp_check(secret_b32: str, code: str, at: Optional[float] = None) -> bool:
    return totp_match(secret_b32, code, at) is not None


def random_token(nbytes: int = 32) -> str:
    return b64e(secrets.token_bytes(nbytes))
