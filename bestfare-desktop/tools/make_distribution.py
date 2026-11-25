r"""Make the folder you give to a user.

    python tools\make_distribution.py OUT_DIR --server https://<server>/bestfare [--zip]

The folder holds ONLY the shell (sign-in, key handling, updater: ~60 KB of plain code) and run.py. The program itself - the booking engine, the
windows, the offer logic - is not in it: it arrives from the server, encrypted and signed, after the user's key has been accepted, and is opened
in memory only. (So there is nothing in the folder to copy, edit or share that books anything.)
The server's address is stamped into trust.py. The server's identity key and the developer's release PUBLIC key are already in trust.py.
"""
import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_tools as rt  # noqa: E402

HERE = rt.HERE

README = """Best Fare
=========

1. Install Python 3.10 or newer from python.org (tick "Add python to PATH"). Microsoft Edge is needed too (Windows has it).
2. Double-click run.bat   (or run:  python run.py)
3. The first time it sets itself up (a minute), then asks for your KEY - type the key your admin gave you.

The key works on ONE computer only. If you change computers, ask your admin to move it.
You need the internet: Best Fare checks your credits with the server for every booking.
"""


def check_address(server: str) -> str:
    """The address users will connect to. Anything that is not this very computer must be https: the offer code and the sign-in travel there."""
    m = re.fullmatch(r"(https?)://([A-Za-z0-9.\-]+|\[[0-9a-fA-F:]+\])(:[0-9]{1,5})?(/[A-Za-z0-9._~/\-]*)?", server.strip())
    if not m:
        raise rt.ReleaseError("the server address is not valid (like https://example.com/bestfare)")
    if m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "[::1]"):
        raise rt.ReleaseError("a server on the internet must be reached with https:// (the sign-in and the offer code are sent there)")
    return server.strip().rstrip("/")


def check_pin(pin: str) -> str:
    pin = (pin or "").strip().lower()
    if pin and not re.fullmatch(r"[0-9a-f]{64}", pin):
        raise rt.ReleaseError("the certificate PIN must be 64 hexadecimal characters (python deploy/make_cert.py prints it)")
    return pin


def make(out: Path, server: str, make_zip: bool = False, pin: str = "") -> Path:
    server = check_address(server)
    pin = check_pin(pin)
    if pin and not server.startswith("https://"):
        raise rt.ReleaseError("a certificate PIN only makes sense with an https:// address")
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in rt.SHELL_FILES:
        shutil.copy2(HERE / name, out / name)
    shutil.copy2(HERE / "requirements.txt", out / "requirements.txt")
    trust = (out / "trust.py").read_text(encoding="utf-8")
    trust, n = re.subn(r'^SERVER_URL = "[^"]*"', f'SERVER_URL = "{server.rstrip("/")}"', trust, flags=re.M)
    if n != 1:
        raise rt.ReleaseError("could not stamp the server address into trust.py")
    trust, n = re.subn(r'^TLS_PIN = "[^"]*"', f'TLS_PIN = "{pin}"', trust, flags=re.M)
    if n != 1:
        raise rt.ReleaseError("could not stamp the certificate PIN into trust.py")
    (out / "trust.py").write_text(trust, encoding="utf-8")
    (out / "run.bat").write_text("@echo off\r\ncd /d %~dp0\r\npython run.py %*\r\npause\r\n", encoding="utf-8")
    (out / "README.txt").write_text(README, encoding="utf-8")
    # the folder must not hold anything that books, nothing secret
    for f in out.rglob("*"):
        if f.is_file() and f.suffix == ".py":
            txt = f.read_text(encoding="utf-8")
            for pat in rt.SECRET_PATTERNS:
                if pat.search(txt):
                    raise rt.ReleaseError(f"{f.name} contains something that must never be shipped")
    leftovers = {p.name for p in out.glob("*.py")} - set(rt.SHELL_FILES)
    if leftovers:
        raise rt.ReleaseError(f"unexpected files in the distribution: {sorted(leftovers)}")
    if make_zip:
        z = out.with_suffix(".zip")
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(out.rglob("*")):
                if f.is_file():
                    zf.write(f, f"BestFare/{f.relative_to(out)}")
        return z
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--server", required=True, help="the server's address, e.g. https://example.com/bestfare")
    ap.add_argument("--pin", default="", help="the PIN of the server's own certificate (python deploy/make_cert.py prints it) - for a server with no domain name")
    ap.add_argument("--zip", action="store_true")
    a = ap.parse_args()
    r = make(Path(a.out), a.server, a.zip, a.pin)
    print("Distribution ready:", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
