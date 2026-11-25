"""The release bundle: how an update travels from the developer to every app.

    bundle.bfr  (a zip, nothing compressed)
        manifest.json   canonical JSON: format, build (a number that only ever goes up), version, channel, notes, created,
                        payload {sha256 of the encrypted payload, size, nonce, plain_sha256, files}
        manifest.sig    Ed25519 signature of manifest.json by the DEVELOPER'S release key
        payload.bin     AES-256-GCM( zip of the app's files ), bound to this build number

  * Only the developer holds the signing key (it is never on the server). The server only stores and serves bundles - it cannot
    make one, so a hacked server cannot push code to the apps.
  * The payload is encrypted with a key that is NOT in the bundle. The server hands that key, wrapped for ONE app, to a licensed
    and activated device only - so the application code is never readable on disk, and cannot be run without a valid licence.
  * An app accepts a bundle only if: the signature is right, the build number is higher than its own, the payload's hash matches,
    and every file path inside is safe (no '..', no absolute paths, no odd names).

This file is used, unchanged, by the developer's tool, by the server and by the app (tests/test_bundle_sync.py keeps the copies equal).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import zipfile
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidSignature
import base64

FORMAT = 1
MAX_BUNDLE = 40 * 1024 * 1024          # a release is a few hundred KB; anything near this size is not one
MAX_FILES = 400
MAX_PLAIN = 60 * 1024 * 1024
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*(/[A-Za-z0-9_][A-Za-z0-9_.\-]*)*$")


class BundleError(Exception):
    """The bundle is not acceptable (the message says why, in plain words)."""


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(t: str) -> bytes:
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def safe_path(name: str) -> bool:
    return bool(_SAFE_PATH.match(name)) and ".." not in name.split("/") and len(name) <= 120


# ── building (developer's machine) ──────────────────────────────────────────────────────────────────────────────────
def pack_files(files: dict[str, bytes]) -> bytes:
    """The plain zip that gets encrypted."""
    if not files or len(files) > MAX_FILES:
        raise BundleError("a release needs between 1 and %d files" % MAX_FILES)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name in sorted(files):
            if not safe_path(name):
                raise BundleError(f"unsafe file name: {name!r}")
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))      # (fixed time: the same files give the same bundle)
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name])
    return buf.getvalue()


def build(files: dict[str, bytes], *, build_no: int, version: str, channel: str, notes: str, signing_key: Ed25519PrivateKey,
          content_key: Optional[bytes] = None, created: Optional[float] = None) -> tuple[bytes, bytes, dict]:
    """-> (bundle bytes, content key (keep it: the server needs it), manifest)."""
    if not isinstance(build_no, int) or build_no < 1:
        raise BundleError("the build number must be a positive whole number")
    key = content_key or os.urandom(32)
    plain = pack_files(files)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plain, b"bestfare-release/%d" % build_no)
    manifest = {"format": FORMAT, "build": build_no, "version": str(version)[:30], "channel": str(channel)[:20],
                "notes": str(notes)[:2000], "created": int(created if created is not None else time.time()),
                "payload": {"sha256": hashlib.sha256(ct).hexdigest(), "size": len(ct), "nonce": _b64e(nonce),
                            "plain_sha256": hashlib.sha256(plain).hexdigest(), "files": len(files)}}
    mbytes = _canonical(manifest)
    sig = _b64e(signing_key.sign(mbytes)).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for name, data in (("manifest.json", mbytes), ("manifest.sig", sig), ("payload.bin", ct)):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            z.writestr(info, data)
    return out.getvalue(), key, manifest


# ── checking (server and app) ───────────────────────────────────────────────────────────────────────────────────────
def read_bundle(data: bytes, trusted_keys: list[str]) -> tuple[dict, bytes]:
    """Verify a bundle WITHOUT decrypting it -> (manifest, encrypted payload). Raises BundleError for anything wrong."""
    if not data or len(data) > MAX_BUNDLE:
        raise BundleError("the bundle has an impossible size")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BundleError("this is not a release bundle") from e
    names = {i.filename for i in z.infolist()}
    if names != {"manifest.json", "manifest.sig", "payload.bin"} or len(z.infolist()) != 3:
        raise BundleError("the bundle has the wrong content")
    for info in z.infolist():
        if info.file_size > MAX_BUNDLE:
            raise BundleError("a part of the bundle is too large")
    mbytes, sig, payload = z.read("manifest.json"), z.read("manifest.sig").decode("ascii", "replace").strip(), z.read("payload.bin")
    if not any(_verify(k, mbytes, sig) for k in trusted_keys):
        raise BundleError("the signature is not valid - this release was not made by the developer")
    try:
        manifest = json.loads(mbytes)
    except ValueError as e:
        raise BundleError("the manifest is damaged") from e
    if mbytes != _canonical(manifest):
        raise BundleError("the manifest is not in canonical form")
    p = manifest.get("payload") or {}
    if manifest.get("format") != FORMAT or not isinstance(manifest.get("build"), int) or manifest["build"] < 1:
        raise BundleError("unknown bundle format")
    if hashlib.sha256(payload).hexdigest() != p.get("sha256") or len(payload) != p.get("size"):
        raise BundleError("the payload does not match the signed manifest")
    return manifest, payload


def _verify(public_b64: str, data: bytes, sig_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_b64d(public_b64)).verify(_b64d(sig_b64), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def decrypt_files(manifest: dict, payload: bytes, content_key: bytes) -> dict[str, bytes]:
    """Open the payload (the app does this in memory only) -> {path: bytes}. Every path and size is checked first."""
    try:
        plain = AESGCM(content_key).decrypt(_b64d(manifest["payload"]["nonce"]), payload, b"bestfare-release/%d" % manifest["build"])
    except (InvalidTag, ValueError, KeyError, TypeError) as e:
        raise BundleError("the release could not be opened (wrong key or damaged)") from e
    if hashlib.sha256(plain).hexdigest() != manifest["payload"].get("plain_sha256"):
        raise BundleError("the opened release does not match its manifest")
    try:
        z = zipfile.ZipFile(io.BytesIO(plain))
    except zipfile.BadZipFile as e:
        raise BundleError("the release content is damaged") from e
    infos = z.infolist()
    if not infos or len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_PLAIN:
        raise BundleError("the release content has an impossible size")
    files: dict[str, bytes] = {}
    for info in infos:
        if not safe_path(info.filename) or info.filename in files or info.is_dir():
            raise BundleError(f"unsafe file name in the release: {info.filename!r}")
        files[info.filename] = z.read(info)
    return files


# ── the installer (the Windows setup program that puts the shell on a computer) ───────────────────────────────────────
# The installer is a big file that is only downloaded and run, never opened in memory: it carries the developer's signature over
# "which version" + "which file (sha256)". The server checks it when it is uploaded; the app checks it again before it runs one.
MAX_INSTALLER = 400 * 1024 * 1024
_VERSION = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def version_number(version: str) -> int:
    """'1.2.3' -> 1002003 (a number that only goes up with the version). ValueError when it is not a version like 1.2.3."""
    m = _VERSION.match(version or "")
    if not m:
        raise ValueError("a version looks like 1.2.3")
    a, b, c = (int(x) for x in m.groups())
    return a * 1_000_000 + b * 1_000 + c


def installer_message(version: str, sha256_hex: str) -> bytes:
    return b"BFSETUP1\n" + version.encode("ascii") + b"\n" + sha256_hex.lower().encode("ascii")


def sign_installer(version: str, sha256_hex: str, signing_key: Ed25519PrivateKey) -> str:
    return _b64e(signing_key.sign(installer_message(version, sha256_hex)))


def verify_installer(version: str, sha256_hex: str, sig_b64: str, trusted_keys: list[str]) -> bool:
    try:
        version_number(version)
        msg = installer_message(version, sha256_hex)
    except (ValueError, UnicodeEncodeError):
        return False
    return any(_verify(k, msg, sig_b64) for k in trusted_keys)
