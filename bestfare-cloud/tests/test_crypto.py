"""The cryptographic building blocks, against known answers and against tampering."""
import pytest

from bfcloud import crypto


def test_a_signature_only_verifies_for_the_exact_bytes_and_key():
    s = crypto.Signer.generate()
    sig = s.sign(b"hello")
    assert crypto.verify(s.public_b64, b"hello", sig)
    assert not crypto.verify(s.public_b64, b"hellp", sig)
    assert not crypto.verify(crypto.Signer.generate().public_b64, b"hello", sig)
    assert not crypto.verify(s.public_b64, b"hello", ("A" if sig[0] != "A" else "B") + sig[1:])      # (always a real change: a signature may by chance end in "AA")
    assert not crypto.verify(s.public_b64, b"hello", sig[:-4])
    assert not crypto.verify("not-a-key", b"hello", sig) and not crypto.verify(s.public_b64, b"hello", "")


def test_canonical_json_is_one_byte_string_whatever_the_order():
    assert crypto.canonical({"b": 1, "a": [1, 2]}) == crypto.canonical({"a": [1, 2], "b": 1}) == b'{"a":[1,2],"b":1}'
    with pytest.raises(ValueError):
        crypto.canonical({"x": float("nan")})


def test_a_key_is_delivered_to_exactly_one_app():
    priv, pub = crypto.new_ephemeral()
    wrapped = crypto.wrap_for(pub, b"K" * 32, b"ctx")
    assert crypto.unwrap_with(priv, wrapped, b"ctx") == b"K" * 32
    other, _ = crypto.new_ephemeral()
    with pytest.raises(ValueError):
        crypto.unwrap_with(other, wrapped, b"ctx")                        # another app cannot read it
    with pytest.raises(ValueError):
        crypto.unwrap_with(priv, wrapped, b"another context")             # nor can it be replayed for another build/device
    wrapped["ct"] = wrapped["ct"][:-4] + "AAAA"
    with pytest.raises(ValueError):
        crypto.unwrap_with(priv, wrapped, b"ctx")


def test_sealing_protects_secrets_at_rest():
    k = b"s" * 32
    sealed = crypto.seal(k, b"BF-KEY", b"a")
    assert crypto.unseal(k, sealed, b"a") == b"BF-KEY" and b"BF-KEY" not in sealed.encode()
    with pytest.raises(ValueError):
        crypto.unseal(k, sealed, b"b")
    with pytest.raises(ValueError):
        crypto.unseal(b"t" * 32, sealed, b"a")


def test_session_tokens_are_tamper_proof_and_expire():
    k = b"t" * 32
    t = crypto.token_make(k, {"l": 1, "exp": 2000.0})
    assert crypto.token_check(k, t, 1000.0)["l"] == 1
    assert crypto.token_check(k, t, 2001.0) is None                       # expired
    body, mac = t.split(".")
    assert crypto.token_check(k, crypto.b64e(crypto.canonical({"l": 2, "exp": 2000.0})) + "." + mac, 1000.0) is None   # edited payload
    assert crypto.token_check(b"u" * 32, t, 1000.0) is None
    for bad in ("", "x", "a.b", "....", None, 5):
        assert crypto.token_check(k, bad, 1000.0) is None                  # type: ignore[arg-type]


def test_licence_keys_look_right_and_are_found_however_they_are_typed():
    k = crypto.new_licence_key()
    assert k.startswith("BF-") and len(k) == 22 and k.count("-") == 4
    assert crypto.normalize_key(k) == k
    assert crypto.normalize_key(k.lower()) == k
    assert crypto.normalize_key(k.replace("-", " ")) == k
    assert crypto.normalize_key(k[3:].replace("-", "")) == k              # without the BF prefix
    for bad in ("", "BF-1234", k + "X", "BF-OOOO-IIII-0000-1111", "<script>", "' OR 1=1 --"):
        assert crypto.normalize_key(bad) == ""
    assert len({crypto.new_licence_key() for _ in range(500)}) == 500


def test_passwords_are_hashed_checked_and_judged():
    h = crypto.password_hash("Correct-Horse-9")
    assert crypto.password_check("Correct-Horse-9", h) and not crypto.password_check("correct-horse-9", h)
    assert crypto.password_hash("Correct-Horse-9") != h                    # (a new salt each time)
    assert "Correct" not in h
    assert not crypto.password_check("x", "garbage") and not crypto.password_check("x", "scrypt$1$a$b")
    assert crypto.password_problem("short") and crypto.password_problem("alllowercaseletters") and crypto.password_problem("Password123") is not None
    assert crypto.password_problem("Correct-Horse-9") == ""
    assert all(crypto.password_problem(crypto.random_password()) == "" for _ in range(20))


def test_totp_matches_the_rfc_test_vector_and_tolerates_one_step():
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert crypto.totp_code(secret, at=59) == "287082"                     # RFC 6238, SHA-1, T=59 -> 94287082 (last six digits)
    assert crypto.totp_check(secret, "287082", at=59) and crypto.totp_check(secret, "287 082", at=80)
    assert not crypto.totp_check(secret, "287082", at=59 + 120) and not crypto.totp_check(secret, "12345", at=59) and not crypto.totp_check(secret, "abcdef", at=59)
