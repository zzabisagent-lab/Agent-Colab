"""HMAC-SHA256 signing and verification for REST/Webhook push delivery (development plan §7B.2).

signature = hex(HMAC-SHA256(key, f"{timestamp}.{nonce}.{sha256hex(body)}"))
Verification rejects with stable codes: WEBHOOK_SIGNATURE_INVALID, WEBHOOK_TIMESTAMP_EXPIRED,
WEBHOOK_NONCE_REUSED, WEBHOOK_BODY_HASH_MISMATCH, WEBHOOK_HEADER_MISSING. The signing key is a
Secret Broker reference resolved by the caller; it is never logged.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Protocol

from server.domain import defaults
from server.domain.clock import Clock

HEADER_TIMESTAMP = "X-Colab-Timestamp"
HEADER_NONCE = "X-Colab-Nonce"
HEADER_SIGNATURE = "X-Colab-Signature"
HEADER_KEY_REF = "X-Colab-Key-Ref"


class WebhookError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class NonceStore(Protocol):
    """Remembers nonces for 24 h; ``remember`` returns False if the nonce was already seen."""

    def remember(self, nonce: str, now: dt.datetime) -> bool: ...


@dataclass
class InMemoryNonceStore:
    retention: dt.timedelta = dt.timedelta(hours=defaults.WEBHOOK_NONCE_RETENTION_H)

    def __post_init__(self) -> None:
        self._seen: dict[str, dt.datetime] = {}

    def remember(self, nonce: str, now: dt.datetime) -> bool:
        cutoff = now - self.retention
        for key in [k for k, seen_at in self._seen.items() if seen_at < cutoff]:
            del self._seen[key]
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def signing_string(timestamp: str, nonce: str, body: bytes) -> str:
    return f"{timestamp}.{nonce}.{body_sha256(body)}"


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def sign(
    key: bytes, body: bytes, clock: Clock, *, key_ref: str, nonce: str | None = None
) -> dict[str, str]:
    """Return the four signing headers for ``body``."""
    timestamp = str(int(clock.now().timestamp()))
    nonce = nonce or new_nonce()
    mac = hmac.new(key, signing_string(timestamp, nonce, body).encode(), hashlib.sha256)
    return {
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: mac.hexdigest(),
        HEADER_KEY_REF: key_ref,
    }


def verify(
    key: bytes,
    headers: dict[str, str],
    body: bytes,
    clock: Clock,
    nonce_store: NonceStore,
    *,
    body_sha256_claim: str | None = None,
    window_s: int = defaults.WEBHOOK_TIMESTAMP_WINDOW_S,
) -> None:
    """Validate timestamp window, signature, body hash, and nonce uniqueness (in that order).

    ``body_sha256_claim`` is an optional digest the sender declared inside the body; if present
    and different from the received body, ``WEBHOOK_BODY_HASH_MISMATCH`` is raised.
    """
    try:
        timestamp = headers[HEADER_TIMESTAMP]
        nonce = headers[HEADER_NONCE]
        signature = headers[HEADER_SIGNATURE]
    except KeyError as exc:
        raise WebhookError("WEBHOOK_HEADER_MISSING", str(exc)) from None
    if not timestamp.isdigit():
        raise WebhookError("WEBHOOK_TIMESTAMP_EXPIRED", "timestamp not numeric")
    now_s = int(clock.now().timestamp())
    if abs(now_s - int(timestamp)) > window_s:
        raise WebhookError("WEBHOOK_TIMESTAMP_EXPIRED", f"outside ±{window_s}s")
    expected = hmac.new(key, signing_string(timestamp, nonce, body).encode(), hashlib.sha256)
    if not hmac.compare_digest(expected.hexdigest(), signature.lower()):
        raise WebhookError("WEBHOOK_SIGNATURE_INVALID")
    if body_sha256_claim is not None and not hmac.compare_digest(
        body_sha256_claim.lower(), body_sha256(body)
    ):
        raise WebhookError("WEBHOOK_BODY_HASH_MISMATCH")
    if not nonce_store.remember(nonce, clock.now()):
        raise WebhookError("WEBHOOK_NONCE_REUSED")
