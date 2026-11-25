r"""Add the DNS names Best Fare needs to a Cloudflare zone. It ONLY ADDS: an existing record is never changed or deleted.

    set CF_EMAIL=<the Cloudflare account email>   and   CF_KEY=<its Global API key>        (PowerShell:  $env:CF_EMAIL='...'; $env:CF_KEY='...')
    python deploy\cloudflare_dns.py --zone shizanvps.me --ip 203.0.113.10 --name bestfare --name bestfare-admin  [--dry-run]

(or CF_TOKEN=<an API token with DNS edit rights> instead of the two above.) The email and the key are read from the environment only, are sent to Cloudflare only,
and are never printed or saved. Records are made "DNS only" (grey cloud): visitors reach the server directly, so the server sees real addresses and the
certificate it gets from Let's Encrypt is the one browsers see.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"


class CloudflareError(Exception):
    pass


def headers() -> dict:
    token = os.environ.get("CF_TOKEN")
    if token:
        return {"Authorization": "Bearer " + token}
    email, key = os.environ.get("CF_EMAIL"), os.environ.get("CF_KEY")
    if not email or not key:
        raise CloudflareError("Set CF_EMAIL and CF_KEY (or CF_TOKEN) in the environment first - see the top of this file.")
    return {"X-Auth-Email": email, "X-Auth-Key": key}


def call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**headers(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except ValueError:
            data = {}
        msg = "; ".join(str(x.get("message")) for x in (data.get("errors") or []) if isinstance(x, dict)) or f"HTTP {e.code}"
        raise CloudflareError(f"Cloudflare said: {msg}") from None
    except (urllib.error.URLError, OSError) as e:
        raise CloudflareError("Cannot reach Cloudflare: " + str(e.reason if hasattr(e, "reason") else e)) from None
    if not data.get("success"):
        raise CloudflareError("Cloudflare refused: " + "; ".join(str(x.get("message")) for x in (data.get("errors") or []) if isinstance(x, dict)))
    return data


def plan(zone: str, ip: str, names: list[str]) -> list[dict]:
    """What to do for each name: add / already there / conflict (an existing record that is not exactly ours: left alone)."""
    z = call("GET", f"/zones?name={zone}")["result"]
    if not z:
        raise CloudflareError(f"The zone {zone} is not in this Cloudflare account.")
    zid = z[0]["id"]
    out = []
    for n in names:
        fqdn = n if n.endswith(zone) else f"{n}.{zone}"
        have = call("GET", f"/zones/{zid}/dns_records?name={fqdn}")["result"]
        if not have:
            out.append({"zone_id": zid, "name": fqdn, "action": "add"})
        elif any(r["type"] == "A" and r["content"] == ip and not r["proxied"] for r in have):
            out.append({"zone_id": zid, "name": fqdn, "action": "exists"})
        else:
            out.append({"zone_id": zid, "name": fqdn, "action": "conflict", "found": [f"{r['type']} {r['content']}" + (" (proxied)" if r["proxied"] else "") for r in have]})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zone", required=True)
    ap.add_argument("--ip", required=True)
    ap.add_argument("--name", action="append", required=True, help="a name to add (repeat), e.g. bestfare")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        steps = plan(a.zone, a.ip, a.name)
        bad = 0
        for s in steps:
            if s["action"] == "add":
                if a.dry_run:
                    print(f"would add   A {s['name']} -> {a.ip} (DNS only)")
                else:
                    call("POST", f"/zones/{s['zone_id']}/dns_records", {"type": "A", "name": s["name"], "content": a.ip, "ttl": 1, "proxied": False, "comment": "Best Fare"})
                    print(f"added       A {s['name']} -> {a.ip} (DNS only)")
            elif s["action"] == "exists":
                print(f"already     A {s['name']} -> {a.ip} (nothing to do)")
            else:
                bad += 1
                print(f"CONFLICT    {s['name']} already has: {', '.join(s['found'])} - left untouched")
        return 1 if bad else 0
    except CloudflareError as e:
        print("FAILED:", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
