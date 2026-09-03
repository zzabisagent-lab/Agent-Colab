"""Failure rate limits for authentication endpoints (development plan §8.1 pattern; P4-08).

Per source IP and per credential fingerprint: ``security.rate_limit_failures`` (6) failures within
``security.rate_limit_window_s`` (15 minutes) block the key for ``security.rate_limit_block_s``
(15 minutes) with HTTP 429 and one redacted audit entry per rejection. Keys never contain the
credential itself, only a fingerprint (first 16 hex chars of its SHA-256).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.api.errors import ApiError
from server.domain.clock import Clock
from server.observability.audit import append_audit
from server.security import policy as secpolicy


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RateLimitState:
    failures: int
    blocked_until: dt.datetime | None


_SELECT = "SELECT window_start, failures, blocked_until FROM auth_rate_limits WHERE scope_key = :k"


def _row(session: Session, key: str) -> RateLimitState | None:
    row = session.execute(text(_SELECT), {"k": key}).first()
    return None if row is None else RateLimitState(int(row[1]), row[2])


def check(
    session: Session,
    keys: list[str],
    *,
    clock: Clock,
    action: str,
    actor_label: str,
    correlation_id: str,
    workspace_id: uuid.UUID | None = None,
) -> None:
    """Raise 429 ``RATE_LIMITED`` (audited, redacted) when any key is blocked."""
    now = clock.now()
    for key in keys:
        state = _row(session, key)
        if state is not None and state.blocked_until is not None and state.blocked_until > now:
            retry = int((state.blocked_until - now).total_seconds()) + 1
            append_audit(
                session,
                action=f"{action}.rate_limited",
                target_type="auth",
                target_id=key.split(":", 1)[0],
                result="DENIED",
                actor_label=actor_label,
                correlation_id=correlation_id,
                workspace_id=workspace_id,
                error_code="RATE_LIMITED",
                metadata={"retry_after_s": retry},
                clock=clock,
            )
            session.commit()
            raise ApiError(
                429,
                "RATE_LIMITED",
                f"too many failures; retry in {retry} s",
                {"retry_after_s": retry},
            )


def record_failure(session: Session, keys: list[str], *, clock: Clock) -> None:
    now = clock.now()
    limit = secpolicy.int_value("security.rate_limit_failures")
    window = dt.timedelta(seconds=secpolicy.int_value("security.rate_limit_window_s"))
    block = dt.timedelta(seconds=secpolicy.int_value("security.rate_limit_block_s"))
    for key in keys:
        row = session.execute(
            text(
                "SELECT window_start, failures FROM auth_rate_limits WHERE "
                "scope_key = :k FOR UPDATE"
            ),
            {"k": key},
        ).first()
        if row is None or row[0] < now - window:
            failures, start = 1, now
        else:
            failures, start = int(row[1]) + 1, row[0]
        blocked = now + block if failures >= limit else None
        session.execute(
            text(
                "INSERT INTO auth_rate_limits (scope_key, window_start, failures, blocked_until) "
                "VALUES (:k, :w, :f, :b) ON CONFLICT (scope_key) DO UPDATE SET window_start = :w, "
                "failures = :f, blocked_until = :b"
            ),
            {"k": key, "w": start, "f": failures, "b": blocked},
        )


def reset(session: Session, keys: list[str]) -> None:
    for key in keys:
        session.execute(text("DELETE FROM auth_rate_limits WHERE scope_key = :k"), {"k": key})


def keys_for(action: str, client_ip: str, credential: str | None) -> list[str]:
    keys = [f"{action}:ip:{client_ip}"]
    if credential:
        keys.append(f"{action}:fp:{fingerprint(credential)}")
    return keys
