"""A server that has no domain name is trusted by the fingerprint of its own certificate - and by nothing else."""
import ssl
import sys
from pathlib import Path

import pytest

import cloud
from cloud import CloudClient, CloudError
from device import DeviceKey, Vault
from livecloud import LiveServer

CLOUD_DEPLOY = Path(__file__).resolve().parents[2] / "bestfare-cloud" / "deploy"
sys.path.insert(0, str(CLOUD_DEPLOY))
import make_cert  # noqa: E402


@pytest.fixture()
def tls(tmp_path):
    crt, key = make_cert.make_self_signed("127.0.0.1", 30)
    (tmp_path / "s.crt").write_bytes(crt)
    (tmp_path / "s.key").write_bytes(key)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "s.crt", tmp_path / "s.key")
    srv = LiveServer(tmp_path / "cloud", ssl_context=ctx)

    class T:
        pass
    t = T()
    t.srv, t.pin, t.tmp = srv, make_cert.pin_of_pem(crt), tmp_path
    yield t
    srv.stop()


def client(t, pin, name="a"):
    return CloudClient(DeviceKey(Vault(t.tmp / f"v-{name}.bin")), 1, base_url=t.srv.url, server_public=t.srv.public, tls_pin=pin)


def test_the_right_pin_connects_even_though_no_authority_signed_the_certificate(tls):
    d = client(tls, tls.pin).hello()
    assert d["server"] == "bestfare" and d["pub"] == tls.srv.public


def test_a_pin_makes_the_python_and_the_makecert_tool_agree_on_the_fingerprint(tls):
    import socket
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", tls.srv.port)) as raw, ctx.wrap_socket(raw) as s:
        assert cloud.spki_pin(s.getpeercert(binary_form=True)) == tls.pin


def test_any_other_certificate_is_refused_even_a_perfectly_valid_looking_one(tls):
    other_crt, _ = make_cert.make_self_signed("127.0.0.1", 30)
    wrong = make_cert.pin_of_pem(other_crt)
    assert wrong != tls.pin
    with pytest.raises(CloudError) as e:
        client(tls, wrong).hello()
    assert e.value.code == "untrusted" and "not the real" in e.value.message.lower()


def test_without_a_pin_a_self_made_certificate_is_not_accepted(tls):
    with pytest.raises(CloudError) as e:
        client(tls, "").hello()
    assert e.value.code == "untrusted"                                   # the ordinary rules: no authority, no trust


def test_a_whole_sign_in_and_booking_runs_over_the_pinned_connection(tls):
    import licence as lic
    k = tls.srv.cloud.create_licence("Rahul", credits=2, actor="boss")
    c = client(tls, tls.pin, "b")
    L = lic.Licence(c, c.device.vault if hasattr(c.device, "vault") else Vault(tls.tmp / "v-b.bin"), "t")
    L.activate(k["key"])
    t = L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "ref-tls-0001")
    assert t.promo and L.account.credits == 1


def test_the_distribution_tool_stamps_a_pin_only_where_it_makes_sense(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import make_distribution as md
    import release_tools as rt
    out = md.make(tmp_path / "d", "https://203.0.113.7:8443", pin="ab" * 32)
    assert f'TLS_PIN = "{"ab" * 32}"' in (out / "trust.py").read_text()
    for bad_pin in ("xyz", "ab" * 31, "AB" * 33, '"; import os #'):
        with pytest.raises(rt.ReleaseError):
            md.make(tmp_path / "d2", "https://203.0.113.7:8443", pin=bad_pin)
    with pytest.raises(rt.ReleaseError):
        md.make(tmp_path / "d3", "http://127.0.0.1:8780", pin="ab" * 32)
