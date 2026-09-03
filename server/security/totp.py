"""RFC 6238 TOTP (30-second step, 6 digits, SHA-1 by default) on the standard library only."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets
import struct
import urllib.parse

STEP_S = 30
DIGITS = 6
WINDOW = 1  # ±1 step tolerance


def new_secret(nbytes: int = 20) -> str:
    """Base32 secret (160 bits by default) for authenticator apps."""
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii").rstrip("=")


def _key(secret_b32: str) -> bytes:
    padded = secret_b32.upper() + "=" * (-len(secret_b32) % 8)
    return base64.b32decode(padded)


def hotp(secret_b32: str, counter: int, *, digits: int = DIGITS, algorithm: str = "sha1") -> str:
    digest = hmac.new(_key(secret_b32), struct.pack(">Q", counter), getattr(hashlib, algorithm))
    mac = digest.digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return f"{code:0{digits}d}"


def totp(secret_b32: str, at: dt.datetime, *, step_s: int = STEP_S, **kw: object) -> str:
    counter = int(at.timestamp()) // step_s
    return hotp(secret_b32, counter, **kw)  # type: ignore[arg-type]


def verify(
    secret_b32: str, code: str, at: dt.datetime, *, window: int = WINDOW, step_s: int = STEP_S
) -> bool:
    """Constant-time comparison over the ±window steps around ``at``."""
    if not code or not code.isdigit() or len(code) != DIGITS:
        return False
    counter = int(at.timestamp()) // step_s
    ok = False
    for delta in range(-window, window + 1):
        if hmac.compare_digest(hotp(secret_b32, counter + delta), code):
            ok = True
    return ok


def otpauth_uri(secret_b32: str, account_label: str, issuer: str) -> str:
    label = urllib.parse.quote(f"{issuer}:{account_label}", safe="")
    query = urllib.parse.urlencode(
        {
            "secret": secret_b32,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP_S,
        }
    )
    return f"otpauth://totp/{label}?{query}"
