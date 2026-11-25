r"""Install (or update) the Best Fare cloud on the server - it ADDS one service next to what is already there and touches nothing else.

    python deploy\deploy_cloud.py --host <ip> --ssh-key <path> [--user root] [--dry-run]          first install
    python deploy\deploy_cloud.py --host <ip> --ssh-key <path> --update                           new server code only (no secrets, no nginx)
    python deploy\deploy_cloud.py --host <ip> --ssh-key <path> --backup secrets\backups            download a backup of the server's data

What the first install does, step by step (--dry-run prints exactly this and connects to nothing):
  1. looks (read-only): operating system, python, free memory/disk, whether ports 8780 and 8443 are free, whether nginx is there
  2. a system user `bestfare` (no login) and two folders: /opt/bestfare-cloud (code, owned by root) and /var/lib/bestfare-cloud (data, 0700)
  3. the server code (the bfcloud folder) and a private python environment for it (/opt/bestfare-cloud/venv) - nothing is installed system-wide
  4. the server's identity key and the TLS certificate (made by deploy\make_cert.py); an existing identity is NEVER replaced
  5. the developer's release PUBLIC key (releases signed by anything else are refused) and the owner login of the admin panel
  6. the systemd service `bestfare-cloud` (127.0.0.1:8780 only) and a NEW nginx file /etc/nginx/conf.d/bestfare.conf on port 8443;
     `nginx -t` must pass before nginx is reloaded (a reload keeps every other site running); if it fails the new file is removed
  7. a check from this computer: the certificate's PIN, /healthz, and the server's signed answer
Nothing here edits another site's configuration, restarts another service, or changes the firewall (--open-firewall only ADDS port 8443).
Secrets (the identity key, the certificate key, the owner password) travel over ssh only and are never printed.

DOMAIN MODE (--hostname NAME): the public door is a name of its own with a real (Let's Encrypt) certificate on its own port 8443 - what a browser
trusts, so the download link has no warning. Instead of steps 4 and 6 it: checks that NAME points at this server and that no other site already uses it,
installs certbot (in its own private python environment: no system package) if it is missing, adds a plain-http door (name-matched, for the certificate check), gets the certificate, then adds the https door on 8443 (+ the renewal hook).
SITE MODE (--site NAME --admin-host NAME2): on a server that already runs Best Fare, ADD the public names on the ordinary https port 443 - the install page and the apps on NAME,
the admin panel on NAME2 - with one Let's Encrypt certificate for both, in a second nginx file (the first keeps working).
Before and after, a checksum of EVERY other nginx file is compared: if anything else changed, the run says so. (--open-firewall adds port 8443 only.)
"""
from __future__ import annotations

import argparse
import datetime
import json
import shlex
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from bfcloud import crypto  # noqa: E402

ACME_ROOT = "/var/www/bestfare-acme"
CERTBOT_HOME = "/opt/bestfare-certbot"
CERTBOT = f"{CERTBOT_HOME}/bin/certbot"
NGINX_FILE = "/etc/nginx/conf.d/bestfare.conf"
CODE = "/opt/bestfare-cloud"
DATA = "/var/lib/bestfare-cloud"
SERVICE = "bestfare-cloud"
PORT_APP, PORT_PUBLIC = 8780, 8443
RUN_REQUIREMENTS = "aiohttp>=3.10,<4\ncryptography>=43\n"
PY = f"cd {CODE} && runuser -u bestfare -- {CODE}/venv/bin/python -m bfcloud --data {DATA}"


class Step:
    def __init__(self, title: str, command: str = "", *, upload: list[tuple[Path, str]] | None = None, stdin_note: str = ""):
        self.title, self.command, self.upload, self.stdin_note = title, command, upload or [], stdin_note


class Remote:
    def __init__(self, host: str, user: str, key: str, dry: bool):
        self.host, self.user, self.key, self.dry = host, user, key, dry

    @property
    def base(self) -> list[str]:
        return ["-i", self.key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]

    def ssh(self, command: str, stdin: bytes | None = None, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh", *self.base, f"{self.user}@{self.host}", command], input=stdin, capture_output=capture, check=check, timeout=900)

    def scp(self, local: Path, remote: str) -> None:
        subprocess.run(["scp", "-q", *self.base, str(local), f"{self.user}@{self.host}:{remote}"], check=True, capture_output=True, timeout=600)


def pack_code(dest: Path) -> Path:
    """The server code as one archive (no caches, no tests, no secrets)."""
    out = dest / "bfcloud.tgz"
    with tarfile.open(out, "w:gz") as t:
        for p in sorted((ROOT / "bfcloud").rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                t.add(p, arcname=str(Path("bfcloud") / p.relative_to(ROOT / "bfcloud")).replace("\\", "/"))
    return out


def public_keys() -> dict[str, str]:
    out = {}
    for line in (ROOT / "secrets" / "PUBLIC_KEYS.txt").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def render(name: str, tmp: Path, **values: str) -> Path:
    """A deploy file with its {{PLACEHOLDERS}} filled in, written into the temporary folder."""
    text = (HERE / name).read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    out = tmp / name
    out.write_text(text, encoding="utf-8")
    return out


def valid_hostname(h: str) -> bool:
    import re
    return bool(re.fullmatch(r"(?=.{4,100}$)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}", h or ""))


OTHERS = "cd /etc/nginx && sha256sum $(ls conf.d/*.conf sites-enabled/* nginx.conf 2>/dev/null | grep -v bestfare) 2>/dev/null"


def domain_steps(a, tmp: Path, code: Step, env: Step, restart: Step) -> list[Step]:
    """The first install with a name of its own and a Let's Encrypt certificate (see the docstring)."""
    secrets = ROOT / "secrets"
    q = shlex.quote
    keys = public_keys()
    host = a.hostname
    unit = render("bestfare-cloud.service", tmp, PUBLIC_URL_ARG=f"--public-url https://{host}:{PORT_PUBLIC}")
    acme = render("bestfare-nginx-acme.conf", tmp, HOST=host)
    door = render("bestfare-nginx-domain.conf", tmp, HOST=host)
    reload_nginx = (f"set -e; F={NGINX_FILE}; [ -e $F ] && cp $F $F.bak; install -m 644 /tmp/bf_nginx $F; rm -f /tmp/bf_nginx; "
                    "if nginx -t >/dev/null 2>&1; then systemctl reload nginx; rm -f $F.bak; echo 'nginx reloaded'; "
                    "else nginx -t 2>&1 | tail -5; if [ -e $F.bak ]; then mv $F.bak $F; else rm -f $F; fi; echo 'nginx configuration was NOT changed'; exit 1; fi")
    return [
        Step("Look at the server (read-only)", (
            'echo "system: $(. /etc/os-release; echo $PRETTY_NAME)"; echo "python: $(python3 --version 2>&1)"; '
            'echo "memory free (MB): $(free -m | awk \'/Mem:/{print $7}\')"; echo "disk free (MB): $(df -m /opt | awk \'NR==2{print $4}\')"; '
            'echo "nginx: $(nginx -v 2>&1 | head -1)"; '
            f'echo "dns: $(getent ahostsv4 {q(host)} | awk \'NR==1{{print $1}}\')"; '
            f'if ss -ltnH "sport = :{PORT_PUBLIC}" | grep -q .; then echo "port {PORT_PUBLIC}: IN USE"; else echo "port {PORT_PUBLIC}: free"; fi; '
            f'echo "name in nginx: $(nginx -T 2>/dev/null | awk -v h={q(host)} \'/# configuration file/{{f=$4}} /^[ \\t]*server_name/ {{ for(i=2;i<=NF;i++) {{ n=$i; sub(/;$/,"",n); if (n==h) print f }} }}\' | sort -u | tr \'\\n\' \' \')"; '
            f'echo "service: $(systemctl is-active {SERVICE} 2>/dev/null || true)"; echo "certbot: $([ -x {CERTBOT} ] && echo ready || echo missing)"; echo "other-sites-before:"; {OTHERS}')),
        Step("Make sure python can create its private environment", "python3 -c 'import venv, ensurepip' 2>/dev/null || (apt-get update -qq && apt-get install -y -qq python3-venv)"),
        Step("Install certbot in its own private python environment (nothing system-wide) - it only fetches and renews this one certificate",
             f"set -e; [ -x {CERTBOT} ] || (python3 -m venv {CERTBOT_HOME} && {CERTBOT_HOME}/bin/pip install -q --disable-pip-version-check certbot)"),
        Step("Create the service user and the folders", (
            f"set -e; id bestfare >/dev/null 2>&1 || useradd --system --home-dir {DATA} --shell /usr/sbin/nologin bestfare; "
            f"install -d -m 755 {CODE}; install -d -m 700 -o bestfare -g bestfare {DATA}; install -d -m 755 {ACME_ROOT}")),
        code, env,
        Step("Place the server's identity key (an existing one is never replaced)", (
            f"set -e; if [ -e {DATA}/identity.pem ]; then rm -f /tmp/bf_identity.pem; echo 'identity already there - kept'; "
            f"else install -m 600 -o bestfare -g bestfare /tmp/bf_identity.pem {DATA}/identity.pem; shred -u /tmp/bf_identity.pem 2>/dev/null || rm -f /tmp/bf_identity.pem; echo 'identity placed'; fi"),
            upload=[(secrets / "server_identity.pem", "/tmp/bf_identity.pem")]),
        Step("Accept the developer's release PUBLIC key", f"{PY} trust-key {q(keys['release_signing'])}"),
        Step("Create the owner login of the admin panel (the password is made here, sent over ssh, and saved to secrets\\OWNER_LOGIN.txt)",
             f"{PY} create-owner {q(a.owner)} --password-stdin", stdin_note="the new owner password (not shown)"),
        Step("Install the service", f"set -e; install -m 644 /tmp/bf_unit /etc/systemd/system/{SERVICE}.service; rm -f /tmp/bf_unit",
             upload=[(unit, "/tmp/bf_unit")]),
        restart,
        Step(f"Add the plain-http door for {host} (a new file; for the certificate check only; `nginx -t` must pass, otherwise nothing is changed)", reload_nginx,
             upload=[(acme, "/tmp/bf_nginx")]),
        Step("Ask Let's Encrypt for the certificate - first a rehearsal (no certificate is issued), then the real one",
             f"set -e; {CERTBOT} certonly --webroot -w {ACME_ROOT} -d {q(host)} --non-interactive --agree-tos --register-unsafely-without-email --dry-run >/dev/null 2>&1 "
             f"|| ({CERTBOT} certonly --webroot -w {ACME_ROOT} -d {q(host)} --non-interactive --agree-tos --register-unsafely-without-email --dry-run 2>&1 | tail -12; exit 1); "
             f"{CERTBOT} certonly --webroot -w {ACME_ROOT} -d {q(host)} --non-interactive --agree-tos --register-unsafely-without-email --keep-until-expiring 2>&1 | tail -4"),
        Step("Renewal: a twice-daily check (cron) and nginx re-reads the certificate after each renewal",
             "set -e; install -d /etc/letsencrypt/renewal-hooks/deploy; printf '#!/bin/sh\\nnginx -t >/dev/null 2>&1 && systemctl reload nginx\\n' > /etc/letsencrypt/renewal-hooks/deploy/bestfare-nginx-reload.sh; "
             f"chmod 755 /etc/letsencrypt/renewal-hooks/deploy/bestfare-nginx-reload.sh; printf '17 3,15 * * * root {CERTBOT} renew --quiet\\n' > /etc/cron.d/bestfare-certbot; chmod 644 /etc/cron.d/bestfare-certbot"),
        Step(f"Add the https door for {host} (replaces the file above; `nginx -t` must pass, otherwise the previous file stays)", reload_nginx,
             upload=[(door, "/tmp/bf_nginx")]),
        Step("Allow port 8443 in the firewall (adds one rule; nothing else is changed)" if a.open_firewall else "Firewall: only a hint (use --open-firewall to add port 8443)",
             f"ufw allow {PORT_PUBLIC}/tcp" if a.open_firewall else f"ufw status 2>/dev/null | grep -q 'Status: active' && (ufw status | grep -q {PORT_PUBLIC} && echo 'port {PORT_PUBLIC}: open' || echo 'NOTE: ufw is active and port {PORT_PUBLIC} is not open - run again with --open-firewall') || echo 'no active ufw'"),
        Step("Check that no other site was touched (every other nginx file must be exactly as before)", f'echo "other-sites-after:"; {OTHERS}'),
    ]


SITE_FILE = "/etc/nginx/conf.d/bestfare-site.conf"


def site_steps(a, tmp: Path, code: Step, env: Step, restart: Step) -> list[Step]:
    """ADD the public names on the ordinary https port to a server that already runs Best Fare (domain mode or IP mode): the install page / the apps on a.site, the
    admin panel on a.admin_host. A second nginx file; the existing one (port 8443) keeps working, so installed apps do not break while they move over."""
    q = shlex.quote
    host, admin = a.site, a.admin_host
    unit = render("bestfare-cloud.service", tmp, PUBLIC_URL_ARG=f"--public-url https://{host}")
    acme = render("bestfare-site-acme.conf", tmp, HOST=host, ADMIN=admin)
    door = render("bestfare-nginx-site.conf", tmp, HOST=host, ADMIN=admin)
    reload_nginx = (f"set -e; F={SITE_FILE}; [ -e $F ] && cp $F $F.bak; install -m 644 /tmp/bf_nginx $F; rm -f /tmp/bf_nginx; "
                    "if nginx -t >/dev/null 2>&1; then systemctl reload nginx; rm -f $F.bak; echo 'nginx reloaded'; "
                    "else nginx -t 2>&1 | tail -5; if [ -e $F.bak ]; then mv $F.bak $F; else rm -f $F; fi; echo 'nginx configuration was NOT changed'; exit 1; fi")
    names = f"-d {q(host)} -d {q(admin)}"
    certbot = f"{CERTBOT} certonly --webroot -w {ACME_ROOT} {names} --non-interactive --agree-tos --register-unsafely-without-email"
    awk = ("awk -v h1=" + q(host) + " -v h2=" + q(admin) + " '/# configuration file/{f=$4} /^[ \\t]*server_name/ { for(i=2;i<=NF;i++) { n=$i; sub(/;$/,\"\",n); if (n==h1 || n==h2) print f } }'")
    return [
        Step("Look at the server (read-only)", (
            f'echo "dns {host}: $(getent ahostsv4 {q(host)} | awk \'NR==1{{print $1}}\')"; echo "dns {admin}: $(getent ahostsv4 {q(admin)} | awk \'NR==1{{print $1}}\')"; '
            f'echo "names in nginx: $(nginx -T 2>/dev/null | {awk} | sort -u | tr \'\\n\' \' \')"; '
            f'echo "service: $(systemctl is-active {SERVICE} 2>/dev/null || true)"; echo "certbot: $([ -x {CERTBOT} ] && echo ready || echo missing)"; '
            f'echo "other-sites-before:"; {OTHERS}')),
        code, env,
        Step("Update the service file (the public address the panel shows) and restart the service", (
            f"set -e; install -m 644 /tmp/bf_unit /etc/systemd/system/{SERVICE}.service; rm -f /tmp/bf_unit; systemctl daemon-reload; systemctl restart {SERVICE}; "
            f"for i in $(seq 1 25); do curl -fsS http://127.0.0.1:{PORT_APP}/healthz >/dev/null 2>&1 && exit 0; sleep 1; done; journalctl -u {SERVICE} -n 20 --no-pager; exit 1"),
             upload=[(unit, "/tmp/bf_unit")]),
        Step(f"Add the plain-http door for {host} and {admin} (a new file; for the certificate check only; `nginx -t` must pass, otherwise nothing is changed)", reload_nginx,
             upload=[(acme, "/tmp/bf_nginx")]),
        Step("Ask Let's Encrypt for one certificate for both names - first a rehearsal (no certificate is issued), then the real one",
             f"set -e; {certbot} --dry-run >/dev/null 2>&1 || ({certbot} --dry-run 2>&1 | tail -12; exit 1); {certbot} --keep-until-expiring 2>&1 | tail -4"),
        Step(f"Add the https doors: {host} (apps + install page) and {admin} (admin panel) - replaces the file above; `nginx -t` must pass, otherwise the previous file stays", reload_nginx,
             upload=[(door, "/tmp/bf_nginx")]),
        Step("Check that no other site was touched (every other nginx file must be exactly as before)", f'echo "other-sites-after:"; {OTHERS}'),
    ]


def verify_site(a, keys: dict[str, str]) -> bool:
    """The last check for site mode, from this computer, the way a browser and an app see it."""
    ctx = ssl.create_default_context()
    app, panel = f"https://{a.site}", f"https://{a.admin_host}"

    def status(url: str, method: str = "GET", data: bytes | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"} if data is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    ok = True

    def show(text: str, good: bool) -> None:
        nonlocal ok
        ok = ok and good
        print(f"  {text}: {good}")
    s, body = status(app + "/healthz")
    show(f"{a.site}: the certificate is trusted and /healthz answers", s == 200)
    s, body = status(app + "/install")
    show("the public install page is served", s == 200 and b"Best Fare" in body)
    nonce = crypto.random_token(12)
    req = urllib.request.Request(app + "/api/v1/hello", data=b"{}", headers={"X-BF-Nonce": nonce, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        env = json.loads(r.read())
    core = {k: env[k] for k in ("ok", "d", "ts", "n", "p")}
    show("the server proves it holds the identity key the apps trust", crypto.verify(keys["server_identity"], crypto.canonical(core), env["sig"]) and env["n"] == nonce)
    s, body = status(panel + "/admin/")
    show(f"{a.admin_host}: the admin panel's sign-in page is served", s == 200 and b"Best Fare" in body)
    show("the panel host serves nothing else (no apps API, no install page)", status(panel + "/api/v1/hello", "POST", b"{}")[0] == 404 and status(panel + "/install")[0] == 404)
    show("the app host does not serve the panel", status(app + "/admin/")[0] == 404 and status(app + "/admin/api/licences")[0] == 404)
    return ok


def steps(a, tmp: Path) -> list[Step]:
    secrets = ROOT / "secrets"
    q = shlex.quote
    keys = public_keys()
    code = Step("Put the server code in place", (
        f"set -e; mkdir -p {CODE}; cd {CODE}; rm -rf bfcloud.new; mkdir bfcloud.new; tar -xzf /tmp/bfcloud.tgz -C bfcloud.new; rm -f /tmp/bfcloud.tgz; "
        f"rm -rf bfcloud.old; if [ -d bfcloud ]; then mv bfcloud bfcloud.old; fi; mv bfcloud.new/bfcloud bfcloud; rm -rf bfcloud.new; "
        f"printf %s {q(RUN_REQUIREMENTS)} > requirements-run.txt; chown -R root:root {CODE}; chmod -R go-w {CODE}"),
        upload=[(pack_code(tmp), "/tmp/bfcloud.tgz")])
    env = Step("Create the private python environment and install the two libraries it needs", (
        f"set -e; cd {CODE}; [ -d venv ] || python3 -m venv venv; venv/bin/pip install -q --disable-pip-version-check --upgrade pip; "
        f"venv/bin/pip install -q --disable-pip-version-check -r requirements-run.txt"))
    restart = Step("Start the service and wait until it answers", (
        f"set -e; systemctl daemon-reload; systemctl enable {SERVICE} >/dev/null 2>&1; systemctl restart {SERVICE}; "
        f"for i in $(seq 1 25); do curl -fsS http://127.0.0.1:{PORT_APP}/healthz >/dev/null 2>&1 && exit 0; sleep 1; done; "
        f"journalctl -u {SERVICE} -n 20 --no-pager; exit 1"))
    if a.update and not getattr(a, "site", ""):
        return [Step("Look at the server (read-only)", f"systemctl is-active {SERVICE}; free -m | head -2"), code, env, restart]
    if getattr(a, "site", ""):
        return site_steps(a, tmp, code, env, restart)
    if getattr(a, "hostname", ""):
        return domain_steps(a, tmp, code, env, restart)
    unit_ip = render("bestfare-cloud.service", tmp, PUBLIC_URL_ARG=f"--public-url https://{getattr(a, 'host', '')}:{PORT_PUBLIC}")
    return [
        Step("Look at the server (read-only)", (
            'echo "system: $(. /etc/os-release; echo $PRETTY_NAME)"; echo "python: $(python3 --version 2>&1)"; '
            'echo "memory free (MB): $(free -m | awk \'/Mem:/{print $7}\')"; echo "disk free (MB): $(df -m /opt | awk \'NR==2{print $4}\')"; '
            'echo "nginx: $(nginx -v 2>&1 | head -1)"; '
            f'for p in {PORT_APP} {PORT_PUBLIC}; do if ss -ltnH "sport = :$p" | grep -q .; then echo "port $p: IN USE"; else echo "port $p: free"; fi; done; '
            f'echo "service: $(systemctl is-active {SERVICE} 2>/dev/null || true)"; echo "firewall: $(ufw status 2>/dev/null | head -1 || echo none)"')),
        Step("Make sure python can create its private environment", "python3 -c 'import venv, ensurepip' 2>/dev/null || (apt-get update -qq && apt-get install -y -qq python3-venv)"),
        Step("Create the service user and the two folders", (
            f"set -e; id bestfare >/dev/null 2>&1 || useradd --system --home-dir {DATA} --shell /usr/sbin/nologin bestfare; "
            f"install -d -m 755 {CODE}; install -d -m 700 -o bestfare -g bestfare {DATA}; install -d -m 750 /etc/ssl/bestfare")),
        code, env,
        Step("Place the server's identity key (an existing one is never replaced)", (
            f"set -e; if [ -e {DATA}/identity.pem ]; then rm -f /tmp/bf_identity.pem; echo 'identity already there - kept'; "
            f"else install -m 600 -o bestfare -g bestfare /tmp/bf_identity.pem {DATA}/identity.pem; shred -u /tmp/bf_identity.pem 2>/dev/null || rm -f /tmp/bf_identity.pem; echo 'identity placed'; fi"),
            upload=[(secrets / "server_identity.pem", "/tmp/bf_identity.pem")]),
        Step("Place the TLS certificate and its key", (
            "set -e; install -m 644 /tmp/bf_server.crt /etc/ssl/bestfare/server.crt; install -m 600 /tmp/bf_server.key /etc/ssl/bestfare/server.key; "
            "shred -u /tmp/bf_server.key 2>/dev/null || rm -f /tmp/bf_server.key; rm -f /tmp/bf_server.crt"),
            upload=[(secrets / "server.crt", "/tmp/bf_server.crt"), (secrets / "server.key", "/tmp/bf_server.key")]),
        Step("Accept the developer's release PUBLIC key", f"{PY} trust-key {q(keys['release_signing'])}"),
        Step("Create the owner login of the admin panel (the password is made here, sent over ssh, and saved to secrets\\OWNER_LOGIN.txt)",
             f"{PY} create-owner {q(a.owner)} --password-stdin", stdin_note="the new owner password (not shown)"),
        Step("Install the service", f"set -e; install -m 644 /tmp/bf_unit /etc/systemd/system/{SERVICE}.service; rm -f /tmp/bf_unit",
             upload=[(unit_ip, "/tmp/bf_unit")]),
        restart,
        Step("Add the nginx door on port 8443 (a new file; `nginx -t` must pass, otherwise the new file is removed and nothing is reloaded)", (
            "set -e; F=/etc/nginx/conf.d/bestfare.conf; [ -e $F ] && cp $F $F.bak; install -m 644 /tmp/bf_nginx $F; rm -f /tmp/bf_nginx; "
            "if nginx -t >/dev/null 2>&1; then systemctl reload nginx; echo 'nginx reloaded'; "
            "else nginx -t 2>&1 | tail -5; if [ -e $F.bak ]; then mv $F.bak $F; else rm -f $F; fi; echo 'nginx configuration was NOT changed'; exit 1; fi"),
            upload=[(HERE / "bestfare-nginx.conf", "/tmp/bf_nginx")]),
        Step("Allow port 8443 in the firewall" if a.open_firewall else "Firewall: only a hint (use --open-firewall to add port 8443)",
             f"ufw allow {PORT_PUBLIC}/tcp" if a.open_firewall else f"ufw status 2>/dev/null | grep -q 'Status: active' && (ufw status | grep -q {PORT_PUBLIC} && echo 'port {PORT_PUBLIC}: open' || echo 'NOTE: ufw is active and port {PORT_PUBLIC} is not open - run again with --open-firewall') || echo 'no active ufw'"),
    ]


def describe(s: Step, n: int) -> None:
    print(f"\n[{n}] {s.title}")
    for local, remote in s.upload:
        print(f"      upload  {local.name}  ->  {remote}")
    if s.command:
        print(f"      run     {s.command if len(s.command) < 600 else s.command[:600] + ' ...'}")
    if s.stdin_note:
        print(f"      stdin   {s.stdin_note}")


def verify_from_here(a, keys: dict[str, str]) -> bool:
    """The last check, made from this computer: the certificate is the one made for this server, and the server proves its identity."""
    import make_cert
    crt = (ROOT / "secrets" / "server.crt").read_bytes()
    want = make_cert.pin_of_pem(crt)
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    import socket
    with socket.create_connection((a.host, PORT_PUBLIC), timeout=15) as raw, ctx.wrap_socket(raw) as s:
        der = s.getpeercert(binary_form=True) or b""
        got = make_cert.pin_of_pem(ssl.DER_cert_to_PEM_cert(der).encode()) if der else ""
    print("  certificate PIN matches:", got == want)
    nonce = crypto.random_token(12)
    req = urllib.request.Request(f"https://{a.host}:{PORT_PUBLIC}/api/v1/hello", data=b"{}", headers={"X-BF-Nonce": nonce, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        env = json.loads(r.read())
    core = {k: env[k] for k in ("ok", "d", "ts", "n", "p")}
    signed = crypto.verify(keys["server_identity"], crypto.canonical(core), env["sig"]) and env["n"] == nonce
    print("  the server proves it holds the identity key the apps trust:", signed)
    return got == want and signed


def verify_domain(a, keys: dict[str, str]) -> bool:
    """The last check for domain mode, made from this computer the way an app and a browser see it: a certificate every computer trusts (no
    pin, no exceptions), the public page, and the server's signed proof of identity."""
    base = f"https://{a.hostname}:{PORT_PUBLIC}"
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(base + "/healthz", timeout=20, context=ctx) as r:
        ok_tls = r.status == 200
    print("  the certificate is trusted, /healthz answers:", ok_tls)
    with urllib.request.urlopen(base + "/install", timeout=20, context=ctx) as r:
        page = r.read().decode("utf-8", "replace")
    print("  the public install page is served:", "Best Fare" in page)
    nonce = crypto.random_token(12)
    req = urllib.request.Request(base + "/api/v1/hello", data=b"{}", headers={"X-BF-Nonce": nonce, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        env = json.loads(r.read())
    core = {k: env[k] for k in ("ok", "d", "ts", "n", "p")}
    signed = crypto.verify(keys["server_identity"], crypto.canonical(core), env["sig"]) and env["n"] == nonce
    print("  the server proves it holds the identity key the apps trust:", signed)
    try:
        urllib.request.urlopen(base + "/admin/", timeout=15, context=ctx)
        hidden = False
    except urllib.error.HTTPError as e:
        hidden = e.code == 404
    print("  the admin panel is NOT reachable from the internet:", hidden)
    return ok_tls and "Best Fare" in page and signed and hidden


def other_sites(out: str, marker: str) -> list[str]:
    """The checksum lines printed after `marker` (one line per other nginx file)."""
    if marker not in out:
        return []
    return sorted(l.strip() for l in out.split(marker, 1)[1].splitlines() if l.strip() and not l.strip().endswith(":"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True)
    ap.add_argument("--ssh-key", required=True)
    ap.add_argument("--user", default="root")
    ap.add_argument("--owner", default="boss", help="the admin panel's owner username (first install)")
    ap.add_argument("--update", action="store_true", help="only replace the server code and restart (nothing else is touched)")
    ap.add_argument("--hostname", default="", help="DOMAIN MODE: a name that points at this server (e.g. 192-3-164-209.sslip.io): a real certificate, no browser warning")
    ap.add_argument("--site", default="", help="SITE MODE: the public name for the install page and the apps (e.g. bestfare.example.com)")
    ap.add_argument("--admin-host", default="", help="SITE MODE: the name of the admin panel (e.g. bestfare-admin.example.com)")
    ap.add_argument("--open-firewall", action="store_true")
    ap.add_argument("--backup", metavar="DIR", help="download a backup of the server's data into DIR and do nothing else")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; connect to nothing")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    a = ap.parse_args(argv)
    if a.hostname and not valid_hostname(a.hostname):
        print("--hostname is not a valid host name.")
        return 2
    if a.site or a.admin_host:
        if not (a.site and a.admin_host) or not valid_hostname(a.site) or not valid_hostname(a.admin_host) or a.site == a.admin_host:
            print("--site and --admin-host must both be given, as two different valid host names.")
            return 2
    remote = Remote(a.host, a.user, a.ssh_key, a.dry_run)

    if a.backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = Path(a.backup)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"bestfare-cloud-{stamp}.tgz"
        cmd = f"set -e; B=/tmp/bf-backup-{stamp}; rm -rf $B; {PY} backup $B >/dev/null; tar -C /tmp -czf - bf-backup-{stamp}; rm -rf $B"
        if a.dry_run:
            print("would run on the server:", cmd, "\nand save the archive to", target)
            return 0
        res = remote.ssh(cmd)
        crypto.write_private(target, res.stdout)
        print("Backup saved:", target, f"({len(res.stdout) // 1024} KB) - it holds the server's secrets: keep it private, and keep a copy offline.")
        return 0

    with tempfile.TemporaryDirectory() as td:
        plan = steps(a, Path(td))
        print("Best Fare cloud -", ("ADD PUBLIC NAMES " + a.site + " + " + a.admin_host) if a.site else ("UPDATE" if a.update else "FIRST INSTALL" + (f" (name {a.hostname})" if a.hostname else "")), "on", a.host, "(dry run: nothing is executed)" if a.dry_run else "")
        for i, s in enumerate(plan, 1):
            describe(s, i)
        if a.dry_run:
            return 0
        missing = [local for st in plan for local, _r in st.upload if not local.exists()]
        if missing:
            print("\nThese local files are missing:", ", ".join(m.name for m in missing))
            if any(m.name.startswith("server.") for m in missing):
                print("Make the certificate first:  python deploy\\make_cert.py --host", a.host)
            return 1
        if not a.yes and input("\nType YES to run this on the server: ").strip() != "YES":
            print("Nothing was done.")
            return 1
        owner_pw = None
        before: list[str] = []
        for i, s in enumerate(plan, 1):
            print(f"\n[{i}/{len(plan)}] {s.title}")
            for local, rpath in s.upload:
                remote.scp(local, rpath)
            stdin = None
            if s.stdin_note:
                if (ROOT / "secrets" / "OWNER_LOGIN.txt").exists():
                    print("      the owner login file already exists here - the owner step is skipped")
                    continue
                owner_pw = crypto.random_password()
                stdin = (owner_pw + "\n").encode()
            res = remote.ssh(s.command, stdin=stdin, check=False)
            out = (res.stdout or b"").decode("utf-8", "replace").strip()
            if out:
                print("      " + out.replace("\n", "\n      "))
            if res.returncode != 0:
                err = (res.stderr or b"").decode("utf-8", "replace").strip()
                if owner_pw and "taken" in out:
                    print("      the owner already exists on the server - its password was not changed")
                    owner_pw = None
                    continue
                print("      FAILED:", err[-600:])
                return 1
            if s.title.startswith("Look at the server") and a.site:
                for name in (a.site, a.admin_host):
                    dns = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith(f"dns {name}:")), "")
                    if dns != a.host:
                        print(f"      {name} points at '{dns or 'nothing'}', not at {a.host} - the certificate check would fail. Stopping before anything is changed.")
                        return 1
                used = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("names in nginx:")), "")
                if used and any("bestfare" not in f for f in used.split()):
                    print(f"      one of these names is already used by another site ({used}) - stopping before anything is changed.")
                    return 1
                if "service: active" not in out:
                    print("      The Best Fare service is not running here - install it first (without --site). Stopping.")
                    return 1
                before = other_sites(out, "other-sites-before:")
            if s.title.startswith("Look at the server") and not a.update and not a.site:
                if f"port {PORT_PUBLIC}: IN USE" in out and "service: active" not in out:
                    print("      Port 8443 is used by something else - stopping before anything is changed.")
                    return 1
                if a.hostname:
                    dns = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("dns:")), "")
                    if dns != a.host:
                        print(f"      {a.hostname} points at '{dns or 'nothing'}', not at {a.host} - the certificate check would fail. Stopping before anything is changed.")
                        return 1
                    used = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("name in nginx:")), "")
                    if used and any("bestfare.conf" not in f for f in used.split()):
                        print(f"      {a.hostname} is already used by another site ({used}) - stopping before anything is changed.")
                        return 1
                    before = other_sites(out, "other-sites-before:")
            if s.title.startswith("Check that no other site was touched"):
                after = other_sites(out, "other-sites-after:")
                if not before or after != before:
                    print("      WARNING: another nginx file is different from before (or could not be compared) - look at it before going on.")
                    return 1
                print(f"      all {len(after)} other nginx files are byte-for-byte as before.")
        if owner_pw:
            crypto.write_private(ROOT / "secrets" / "OWNER_LOGIN.txt", f"username: {a.owner}\npassword: {owner_pw}\n".encode())
            print(f"\nOwner login saved in {ROOT / 'secrets' / 'OWNER_LOGIN.txt'} (private).")
        if not a.update:
            print("\nChecking from here:")
            ok = verify_site(a, public_keys()) if a.site else verify_domain(a, public_keys()) if a.hostname else verify_from_here(a, public_keys())
            print("\nDONE." if ok else "\nThe install finished but the final check failed - look at the lines above.")
            return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
