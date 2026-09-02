"""Setup token guard (spec §12, development plan §8.1) — P0-05.

The token is CSPRNG ≥ 256 bits, shown once, stored only as its SHA-256 hash and an 8-hex
fingerprint, valid for 30 minutes, single-use. Failures are counted per ``(ip, presented-token
fingerprint)``; 5 failures within a 15-minute window block that source for 15 minutes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from server.domain.clock import Clock
from server.domain.defaults import (
    SETUP_TOKEN_BITS,
    SETUP_TOKEN_BLOCK_MIN,
    SETUP_TOKEN_FAILURE_WINDOW_MIN,
    SETUP_TOKEN_MAX_FAILURES,
    SETUP_TOKEN_TTL_MIN,
)
from server.setup.errors import SetupError

FINGERPRINT_HEX = 8


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def token_fingerprint(value: str) -> str:
    return token_hash(value)[:FINGERPRINT_HEX]


@dataclass(frozen=True)
class TokenRecord:
    """What may be persisted: never the token value."""

    token_hash: str
    token_fingerprint: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    used: bool = False

    def as_store_fields(self) -> dict[str, object]:
        return {
            "token_hash": self.token_hash,
            "token_fingerprint": self.token_fingerprint,
            "token_expires_at": self.expires_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "token_used": self.used,
        }


@dataclass(frozen=True)
class IssuedToken:
    value: str  # returned exactly once to the operator; never stored or logged
    record: TokenRecord

    def __repr__(self) -> str:  # pragma: no cover - defensive redaction
        return f"IssuedToken(fingerprint={self.record.token_fingerprint}, value=<redacted>)"


@dataclass
class _FailureState:
    timestamps: list[dt.datetime]
    blocked_until: dt.datetime | None = None


class SetupTokenGuard:
    def __init__(
        self,
        clock: Clock,
        ttl_minutes: int = SETUP_TOKEN_TTL_MIN,
        max_failures: int = SETUP_TOKEN_MAX_FAILURES,
        window_minutes: int = SETUP_TOKEN_FAILURE_WINDOW_MIN,
        block_minutes: int = SETUP_TOKEN_BLOCK_MIN,
    ) -> None:
        self._clock = clock
        self._ttl = dt.timedelta(minutes=ttl_minutes)
        self._max_failures = max_failures
        self._window = dt.timedelta(minutes=window_minutes)
        self._block = dt.timedelta(minutes=block_minutes)
        self._record: TokenRecord | None = None
        self._failures: dict[tuple[str, str], _FailureState] = {}

    @property
    def record(self) -> TokenRecord | None:
        return self._record

    def load(self, record: TokenRecord) -> None:
        """Restore the persisted hash record (e.g. from the sealed bootstrap store)."""
        self._record = record

    def issue(self) -> IssuedToken:
        """Issue a fresh single-use token; any previous token becomes invalid."""
        raw = secrets.token_bytes(SETUP_TOKEN_BITS // 8)
        value = raw.hex()
        now = self._clock.now()
        self._record = TokenRecord(
            token_hash=token_hash(value),
            token_fingerprint=token_fingerprint(value),
            issued_at=now,
            expires_at=now + self._ttl,
        )
        return IssuedToken(value=value, record=self._record)

    def blocked_until(self, ip: str, presented: str) -> dt.datetime | None:
        state = self._failures.get((ip, token_fingerprint(presented)))
        if state is None or state.blocked_until is None:
            return None
        if self._clock.now() >= state.blocked_until:
            return None
        return state.blocked_until

    def verify(self, presented: str, ip: str, consume: bool = True) -> TokenRecord:
        """Verify (and by default consume) the token. Every rejection is a stable code."""
        now = self._clock.now()
        key = (ip, token_fingerprint(presented))
        if self.blocked_until(ip, presented) is not None:
            raise SetupError("SETUP_TOKEN_BLOCKED", "source blocked after repeated failures")
        record = self._record
        try:
            if record is None or not hmac.compare_digest(record.token_hash, token_hash(presented)):
                raise SetupError("SETUP_TOKEN_INVALID", "token does not match")
            if record.used:
                raise SetupError("SETUP_TOKEN_USED", "token already consumed")
            if now >= record.expires_at:
                raise SetupError("SETUP_TOKEN_EXPIRED", "token expired")
        except SetupError:
            self._record_failure(key, now)
            raise
        if consume:
            self._record = TokenRecord(
                token_hash=record.token_hash,
                token_fingerprint=record.token_fingerprint,
                issued_at=record.issued_at,
                expires_at=record.expires_at,
                used=True,
            )
            return self._record
        return record

    def _record_failure(self, key: tuple[str, str], now: dt.datetime) -> None:
        state = self._failures.setdefault(key, _FailureState(timestamps=[]))
        state.timestamps = [t for t in state.timestamps if now - t < self._window]
        state.timestamps.append(now)
        if len(state.timestamps) >= self._max_failures:
            state.blocked_until = now + self._block
            state.timestamps = []

    def failure_counters(self) -> dict[str, dict[str, object]]:
        """Redacted counters suitable for the sealed store (ip + fingerprint only)."""
        out: dict[str, dict[str, object]] = {}
        for (ip, fp), state in self._failures.items():
            out[f"{ip}|{fp}"] = {
                "failures": len(state.timestamps),
                "blocked_until": (
                    state.blocked_until.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                    if state.blocked_until
                    else None
                ),
            }
        return out
