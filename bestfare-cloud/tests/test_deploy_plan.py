"""The installer, tested without a server: the plan it prints, the order it runs in, and that no secret is ever printed or put on a command line."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))
import deploy_cloud as dc  # noqa: E402
import make_cert  # noqa: E402

IDENTITY_MARK = b"-----BEGIN PRIVATE KEY-----\nTHE-SERVERS-SECRET-IDENTITY\n-----END PRIVATE KEY-----\n"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A copy of the workspace's layout: the real public keys, made-up secrets, a tiny server folder."""
    root = tmp_path / "ws"
    (root / "secrets").mkdir(parents=True)
    (root / "bfcloud").mkdir()
    (root / "bfcloud" / "__init__.py").write_text("# the server\n")
    shutil.copy2(DEPLOY.parent / "secrets" / "PUBLIC_KEYS.txt", root / "secrets" / "PUBLIC_KEYS.txt")
    (root / "secrets" / "server_identity.pem").write_bytes(IDENTITY_MARK)
    crt, key = make_cert.make_self_signed("192.0.2.10", 30)
    (root / "secrets" / "server.crt").write_bytes(crt)
    (root / "secrets" / "server.key").write_bytes(key)
    monkeypatch.setattr(dc, "ROOT", root)
    return root


class FakeSSH:
    """Stands in for ssh/scp: records everything, answers like a healthy server."""

    def __init__(self, in_use=False):
        self.calls, self.in_use = [], in_use

    def __call__(self, argv, input=None, capture_output=True, check=True, timeout=None):
        self.calls.append((list(argv), input))
        out = b""
        cmd = argv[-1] if argv[0] == "ssh" else ""
        if "ss -ltnH" in cmd:
            out = b"system: Ubuntu 24.04\nport 8780: free\nport 8443: " + (b"IN USE" if self.in_use else b"free") + b"\nservice: inactive\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr=b"")


def run(argv, capsys):
    code = dc.main(argv)
    return code, capsys.readouterr().out


def test_a_dry_run_shows_every_step_connects_to_nothing_and_shows_no_secret(workspace, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a dry run must not start any program"))
    code, out = run(["--host", "192.0.2.10", "--ssh-key", "k", "--dry-run"], capsys)
    assert code == 0
    for word in ("Look at the server", "service user", "python environment", "identity key", "TLS certificate", "release PUBLIC key", "owner login",
                 "Install the service", "nginx", "Firewall"):
        assert word in out, word
    assert "THE-SERVERS-SECRET-IDENTITY" not in out and "BEGIN" not in out and "PRIVATE" not in out.replace("private python", "")
    assert "never replaced" in out and "nginx -t" in out
    assert not any(w in out for w in ("rm -rf /var", "rm -rf /etc", "rm -rf /opt/bestfare-cloud ", "systemctl restart nginx", "systemctl stop", "apt-get remove", "apt-get purge"))


def test_the_installer_only_adds_it_edits_no_existing_site_and_restarts_nothing_else(workspace, capsys):
    plan = dc.steps(type("A", (), {"update": False, "open_firewall": False, "owner": "boss"})(), workspace)
    text = "\n".join(s.command for s in plan)
    assert "systemctl reload nginx" in text and "restart nginx" not in text
    assert "/etc/nginx/conf.d/bestfare.conf" in text and "sites-enabled" not in text and "nginx.conf " not in text
    assert text.count("restart") == 1                                            # only its own service
    assert "ufw allow" not in text                                               # the firewall is not touched without being asked


def test_an_update_touches_only_the_code(workspace, capsys, monkeypatch):
    plan = dc.steps(type("A", (), {"update": True, "open_firewall": False, "owner": "boss"})(), workspace)
    text = " ".join(s.command + " ".join(str(u[1]) for u in s.upload) for s in plan)
    assert "identity" not in text and "server.key" not in text and "nginx" not in text and "create-owner" not in text and "trust-key" not in text


def test_the_owner_password_travels_on_stdin_only_and_is_saved_privately_never_printed(workspace, monkeypatch, capsys):
    fake = FakeSSH()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(dc, "verify_from_here", lambda a, k: True)
    code, out = run(["--host", "192.0.2.10", "--ssh-key", "k", "--yes"], capsys)
    assert code == 0, out
    saved = (workspace / "secrets" / "OWNER_LOGIN.txt").read_text()
    pw = [l for l in saved.splitlines() if l.startswith("password: ")][0][len("password: "):]
    assert len(pw) >= 16 and pw not in out and "THE-SERVERS-SECRET-IDENTITY" not in out
    stdins = [i for _a, i in fake.calls if i]
    assert stdins == [(pw + "\n").encode()]                                      # exactly one thing was sent on stdin: the password
    assert not any(pw in " ".join(a) for a, _i in fake.calls)                    # never on a command line (anybody on the server could see it)
    ssh = [a[-1] for a, _i in fake.calls if a[0] == "ssh"]
    order = [next(i for i, c in enumerate(ssh) if w in c) for w in ("ss -ltnH", "useradd", "bfcloud.new", "venv/bin/pip", "identity", "trust-key", "create-owner",
                                                                         "daemon-reload", "nginx -t")]
    assert order == sorted(order)                                                # the order that makes sense: look, user, code, environment, secrets, owner, service, door


def test_a_busy_port_stops_everything_before_a_single_change(workspace, monkeypatch, capsys):
    fake = FakeSSH(in_use=True)
    monkeypatch.setattr(subprocess, "run", fake)
    code, out = run(["--host", "192.0.2.10", "--ssh-key", "k", "--yes"], capsys)
    assert code == 1 and "used by something else" in out
    ssh = [a[-1] for a, _i in fake.calls if a[0] == "ssh"]
    assert len(ssh) == 1 and "useradd" not in ssh[0]                              # only the read-only look was made


def test_missing_local_files_are_reported_before_connecting(workspace, monkeypatch, capsys):
    (workspace / "secrets" / "server.key").unlink()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not connect"))
    code, out = run(["--host", "192.0.2.10", "--ssh-key", "k", "--yes"], capsys)
    assert code == 1 and "server.key" in out and "make_cert.py" in out


def test_the_backup_command_saves_a_private_archive(workspace, monkeypatch, capsys, tmp_path):
    class Tar(FakeSSH):
        def __call__(self, argv, input=None, capture_output=True, check=True, timeout=None):
            self.calls.append((list(argv), input))
            return subprocess.CompletedProcess(argv, 0, stdout=b"TARBYTES" * 100, stderr=b"")
    fake = Tar()
    monkeypatch.setattr(subprocess, "run", fake)
    code, out = run(["--host", "192.0.2.10", "--ssh-key", "k", "--backup", str(tmp_path / "bk")], capsys)
    saved = list((tmp_path / "bk").glob("bestfare-cloud-*.tgz"))
    assert code == 0 and len(saved) == 1 and saved[0].read_bytes() == b"TARBYTES" * 100
    assert " backup " in fake.calls[0][0][-1] and "rm -rf $B" in fake.calls[0][0][-1]         # the copy on the server is removed again


def test_the_two_server_config_files_say_what_they_must(workspace):
    nginx = (DEPLOY / "bestfare-nginx.conf").read_text()
    assert "listen 8443 ssl" in nginx and "location /api/v1/" in nginx and "location /admin" not in nginx          # the panel is not published
    assert "proxy_set_header      X-Real-IP $remote_addr" in nginx and "return 404" in nginx and "client_max_body_size    300k" in nginx
    assert "TLSv1.2 TLSv1.3" in nginx and "server_tokens        off" in nginx and "limit_req" in nginx
    unit = (DEPLOY / "bestfare-cloud.service").read_text()
    assert "--host 127.0.0.1" in unit and "--behind-proxy" in unit and "User=bestfare" in unit
    for guard in ("NoNewPrivileges=true", "ProtectSystem=strict", "ReadWritePaths=/var/lib/bestfare-cloud", "ProtectHome=true", "MemoryMax="):
        assert guard in unit, guard


# ── domain mode: a name of its own, a real certificate, port 8443, no other site touched ───────────────────────────────
HOST = "192-3-164-209.sslip.io"
SUMS = "aaa111  conf.d/emt.conf\nbbb222  nginx.conf\n"


class DomainSSH(FakeSSH):
    """Answers the read-only look like a server would; `dns`, `used` and `after` let a test make something go wrong."""

    def __init__(self, dns="192.0.2.10", used="", after=SUMS, in_use=False):
        super().__init__(in_use)
        self.dns, self.used, self.after = dns, used, after

    def __call__(self, argv, input=None, capture_output=True, check=True, timeout=None):
        self.calls.append((list(argv), input))
        cmd = argv[-1] if argv[0] == "ssh" else ""
        out = b""
        if "ss -ltnH" in cmd:
            out = (f"system: Ubuntu 24.04\ndns: {self.dns}\nport 8443: {'IN USE' if self.in_use else 'free'}\nname in nginx: {self.used}\nservice: inactive\n"
                   f"certbot: missing\nother-sites-before:\n{SUMS}").encode()
        elif "other-sites-after" in cmd:
            out = ("other-sites-after:\n" + self.after).encode()
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr=b"")


def domain_args(*extra):
    return ["--host", "192.0.2.10", "--ssh-key", "k", "--hostname", HOST, *extra]


def test_a_domain_dry_run_adds_only_its_own_files_and_never_touches_another_site(workspace, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a dry run must not start any program"))
    code, out = run(domain_args("--dry-run", "--open-firewall"), capsys)
    assert code == 0 and HOST in out
    for word in ("private python environment", "certbot", "rehearsal", "plain-http door", "https door", "other nginx file", "8443"):
        assert word in out, word
    assert "THE-SERVERS-SECRET-IDENTITY" not in out and "server.key" not in out and "BEGIN" not in out
    assert not any(w in out for w in ("restart nginx", "systemctl stop", "apt-get remove", "rm -rf /etc/nginx", "conf.d/emt", "sites-enabled/default", "ufw allow 443", "ufw allow 80", "ufw reset"))
    assert out.count("ufw allow 8443/tcp") == 1 and "apt-get install -y -qq certbot" not in out         # certbot lives in its own folder, not in the system


def test_without_open_firewall_the_firewall_is_not_touched(workspace, capsys):
    code, out = run(domain_args("--dry-run"), capsys)
    assert code == 0 and "ufw allow" not in out and "--open-firewall" in out


def test_the_domain_door_matches_only_its_own_name_and_its_own_port(workspace, tmp_path):
    door = (DEPLOY / "bestfare-nginx-domain.conf").read_text()
    acme = (DEPLOY / "bestfare-nginx-acme.conf").read_text()
    for text in (door, acme):
        assert "server_name {{HOST}};" in text and "default_server" not in text and "server_name _" not in text
    assert "listen 8443 ssl;" in door and "listen 443" not in door and "/etc/letsencrypt/live/{{HOST}}/fullchain.pem" in door
    for loc in ("/api/v1/", "= /install", "= /download/BestFare-Setup.exe", "= /healthz"):
        assert f"location {loc}" in door or f"location = {loc.lstrip('= ')}" in door, loc
    assert "location /admin" not in door and "location ^~ /admin" not in door and "limit_req             zone=bestfare_dl" in door and "proxy_buffering       off" in door and "return 404" in door
    rendered = dc.render("bestfare-nginx-domain.conf", tmp_path, HOST=HOST).read_text()
    assert HOST in rendered and "{{" not in rendered
    unit = dc.render("bestfare-cloud.service", tmp_path, PUBLIC_URL_ARG=f"--public-url https://{HOST}:8443").read_text()
    assert f"--public-url https://{HOST}:8443" in unit and "{{" not in unit


def test_a_bad_host_name_is_refused_before_anything(workspace, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not connect"))
    for bad in ("localhost", "a b.com", "evil.com;rm -rf /", "x" * 200 + ".com", "-x.com"):
        code, out = run(["--host", "192.0.2.10", "--ssh-key", "k", f"--hostname={bad}", "--yes"], capsys)
        assert code == 2 and "not a valid host name" in out, bad


def test_a_name_that_points_somewhere_else_stops_before_any_change(workspace, monkeypatch, capsys):
    fake = DomainSSH(dns="203.0.113.9")
    monkeypatch.setattr(subprocess, "run", fake)
    code, out = run(domain_args("--yes"), capsys)
    assert code == 1 and "points at '203.0.113.9'" in out
    assert len([a for a, _i in fake.calls if a[0] == "ssh"]) == 1                         # only the read-only look


def test_a_name_another_site_already_uses_stops_before_any_change(workspace, monkeypatch, capsys):
    fake = DomainSSH(dns="192.0.2.10", used="/etc/nginx/conf.d/emt.conf:")
    monkeypatch.setattr(subprocess, "run", fake)
    code, out = run(domain_args("--yes"), capsys)
    assert code == 1 and "already used by another site" in out and len([a for a, _i in fake.calls if a[0] == "ssh"]) == 1


def test_if_any_other_nginx_file_differs_afterwards_the_run_says_so_and_fails(workspace, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", DomainSSH(after="aaa111  conf.d/emt.conf\nCHANGED  nginx.conf\n"))
    monkeypatch.setattr(dc, "verify_domain", lambda a, k: True)
    code, out = run(domain_args("--yes", "--open-firewall"), capsys)
    assert code == 1 and "another nginx file is different" in out


def test_a_clean_domain_run_compares_equal_saves_the_owner_login_privately_and_prints_no_secret(workspace, monkeypatch, capsys):
    fake = DomainSSH()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(dc, "verify_domain", lambda a, k: True)
    code, out = run(domain_args("--yes", "--open-firewall"), capsys)
    assert code == 0, out
    assert "byte-for-byte as before" in out
    pw = [l for l in (workspace / "secrets" / "OWNER_LOGIN.txt").read_text().splitlines() if l.startswith("password: ")][0][len("password: "):]
    assert pw not in out and "THE-SERVERS-SECRET-IDENTITY" not in out and not any(pw in " ".join(a) for a, _i in fake.calls)
    ssh = [a[-1] for a, _i in fake.calls if a[0] == "ssh"]
    order = [next(i for i, c in enumerate(ssh) if w in c) for w in ("ss -ltnH", "bestfare-certbot/bin/pip", "bfcloud.new", "trust-key", "create-owner", "daemon-reload",
                                                                   "--dry-run", "certonly", "ufw allow 8443", "other-sites-after")]
    assert order == sorted(order)                                                    # the certificate rehearsal comes before the real request, the door before the check


# ── site mode: the public names on the ordinary https port (install page + apps on one name, the admin panel on another) ──────────────────────────────────
APP, PANEL = "bestfare.example.com", "bestfare-admin.example.com"


class SiteSSH(FakeSSH):
    def __init__(self, dns="192.0.2.10", used="", after=SUMS, active=True):
        super().__init__()
        self.dns, self.used, self.after, self.active = dns, used, after, active

    def __call__(self, argv, input=None, capture_output=True, check=True, timeout=None):
        self.calls.append((list(argv), input))
        cmd = argv[-1] if argv[0] == "ssh" else ""
        out = b""
        if "other-sites-before" in cmd:
            out = (f"dns {APP}: {self.dns}\ndns {PANEL}: {self.dns}\nnames in nginx: {self.used}\nservice: {'active' if self.active else 'inactive'}\ncertbot: ready\n"
                   f"other-sites-before:\n{SUMS}").encode()
        elif "other-sites-after" in cmd:
            out = ("other-sites-after:\n" + self.after).encode()
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr=b"")


def site_args(*extra):
    return ["--host", "192.0.2.10", "--ssh-key", "k", "--site", APP, "--admin-host", PANEL, *extra]


def test_a_site_dry_run_adds_only_a_second_nginx_file_for_two_names(workspace, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a dry run must not start any program"))
    code, out = run(site_args("--dry-run"), capsys)
    assert code == 0 and APP in out and PANEL in out and "bestfare-site.conf" in out and "rehearsal" in out and "other nginx file" in out
    assert not any(w in out for w in ("restart nginx", "ufw", "conf.d/emt", "conf.d/bestfare.conf ", "rm -rf /etc/nginx", "identity", "create-owner", "trust-key"))
    assert "THE-SERVERS-SECRET-IDENTITY" not in out and "BEGIN" not in out


def test_both_names_are_required_and_must_differ(workspace, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not connect"))
    for argv in (["--site", APP], ["--admin-host", PANEL], ["--site", APP, "--admin-host", APP], ["--site", "x y", "--admin-host", PANEL]):
        code, out = run(["--host", "192.0.2.10", "--ssh-key", "k", "--yes", *argv], capsys)
        assert code == 2 and "--site and --admin-host" in out, argv


def test_the_site_door_gives_each_name_only_its_own_pages(workspace, tmp_path):
    conf = dc.render("bestfare-nginx-site.conf", tmp_path, HOST=APP, ADMIN=PANEL).read_text(encoding="utf-8")
    assert "{{" not in conf and "default_server" not in conf and "listen 443 ssl;" in conf
    app_block = conf.split(f"server_name {APP};")[1].split("# ── the admin panel")[0]
    panel_block = conf.split(f"server_name {PANEL};")[1]
    for loc in ("/api/v1/", "= /install", "= /download/BestFare-Setup.exe", "= /healthz"):
        assert f"location {loc}" in app_block, loc
    assert "/admin" not in app_block
    assert "/api/v1" not in panel_block and "/install" not in panel_block and "/download" not in panel_block
    for need in ("location = /admin/api/login", "zone=bfsite_login", "client_max_body_size  400m", "proxy_request_buffering off", "X-Forwarded-Proto https", "noindex", "return 404"):
        assert need in panel_block, need
    assert conf.count("X-Forwarded-Proto https") >= 8 and "proxy_set_header      X-Real-IP $remote_addr" in conf
    acme = dc.render("bestfare-site-acme.conf", tmp_path, HOST=APP, ADMIN=PANEL).read_text()
    assert f"server_name {APP} {PANEL};" in acme and "return 404" in acme and "listen 443" not in acme


def test_the_zones_of_the_site_file_do_not_clash_with_the_first_file():
    import re
    first = (DEPLOY / "bestfare-nginx-domain.conf").read_text()
    second = (DEPLOY / "bestfare-nginx-site.conf").read_text()
    a = set(re.findall(r"zone=(\w+):", first))
    b = set(re.findall(r"zone=(\w+):", second))
    assert a and b and not (a & b)                                              # (nginx refuses two files that define the same zone name)


@pytest.mark.parametrize("kwargs,words", [({"dns": "203.0.113.9"}, "points at '203.0.113.9'"), ({"used": "/etc/nginx/conf.d/emt.conf:"}, "already used by another site"),
                                           ({"active": False}, "service is not running")])
def test_a_site_run_stops_before_any_change_when_something_is_wrong(workspace, monkeypatch, capsys, kwargs, words):
    fake = SiteSSH(**kwargs)
    monkeypatch.setattr(subprocess, "run", fake)
    code, out = run(site_args("--yes"), capsys)
    assert code == 1 and words in out and len([a for a, _i in fake.calls if a[0] == "ssh"]) == 1


def test_a_clean_site_run_compares_equal_in_the_right_order_and_fails_if_another_file_changed(workspace, monkeypatch, capsys):
    fake = SiteSSH()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(dc, "verify_site", lambda a, k: True)
    code, out = run(site_args("--yes"), capsys)
    assert code == 0 and "byte-for-byte as before" in out, out
    ssh = [a[-1] for a, _i in fake.calls if a[0] == "ssh"]
    order = [next(i for i, c in enumerate(ssh) if w in c) for w in ("other-sites-before", "bfcloud.new", "daemon-reload", "--dry-run", "certonly", "other-sites-after")]
    assert order == sorted(order)
    bad = SiteSSH(after="aaa111  conf.d/emt.conf\nCHANGED  nginx.conf\n")
    monkeypatch.setattr(subprocess, "run", bad)
    code, out = run(site_args("--yes"), capsys)
    assert code == 1 and "another nginx file is different" in out
