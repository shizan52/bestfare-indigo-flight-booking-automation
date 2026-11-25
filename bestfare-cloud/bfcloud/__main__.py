"""Command line:  python -m bfcloud <command>

    serve         run the server (the app API and the admin panel)
    create-owner  create the first admin (prints a random password ONCE)
    trust-key     accept a developer release PUBLIC key (releases signed by anything else are refused)
    info          the server's public identity (this goes into the app) and the data folder
    backup        a consistent copy of everything (database, secrets, releases) into a folder - while the server runs
    add-installer register a signed installer file (the file is copied in; use --live to make it the one people download)
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from aiohttp import web

from . import crypto
from .service import Cloud, CloudError
from .web import Config, create_app


def backup(data: Path, dest: Path) -> int:
    """The database through SQLite's own backup (safe while the server writes), the secrets with owner-only rights, the releases as they are."""
    import shutil
    import sqlite3
    if not (data / "cloud.db").exists():
        print("There is nothing to back up in", data)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    try:
        dest.chmod(0o700)
    except OSError:
        pass
    src = sqlite3.connect(data / "cloud.db")
    dst = sqlite3.connect(dest / "cloud.db")
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    for name in ("identity.pem", "master.key"):
        f = data / name
        if f.exists() and not (dest / name).exists():
            crypto.write_private(dest / name, f.read_bytes())
    if (data / "release_keys.txt").exists():
        shutil.copy2(data / "release_keys.txt", dest / "release_keys.txt")
    if (data / "releases").exists():
        shutil.copytree(data / "releases", dest / "releases", dirs_exist_ok=True)
    print("Backup written to", dest, "- it holds the server's secrets: keep it private and offline.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bfcloud")
    ap.add_argument("--data", default="data", help="where the database and secrets live (default ./data)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8780)
    s.add_argument("--behind-proxy", action="store_true", help="nginx is in front: trust X-Real-IP from 127.0.0.1")
    s.add_argument("--secure-cookies", action="store_true", help="the panel is served over https")
    s.add_argument("--public-url", default=os.environ.get("BESTFARE_PUBLIC_URL", ""), help="where users reach this server, e.g. https://api.example.com:8443 (the panel shows the install link built from it)")
    o = sub.add_parser("create-owner")
    o.add_argument("username")
    o.add_argument("--password", help="(default: a random one is generated and printed once; avoid this option: other users of the computer can see it)")
    o.add_argument("--password-stdin", action="store_true", help="read the password from the first line of the standard input")
    t = sub.add_parser("trust-key")
    t.add_argument("public_key")
    sub.add_parser("info")
    b = sub.add_parser("backup")
    b.add_argument("dest", help="an empty or new folder")
    ai = sub.add_parser("add-installer")
    ai.add_argument("file")
    ai.add_argument("--version", required=True)
    ai.add_argument("--sig", required=True, help="the developer's signature (tools\\publish_installer.py prints it)")
    ai.add_argument("--notes", default="")
    ai.add_argument("--live", action="store_true")
    ai.add_argument("--mandatory", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    data = Path(a.data)
    if a.cmd == "info":
        c = Cloud(data)
        print("server identity (public):", c.identity.public_b64)
        print("data folder:", data.resolve())
        print("trusted release keys:", len(c.release_keys))
        return 0
    if a.cmd == "backup":
        return backup(data, Path(a.dest))
    if a.cmd == "add-installer":
        c = Cloud(data)
        try:
            out = c.add_installer(Path(a.file), a.version, a.sig, a.notes, "cli")
            if a.live or a.mandatory:
                c.edit_installer(out["id"], state="live" if a.live else None, mandatory=True if a.mandatory else None, actor="cli")
        except CloudError as e:
            print("Not accepted:", e.message)
            return 2
        print(f"Installer {out['version']} registered ({out['size'] // 1024 // 1024} MB){' and live' if a.live else ''}. sha256 {out['sha256']}")
        return 0
    if a.cmd == "trust-key":
        if not crypto.valid_public_key(a.public_key):
            print("That is not a valid public key.")
            return 2
        data.mkdir(parents=True, exist_ok=True)
        f = data / "release_keys.txt"
        have = f.read_text().splitlines() if f.exists() else []
        if a.public_key not in have:
            f.write_text("\n".join(have + [a.public_key]) + "\n")
        print("trusted release keys:", len(set(have + [a.public_key])))
        return 0
    if a.cmd == "create-owner":
        c = Cloud(data)
        pw = (sys.stdin.readline().rstrip("\r\n") if a.password_stdin else a.password) or crypto.random_password()
        try:
            c.create_admin(a.username, pw, "owner", actor="cli")
        except CloudError as e:
            print("Could not create the admin:", e.message)
            return 2
        print(f"Owner '{a.username}' created.")
        if not (a.password or a.password_stdin):
            print("Password (shown once, store it safely):", pw)
        return 0
    if a.public_url and not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?", a.public_url.rstrip("/")):
        print("--public-url must look like https://host or https://host:port")
        return 2
    cfg = Config(data_dir=data, host=a.host, port=a.port, behind_proxy=a.behind_proxy, secure_cookies=a.secure_cookies, public_url=a.public_url)
    web.run_app(create_app(Cloud(data), cfg), host=cfg.host, port=cfg.port, print=lambda *_: None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
