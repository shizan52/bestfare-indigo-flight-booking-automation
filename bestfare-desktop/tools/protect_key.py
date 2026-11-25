r"""Put a passphrase on the release-signing key, so that the file alone (a stolen laptop, a copied folder) cannot sign anything.

    python tools\protect_key.py [--key %USERPROFILE%\.bestfare-keys\release_signing.pem]

From then on tools\publish_release.py asks for the passphrase (or reads BESTFARE_KEY_PASSWORD). If the passphrase is ever forgotten the key
cannot be recovered: a new key would have to be made, the apps rebuilt with its public half, and every user would install that once by hand.
Keep the passphrase and a copy of the key file somewhere safe and OFFLINE.
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
    ap.add_argument("--key", default=str(Path(os.environ["USERPROFILE"]) / ".bestfare-keys" / "release_signing.pem"))
    a = ap.parse_args()
    p1 = getpass.getpass("New passphrase (12+ characters): ")
    if p1 != getpass.getpass("Again: "):
        print("The two did not match. Nothing was changed.")
        return 1
    try:
        rt.protect_signing_key(Path(a.key), p1)
    except (rt.ReleaseError, ValueError, TypeError) as e:
        print(f"Nothing was changed: {e}")
        return 1
    print("Done. The release key is now protected by your passphrase.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
