"""Enroll and confirm TOTP for a console test administrator through the API (P4-09)."""

from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs, urlparse

import httpx

from server.security.totp import totp


def enroll_totp(server: str, token: str, key_prefix: str) -> str:
    """Returns the base32 secret; the account can then verify on the console MFA screen."""
    h = {"Authorization": f"Bearer {token}"}
    r = httpx.post(
        f"{server}/api/v1/auth/mfa/enroll",
        json={},
        headers={**h, "Idempotency-Key": f"{key_prefix}-1"},
    )
    assert r.status_code in (200, 201), r.text
    secret = parse_qs(urlparse(r.json()["otpauth_uri"]).query)["secret"][0]
    r = httpx.post(
        f"{server}/api/v1/auth/mfa/confirm",
        json={"code": totp(secret, dt.datetime.now(dt.UTC))},
        headers={**h, "Idempotency-Key": f"{key_prefix}-2"},
    )
    assert r.status_code == 200, r.text
    return secret


def verify_totp(server: str, token: str, secret: str, key: str) -> None:
    r = httpx.post(
        f"{server}/api/v1/auth/mfa/verify",
        json={"code": totp(secret, dt.datetime.now(dt.UTC))},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )
    assert r.status_code == 200, r.text
