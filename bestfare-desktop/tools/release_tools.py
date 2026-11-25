"""Building a release from this workspace, and publishing it. Used by publish_release.py, make_distribution.py and the tests.

WHAT GOES INTO A RELEASE (and what does not):
  in   : the app's modules, the engine modules it shares with the bot, the airport list, the coupon rules (comments cleaned), a
         buildinfo.py with the real build number
  out  : the SHELL (sign-in, key, updater: these are in every distribution on disk), the tests, the tools, any secret
A release that would contain an offer code, a private key or a password is refused here, before it is ever signed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
CLOUD = HERE.parent / "bestfare-cloud"
ENGINE = HERE.parent / "indigo-bot"

# the plain-code shell: in every distribution, NEVER in a release
SHELL_FILES = ("trust.py", "device.py", "cloud.py", "licence.py", "shell.py", "shell_ui.py", "packed.py", "updater.py", "bundle.py", "run.py",
               "selfupdate.py", "setup_core.py", "update_ui.py", "autoupdate.py")
APP_MODULES = ("app.py", "browser.py", "choice_panel.py", "demo.py", "engine.py", "extension.py", "fares.py", "flights.py", "forms.py", "gate.py",
               "main_window.py", "paths.py", "resources.py", "selfcheck.py", "shadows.py", "stations.py", "store.py", "theme.py", "verify.py", "widgets.py")
ENGINE_MODULES = ("cdp.py", "human.py", "iframe_tools.py", "coupons.py", "indigo_client.py", "psp_flow.py", "airports.py", "speed.py")
RESOURCES = ("stations.json", "ext/core.js")

# A release must never carry a secret. The guard looks for the SHAPE of secrets (private keys, licence keys, offer codes); the owner adds the literal values of
# his own secrets here in his private copy.
SECRET_PATTERNS = [re.compile(p) for p in (r"-----BEGIN [A-Z ]*PRIVATE KEY", r"BF-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", r"\b6E[A-Z]{3}\d{3}\b")]


class ReleaseError(Exception):
    pass


def sanitize_core(data: bytes) -> bytes:
    """core.js documents the Defence offer's code in two comments. The release carries the same file with that code written as a
    placeholder (nothing else changes: the tests load it in Node and compare its behaviour)."""
    return data.replace(b"6ETST001", b"OFFER-1-CODE")


def collect() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for n in APP_MODULES:
        files[n] = (HERE / n).read_bytes()
    for n in ENGINE_MODULES:
        files[n] = (ENGINE / n).read_bytes()
    for n in RESOURCES:
        data = (HERE / n).read_bytes()
        files[n] = sanitize_core(data) if n == "ext/core.js" else data
    return files


def check_clean(files: dict[str, bytes]) -> None:
    for name, data in files.items():
        for pat in SECRET_PATTERNS:
            if pat.search(data.decode("utf-8", "replace")):
                raise ReleaseError(f"{name} contains something that must never be shipped ({pat.pattern[:24]}...)")
        if name in SHELL_FILES or name.startswith(("tests/", "tools/")):
            raise ReleaseError(f"{name} must not be part of a release")


def load_signing_key(path: Path):
    """The developer's release-signing key. When it has a passphrase (tools/protect_key.py) it is asked for - or read from BESTFARE_KEY_PASSWORD."""
    import getpass
    import os
    from cryptography.hazmat.primitives import serialization
    data = Path(path).read_bytes()
    try:
        return serialization.load_pem_private_key(data, password=None)
    except TypeError:                                            # (the file is encrypted)
        pw = os.environ.get("BESTFARE_KEY_PASSWORD") or getpass.getpass("Release key passphrase: ")
        try:
            return serialization.load_pem_private_key(data, password=pw.encode("utf-8"))
        except (ValueError, TypeError) as e:
            raise ReleaseError("The passphrase for the release key is not right.") from e


def protect_signing_key(path: Path, passphrase: str) -> None:
    """Re-save the signing key encrypted with a passphrase (atomically: the old file is replaced only when the new one is complete)."""
    import os
    from cryptography.hazmat.primitives import serialization
    if len(passphrase) < 12:
        raise ReleaseError("Use a passphrase of at least 12 characters.")
    path = Path(path)
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(passphrase.encode("utf-8")))
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(pem)
    os.replace(tmp, path)


def build_release(build_no: int, version: str, notes: str, signing_key_path: Path, channel: str = "stable", content_key: Optional[bytes] = None):
    from cryptography.hazmat.primitives import serialization
    import bundle
    files = collect()
    files["buildinfo.py"] = f'BUILD = {int(build_no)}\nVERSION = "{version}"\n'.encode()
    check_clean(files)
    key = load_signing_key(signing_key_path)
    return bundle.build(files, build_no=build_no, version=version, channel=channel, notes=notes, signing_key=key, content_key=content_key)


# ── talking to the admin API ────────────────────────────────────────────────────────────────────────────────────────
class Admin:
    def __init__(self, base: str, user: str, password: str, totp: str = ""):
        import http.cookiejar
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""
        r = self._json("POST", "/admin/api/login", {"username": user, "password": password, "totp": totp})
        self.csrf = r["csrf"]

    def _json(self, method: str, path: str, body: Optional[dict] = None, raw: Optional[bytes] = None, headers: Optional[dict] = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"X-CSRF": self.csrf, **(headers or {})}
        if body is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("message", "")
            except ValueError:
                msg = ""
            raise ReleaseError(msg or f"the server answered {e.code}") from e

    def releases(self) -> list[dict]:
        return self._json("GET", "/admin/api/releases")["releases"]

    def upload(self, data: bytes, content_key, notes: str) -> dict:
        """`content_key` is what build_release returns (32 raw bytes); the server wants it as URL-safe base64 in a header."""
        import base64
        key_b64 = content_key if isinstance(content_key, str) else base64.urlsafe_b64encode(content_key).rstrip(b"=").decode("ascii")
        return self._json("POST", "/admin/api/releases", raw=data, headers={"X-Release-Key": key_b64, "X-Release-Notes": notes.replace("\n", " ")[:1900],
                                                                          "Content-Type": "application/octet-stream"})

    def edit(self, build: int, **fields) -> None:
        self._json("PATCH", f"/admin/api/releases/{build}", fields)

    # the installer (the setup program people download; installed apps update themselves with it)
    def installers(self) -> dict:
        return self._json("GET", "/admin/api/installers")

    def upload_installer(self, path: Path, version: str, sig: str, notes: str) -> dict:
        """The setup file is streamed (it is large). The server checks the developer's signature before it keeps it."""
        size = Path(path).stat().st_size
        with open(path, "rb") as f:
            req = urllib.request.Request(self.base + "/admin/api/installers", data=f, method="POST", headers={
                "X-CSRF": self.csrf, "X-Installer-Version": version, "X-Installer-Sig": sig, "X-Installer-Notes": notes.replace(chr(10), " ")[:1900],
                "Content-Type": "application/octet-stream", "Content-Length": str(size)})
            try:
                with self.opener.open(req, timeout=1800) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    msg = json.loads(e.read()).get("message", "")
                except ValueError:
                    msg = ""
                raise ReleaseError(msg or f"the server answered {e.code}") from e

    def edit_installer(self, iid: int, **fields) -> None:
        self._json("PATCH", f"/admin/api/installers/{iid}", fields)
