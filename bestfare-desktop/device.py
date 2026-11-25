"""This computer's identity: a key pair of its own (kept sealed by Windows for this user on this machine), its MAC addresses, its
name. The server ties a licence key to the PUBLIC half of this key pair - a licence key alone is worthless on any other computer,
and a copy of this app's files on another computer has no private key to sign with.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ── Windows DPAPI: sealed for this Windows user on this computer ────────────────────────────────────────────────────
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, "ctypes.Array"]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def protect(data: bytes, entropy: bytes = b"bestfare/vault/1") -> bytes:
    if os.name != "nt":                                              # (development on another system: a plain, clearly marked file)
        return b"PLAIN:" + data
    src, _keep = _blob(data)
    ent, _keep2 = _blob(entropy)
    out = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
        raise OSError("Windows could not protect the data")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def unprotect(data: bytes, entropy: bytes = b"bestfare/vault/1") -> bytes:
    if data.startswith(b"PLAIN:"):
        if os.name == "nt":
            raise OSError("not protected")
        return data[6:]
    src, _keep = _blob(data)
    ent, _keep2 = _blob(entropy)
    out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
        raise OSError("Windows could not open the data (it belongs to another user or computer)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


class Vault:
    """One small protected file: {"device_key": ..., "licence_key": ..., "token": ...}."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            return json.loads(unprotect(self.path.read_bytes()).decode("utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(protect(json.dumps(data).encode("utf-8")))
        os.replace(tmp, self.path)

    def update(self, **fields) -> dict:
        d = self.load()
        for k, v in fields.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        self.save(d)
        return d

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


# ── the device key pair ─────────────────────────────────────────────────────────────────────────────────────────────
class DeviceKey:
    def __init__(self, vault: Vault):
        self.vault = vault
        d = vault.load()
        if d.get("device_key"):
            self._k = serialization.load_pem_private_key(d["device_key"].encode(), password=None)
        else:
            self._k = Ed25519PrivateKey.generate()
            pem = self._k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            vault.update(device_key=pem)

    def sign(self, data: bytes) -> bytes:
        return self._k.sign(data)

    @property
    def public_raw(self) -> bytes:
        return self._k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


# ── what the server is told about this computer ─────────────────────────────────────────────────────────────────────
_MAC = re.compile(r"\b([0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5})\b")


def mac_addresses() -> list[str]:
    """The MAC addresses of this computer's network adapters (the admin sees them and can lock a key to one)."""
    found: list[str] = []
    if os.name == "nt":
        try:
            out = subprocess.run(["getmac", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW).stdout
            for m in _MAC.findall(out):
                mac = m.replace("-", ":").lower()
                if mac not in found and mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
                    found.append(mac)
        except (OSError, subprocess.SubprocessError):
            pass
    node = uuid.getnode()
    if not (node >> 40) & 1:                                           # (a random, made-up address has this bit set: not a real adapter)
        mac = ":".join(f"{(node >> s) & 0xFF:02x}" for s in range(40, -1, -8))
        if mac not in found:
            found.append(mac)
    return found[:8]


def machine_guid() -> str:
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0])
        except OSError:
            pass
    return str(uuid.getnode())


def device_info(app_version: str = "") -> dict:
    macs = mac_addresses()
    return {"macs": macs, "name": platform.node()[:60], "os": f"{platform.system()} {platform.release()}"[:80],
            "guid": hashlib.sha256(("bestfare|" + machine_guid() + "|" + (macs[0] if macs else "")).encode()).hexdigest()[:32], "app": app_version[:30]}


def data_dir() -> Path:
    base = os.environ.get("BESTFARE_HOME") or os.path.join(os.environ.get("LOCALAPPDATA") or str(Path.home()), "BestFare")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p
