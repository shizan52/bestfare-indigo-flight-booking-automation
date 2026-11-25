r"""Publish a new version of Best Fare to every installed app.

    python tools\publish_release.py --server https://<server>/bestfare --notes "Fixes the payment step" [--live] [--required]

What it does: builds the release from this workspace, signs it with YOUR release key (%USERPROFILE%\.bestfare-keys\release_signing.pem -
it never leaves this computer), uploads it to the server as a DRAFT, and (with --live) makes it the current version. Apps update at their
next start or within minutes; use --required when old versions must update before their next booking (IndiGo changed something).

The server only ever stores and serves the bundle: it cannot make one, so nobody who gets into the server can push code to the apps.
Credentials: BESTFARE_ADMIN_USER / BESTFARE_ADMIN_PASS (an OWNER account) or you are asked.
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_tools as rt  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.environ.get("BESTFARE_ADMIN_URL"), help="the admin panel's address (no /admin)")
    ap.add_argument("--notes", default="", help="what changed, in a sentence (shown in the panel)")
    ap.add_argument("--build", type=int, help="the build number (default: the newest on the server + 1)")
    ap.add_argument("--version", default=None, help="the version text (default: buildinfo.VERSION)")
    ap.add_argument("--key", default=str(Path(os.environ["USERPROFILE"]) / ".bestfare-keys" / "release_signing.pem"))
    ap.add_argument("--live", action="store_true", help="make it the current version right away")
    ap.add_argument("--required", action="store_true", help="old versions must update before their next booking")
    ap.add_argument("--rollout", type=int, default=100, help="percent of computers that get it first (default 100)")
    ap.add_argument("--dry-run", action="store_true", help="only build and check; upload nothing")
    a = ap.parse_args()
    import buildinfo
    version = a.version or buildinfo.VERSION
    if a.dry_run:
        data, key, m = rt.build_release(a.build or 999, version, a.notes, Path(a.key))
        print(f"OK: build {m['build']} would be {len(data) // 1024} KB, {m['payload']['files']} files, signed. Nothing was uploaded.")
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
    build = a.build or (max([r["build"] for r in admin.releases()] or [0]) + 1)
    data, key, m = rt.build_release(build, version, a.notes, Path(a.key))
    print(f"Built build {build} ({version}): {len(data) // 1024} KB, {m['payload']['files']} files, signed.")
    r = admin.upload(data, key, a.notes)
    print("Uploaded as a draft:", r)
    if a.live:
        admin.edit(build, state="live", mandatory=a.required, rollout=a.rollout)
        print(f"Build {build} is LIVE" + (" and REQUIRED" if a.required else "") + f" for {a.rollout}% of computers.")
    else:
        print("It is a draft: open the admin panel > Updates and press Publish when you are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
