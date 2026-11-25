"""What this app trusts - and nothing else.

The app believes a server answer ONLY if it is signed by SERVER_PUBLIC, and runs a release ONLY if it is signed by one of
RELEASE_PUBLIC. It does not matter which address the answer came from or what a network in between did to it: no valid signature,
no trust. (A copy that has these keys replaced is no longer Best Fare: it cannot sign in to the real server or get its updates.)

SERVER_URL is stamped into a distribution when it is built (tools/make_distribution.py); BESTFARE_SERVER overrides it for testing.
"""
import os

SERVER_PUBLIC = "0Kl3PvEC9ngxA1_xgVme8ikkMXlmytQOttTTcTTZ1l4"          # the server's identity (secrets/PUBLIC_KEYS.txt)
RELEASE_PUBLIC = ["KafrVss_l5mD98G9QoT5JyJUgkWBp56XkZ1AxW8imEU"]         # the developer's release-signing key(s)
SERVER_URL = "http://127.0.0.1:8780"                                       # <- stamped at distribution time
TLS_PIN = ""                                                               # <- stamped at distribution time when the server has its own certificate (no domain name):
                                                                           #    the SHA-256 of that certificate's public key. Empty = ordinary https checking.
CHANNEL = "stable"
SHELL_VERSION = "1.0.0"                                                    # <- stamped when the installer is built: the version of THIS installed program (the server offers a newer Setup when it has one)


def server_url() -> str:
    return (os.environ.get("BESTFARE_SERVER") or SERVER_URL).rstrip("/")
