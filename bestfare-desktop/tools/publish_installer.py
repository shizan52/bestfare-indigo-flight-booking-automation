r"""Publish a new SETUP program (the installer people download, and the one installed apps update themselves with).

    python tools\publish_installer.py --server http://127.0.0.1:8780 [--file dist\BestFare-Setup.exe] [--notes "..."] [--live] [--required]

Build it first:  python tools\build_installer.py --server https://<address> --version 1.1.0 --notes "..."
That makes dist\BestFare-Setup.exe and dist\BestFare-Setup.json (version, SHA-256, and YOUR signature over them). This tool checks the signature
here, uploads the file to the admin API (the server checks it again) as a DRAFT, and with --live makes it the one the link gives out - from that
moment every installed app that is older updates itself. --required: they must update before they go on.

Like a release, the server cannot make a setup: only the holder of the release key can, so nobody who reaches the server can push a program to your users.
Credentials: BESTFARE_ADMIN_USER / BESTFARE_ADMIN_PASS (an OWNER account) or you are asked.
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_tools as rt  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.environ.get("BESTFARE_ADMIN_URL"), help="the admin panel's address (no /admin) - e.g. through an SSH tunnel")
    ap.add_argument("--file", default=str(rt.HERE / "dist" / "BestFare-Setup.exe"))
    ap.add_argument("--meta", default=None, help="the JSON next to the file (default: same name, .json)")
    ap.add_argument("--notes", default=None, help="what is new (default: the notes the build was made with)")
    ap.add_argument("--live", action="store_true", help="make it the one people download and apps update to, right away")
    ap.add_argument("--required", action="store_true", help="installed apps must update before they go on")
    ap.add_argument("--dry-run", action="store_true", help="only check the file and its signature")
    a = ap.parse_args()
    import bundle
    import trust
    path = Path(a.file)
    meta_path = Path(a.meta) if a.meta else path.with_suffix(".json")
    if not path.is_file() or not meta_path.is_file():
        print(f"Missing {path} or {meta_path} - build it first: python tools\build_installer.py")
        return 2
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if digest != meta.get("sha256") or not meta.get("sig"):
        print("The file does not match its signature record (or it was built without a key). Build it again.")
        return 2
    if not bundle.verify_installer(meta["version"], digest, meta["sig"], trust.RELEASE_PUBLIC):
        print("The signature is not made by the release key this app trusts. Nothing uploaded.")
        return 2
    notes = a.notes if a.notes is not None else ""
    print(f"OK: {path.name} version {meta['version']}, {path.stat().st_size / 1024 / 1024:.1f} MB, signature valid.")
    if a.dry_run:
        return 0
    if not a.server:
        print("Give the server address with --server (or BESTFARE_ADMIN_URL).")
        return 2
    user = os.environ.get("BESTFARE_ADMIN_USER") or input("Admin username: ")
    pw = os.environ.get("BESTFARE_ADMIN_PASS") or getpass.getpass("Admin password: ")
    totp = os.environ.get("BESTFARE_ADMIN_TOTP") or ""
    try:
        admin = rt.Admin(a.server, user, pw, totp)
    except rt.ReleaseError as e:
        if "6-digit" in str(e) and not totp:
            admin = rt.Admin(a.server, user, pw, input("Authenticator code: "))
        else:
            print("Could not sign in:", e)
            return 2
    try:
        out = admin.upload_installer(path, meta["version"], meta["sig"], notes)
    except rt.ReleaseError as e:
        print("The server did not accept it:", e)
        return 1
    print("Uploaded as a draft:", {k: out[k] for k in ("id", "version", "size") if k in out})
    if a.live:
        admin.edit_installer(out["id"], state="live", mandatory=a.required)
        print(f"Version {meta['version']} is LIVE" + (" and REQUIRED" if a.required else "") + ". The link now gives it out; installed apps will update.")
    else:
        print("It is a draft: open the admin panel > Updates > Installer and press Publish when you are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
