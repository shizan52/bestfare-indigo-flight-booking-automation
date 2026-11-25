r"""Make the certificate the server's front door (nginx) uses, when the server has only an IP address and no domain name.

    python deploy\make_cert.py --host 192.0.2.10 [--out secrets]

Writes server.crt and server.key into --out (the key is private: it goes to the server and nowhere else) and prints the PIN: the SHA-256 of the
certificate's public key. The PIN is public; it is stamped into the app (tools\make_distribution.py --pin ...), and from then on the app talks to
this server only - a network in between cannot pose as it even with a certificate from a real authority.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bfcloud.crypto import write_private  # noqa: E402


def make_self_signed(host: str, days: int = 3650) -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Best Fare server")])
    try:
        san = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        san = x509.DNSName(host)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def pin_of_pem(cert_pem: bytes) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="the server's IP address (or name)")
    ap.add_argument("--out", default="secrets")
    ap.add_argument("--days", type=int, default=3650)
    a = ap.parse_args()
    out = Path(a.out)
    if (out / "server.crt").exists() or (out / "server.key").exists():
        print("server.crt / server.key already exist there - they are not replaced (the PIN in every installed app belongs to them).")
        return 1
    crt, key = make_self_signed(a.host, a.days)
    write_private(out / "server.key", key)
    (out / "server.crt").write_bytes(crt)
    print("Written:", out / "server.crt", "and", out / "server.key", "(private: keep it out of the apps and out of any chat)")
    print("PIN:", pin_of_pem(crt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
