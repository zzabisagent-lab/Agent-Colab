"""Mattermost interactive-action callback contract (development plan §7.5, §7A.1; P0-10).

Callbacks arrive at ``/api/v1/providers/mattermost/actions``. Before any normalization or command
handler runs, the server validates: the integration token, a timestamp within ±5 minutes, a
one-time nonce, and the body hash — all bound together by an HMAC-SHA256 signature the server
itself embedded in the action ``context`` when it rendered the button. Every rejection is a
stable code with zero domain side effects (V-P0-16, V-P2-26).

``signature = HMAC-SHA256(key, f"{timestamp}|{nonce}|{sha256(body)}")`` (hex, lower-case).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass
from typing import Protocol

from server.domain.defaults import CALLBACK_TIMESTAMP_TOLERANCE_S

CALLBACK_SIGNATURE_INVALID = "CALLBACK_SIGNATURE_INVALID"
CALLBACK_TIMESTAMP_EXPIRED = "CALLBACK_TIMESTAMP_EXPIRED"
CALLBACK_NONCE_REUSED = "CALLBACK_NONCE_REUSED"
CALLBACK_BODY_HASH_MISMATCH = "CALLBACK_BODY_HASH_MISMATCH"


class CallbackError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class NonceStore(Protocol):
    """One-time nonce registry (DB-backed in Phase 2; in-memory in tests)."""

    def consume(self, nonce: str, expires_at: dt.datetime) -> bool:
        """Return True if the nonce was unseen and is now recorded, False if already used."""
        ...


class MemoryNonceStore:
    def __init__(self) -> None:
        self._seen: dict[str, dt.datetime] = {}

    def consume(self, nonce: str, expires_at: dt.datetime) -> bool:
        if nonce in self._seen:
            return False
        self._seen[nonce] = expires_at
        return True


@dataclass(frozen=True)
class CallbackEnvelope:
    """The security-relevant parts of an interactive action callback."""

    integration_token: str
    timestamp: int  # seconds since the epoch, embedded in the action context
    nonce: str
    body_sha256: str  # hash claimed in the context
    signature: str
    body: bytes  # raw request body as received


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def sign(key: bytes, timestamp: int, nonce: str, body_sha256: str) -> str:
    message = f"{timestamp}|{nonce}|{body_sha256}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def validate_callback(
    envelope: CallbackEnvelope,
    *,
    expected_token: str,
    signing_key: bytes,
    nonces: NonceStore,
    now: dt.datetime,
    tolerance_s: int = CALLBACK_TIMESTAMP_TOLERANCE_S,
) -> None:
    """Validate in a fixed order: token → timestamp → signature → body hash → nonce.

    The nonce is consumed last so a forged request can never burn a legitimate nonce.
    """
    if not hmac.compare_digest(envelope.integration_token, expected_token):
        raise CallbackError(CALLBACK_SIGNATURE_INVALID, "integration token")
    issued = dt.datetime.fromtimestamp(envelope.timestamp, tz=dt.UTC)
    if abs((now - issued).total_seconds()) > tolerance_s:
        raise CallbackError(CALLBACK_TIMESTAMP_EXPIRED, f"outside ±{tolerance_s}s")
    expected_sig = sign(signing_key, envelope.timestamp, envelope.nonce, envelope.body_sha256)
    if not hmac.compare_digest(envelope.signature, expected_sig):
        raise CallbackError(CALLBACK_SIGNATURE_INVALID, "hmac")
    if not hmac.compare_digest(body_digest(envelope.body), envelope.body_sha256):
        raise CallbackError(CALLBACK_BODY_HASH_MISMATCH)
    if not nonces.consume(envelope.nonce, issued + dt.timedelta(seconds=tolerance_s)):
        raise CallbackError(CALLBACK_NONCE_REUSED, envelope.nonce)
