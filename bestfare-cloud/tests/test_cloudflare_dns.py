"""The DNS helper only ever ADDS, never changes or removes a record, and never prints the credentials."""
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import cloudflare_dns as cf  # noqa: E402

SECRET_KEY, SECRET_MAIL = "cfk_SECRET-KEY-VALUE", "someone@example.com"


class FakeCF:
    def __init__(self, records=None, fail=None):
        self.records, self.fail, self.calls = records or {}, fail, []

    def __call__(self, req, timeout=0):
        url = req.full_url.replace(cf.API, "")
        self.calls.append((req.get_method(), url, json.loads(req.data) if req.data else None, dict(req.header_items())))
        if self.fail:
            raise self.fail
        if url.startswith("/zones?name="):
            body = {"success": True, "result": [{"id": "zone1", "name": "example.com"}]}
        elif "/dns_records?name=" in url:
            body = {"success": True, "result": self.records.get(url.split("name=")[1], [])}
        elif req.get_method() == "POST":
            body = {"success": True, "result": {"id": "new"}}
        else:
            body = {"success": False, "errors": [{"message": "unexpected"}]}
        return io.BytesIO(json.dumps(body).encode())


@pytest.fixture()
def creds(monkeypatch):
    monkeypatch.setenv("CF_EMAIL", SECRET_MAIL)
    monkeypatch.setenv("CF_KEY", SECRET_KEY)
    monkeypatch.delenv("CF_TOKEN", raising=False)


def run(monkeypatch, capsys, fake, *argv):
    monkeypatch.setattr(cf.urllib.request, "urlopen", fake)
    code = cf.main(["--zone", "example.com", "--ip", "192.0.2.10", *argv])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_new_names_are_added_as_dns_only_and_nothing_else_is_called(creds, monkeypatch, capsys):
    fake = FakeCF()
    code, out = run(monkeypatch, capsys, fake, "--name", "app", "--name", "panel")
    assert code == 0 and "added       A app.example.com" in out and "added       A panel.example.com" in out
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert [p[2]["name"] for p in posts] == ["app.example.com", "panel.example.com"] and all(p[2]["proxied"] is False and p[2]["type"] == "A" and p[2]["content"] == "192.0.2.10" for p in posts)
    assert {c[0] for c in fake.calls} == {"GET", "POST"}                                  # never PUT / PATCH / DELETE


def test_an_existing_record_is_never_touched(creds, monkeypatch, capsys):
    ours = [{"type": "A", "content": "192.0.2.10", "proxied": False}]
    theirs = [{"type": "CNAME", "content": "other.example.net", "proxied": True}]
    fake = FakeCF({"app.example.com": ours, "panel.example.com": theirs})
    code, out = run(monkeypatch, capsys, fake, "--name", "app", "--name", "panel")
    assert code == 1 and "already     A app.example.com" in out and "CONFLICT    panel.example.com" in out and "left untouched" in out
    assert not [c for c in fake.calls if c[0] != "GET"]


def test_a_dry_run_changes_nothing(creds, monkeypatch, capsys):
    fake = FakeCF()
    code, out = run(monkeypatch, capsys, fake, "--name", "app", "--dry-run")
    assert code == 0 and "would add" in out and not [c for c in fake.calls if c[0] != "GET"]


def test_the_credentials_go_only_to_cloudflare_and_are_never_printed(creds, monkeypatch, capsys):
    fake = FakeCF()
    code, out = run(monkeypatch, capsys, fake, "--name", "app")
    assert SECRET_KEY not in out and SECRET_MAIL not in out
    sent = fake.calls[0][3]
    assert sent.get("X-auth-key") == SECRET_KEY and sent.get("X-auth-email") == SECRET_MAIL
    bad = FakeCF(fail=cf.urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(json.dumps({"errors": [{"message": "Invalid request headers"}]}).encode())))
    code, out = run(monkeypatch, capsys, bad, "--name", "app")
    assert code == 2 and "Invalid request headers" in out and SECRET_KEY not in out and SECRET_MAIL not in out


def test_without_credentials_it_says_what_to_set(monkeypatch, capsys):
    monkeypatch.delenv("CF_EMAIL", raising=False)
    monkeypatch.delenv("CF_KEY", raising=False)
    monkeypatch.delenv("CF_TOKEN", raising=False)
    code, out = run(monkeypatch, capsys, FakeCF(), "--name", "app")
    assert code == 2 and "CF_EMAIL" in out and "CF_KEY" in out
