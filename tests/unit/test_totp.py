"""RFC 6238 vectors (SHA-1, 8 digits truncated to 6 for our default) and window behaviour."""

from __future__ import annotations

import datetime as dt

from server.security import totp

# RFC 6238 Appendix B secret "12345678901234567890" (base32) with SHA-1
RFC_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_rfc6238_sha1_vectors_match() -> None:
    vectors = {59: "94287082", 1111111109: "07081804", 1234567890: "89005924"}
    for ts, expected8 in vectors.items():
        at = dt.datetime.fromtimestamp(ts, tz=dt.UTC)
        code8 = totp.totp(RFC_SECRET_B32, at, digits=8)
        assert code8 == expected8
        assert totp.totp(RFC_SECRET_B32, at) == expected8[-6:]


def test_verify_accepts_one_step_window_and_rejects_beyond() -> None:
    secret = totp.new_secret()
    at = dt.datetime(2026, 5, 1, 9, 0, 0, tzinfo=dt.UTC)
    code = totp.totp(secret, at)
    assert totp.verify(secret, code, at)
    assert totp.verify(secret, code, at + dt.timedelta(seconds=30))
    assert totp.verify(secret, code, at - dt.timedelta(seconds=30))
    assert not totp.verify(secret, code, at + dt.timedelta(seconds=61))
    assert not totp.verify(secret, "000000", at) or code == "000000"
    assert not totp.verify(secret, "12345", at)
    assert not totp.verify(secret, "abcdef", at)


def test_otpauth_uri_has_required_fields_and_no_extra_secret_material() -> None:
    uri = totp.otpauth_uri("ABCDEFGHIJKLMNOP", "acct-owner", "Agent-Colab")
    assert uri.startswith("otpauth://totp/Agent-Colab%3Aacct-owner?")
    assert "secret=ABCDEFGHIJKLMNOP" in uri and "period=30" in uri and "digits=6" in uri
