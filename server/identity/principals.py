"""Credential-derived principals (development plan §3.1 Identity, spec §15.2, V-P1-08).

The actor of every request is resolved ONLY from the presented credential: a service token, a
Human session, or a verified external identity link. Identity claims carried in a request body or
custom header are ignored and audited as spoof attempts. Tokens are random 256-bit values and are
stored only as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock
from server.observability.audit import append_audit

SPOOF_BODY_KEYS = frozenset(
    {"actor_account_id", "on_behalf_of", "actor", "impersonate", "as_account"}
)
SPOOF_HEADERS = frozenset({"x-colab-actor", "x-actor-account", "x-on-behalf-of", "x-impersonate"})
SESSION_COOKIE = "agent_colab_session"


class IdentityError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Principal:
    """The authenticated actor. ``account_uuid`` is the DB id; ``account_id`` the public id."""

    account_id: str
    account_uuid: str
    account_type: str
    credential_fingerprint: str
    credential_kind: str = "service_token"  # service_token | session | external_link
    mfa_verified: bool = False
    reauth_at: str | None = None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits


def fingerprint_of(token: str) -> str:
    return "sha256:" + hashlib.sha256(("fp:" + token_hash(token)).encode()).hexdigest()


# ------------------------------------------------------------------ service tokens
def issue_service_token(
    session: Session,
    account_id: str,
    *,
    actor_label: str,
    correlation_id: str,
    clock: Clock | None = None,
) -> tuple[str, str]:
    """Create an active service credential; returns (plaintext token, fingerprint)."""
    account = session.execute(
        text("SELECT id, workspace_id FROM accounts WHERE account_id = :a AND status = 'ACTIVE'"),
        {"a": account_id},
    ).first()
    if account is None:
        raise IdentityError("ACCOUNT_NOT_FOUND", account_id)
    token = new_token()
    fp = fingerprint_of(token)
    session.execute(
        text(
            "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash, status) "
            "VALUES (:id, :acc, :fp, :h, 'active')"
        ),
        {"id": uuid.uuid4(), "acc": account[0], "fp": fp, "h": token_hash(token)},
    )
    append_audit(
        session,
        action="identity.service_token_issued",
        target_type="account",
        target_id=account_id,
        result="OK",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=uuid.UUID(str(account[1])),
        metadata={"fingerprint": fp},
        clock=clock,
    )
    return token, fp


def revoke_service_token(
    session: Session,
    fingerprint: str,
    *,
    actor_label: str,
    correlation_id: str,
    clock: Clock | None = None,
) -> None:
    now = (clock or SystemClock()).now()
    row = session.execute(
        text(
            "UPDATE service_credentials SET status = 'revoked', revoked_at = :now "
            "WHERE fingerprint = :fp AND status = 'active' RETURNING account_id"
        ),
        {"fp": fingerprint, "now": now},
    ).first()
    if row is None:
        raise IdentityError("CREDENTIAL_NOT_FOUND", fingerprint)
    append_audit(
        session,
        action="identity.service_token_revoked",
        target_type="service_credential",
        target_id=fingerprint,
        result="OK",
        actor_label=actor_label,
        correlation_id=correlation_id,
        actor_account_id=None,
        metadata={"fingerprint": fingerprint},
        clock=clock,
    )


def rotate_service_token(
    session: Session,
    account_id: str,
    old_fingerprint: str,
    *,
    actor_label: str,
    correlation_id: str,
    clock: Clock | None = None,
) -> tuple[str, str]:
    """Issue a new token and revoke the old one atomically (caller's transaction)."""
    token, fp = issue_service_token(
        session, account_id, actor_label=actor_label, correlation_id=correlation_id, clock=clock
    )
    revoke_service_token(
        session,
        old_fingerprint,
        actor_label=actor_label,
        correlation_id=correlation_id,
        clock=clock,
    )
    return token, fp


def resolve_service_token(session: Session, token: str) -> Principal | None:
    row = session.execute(
        text(
            "SELECT a.account_id, a.id, a.account_type, c.fingerprint FROM service_credentials c "
            "JOIN accounts a ON a.id = c.account_id "
            "WHERE c.token_hash = :h AND c.status = 'active' AND a.status = 'ACTIVE'"
        ),
        {"h": token_hash(token)},
    ).first()
    if row is None:
        return None
    return Principal(str(row[0]), str(row[1]), str(row[2]), str(row[3]), "service_token")


# ------------------------------------------------------------------ human sessions
def create_session(
    session: Session,
    account_id: str,
    *,
    ttl_seconds: int = 8 * 3600,
    mfa_verified: bool = False,
    clock: Clock | None = None,
) -> str:
    """Create an opaque session for a Human account; returns the plaintext session token."""
    now = (clock or SystemClock()).now()
    account = session.execute(
        text("SELECT id FROM accounts WHERE account_id = :a AND status = 'ACTIVE'"),
        {"a": account_id},
    ).first()
    if account is None:
        raise IdentityError("ACCOUNT_NOT_FOUND", account_id)
    token = new_token()
    session.execute(
        text(
            "INSERT INTO account_sessions (id, account_id, session_token_hash, fingerprint, "
            "mfa_verified_at, created_at, expires_at) VALUES (:id, :acc, :h, :fp, :mfa, :now, :exp)"
        ),
        {
            "id": uuid.uuid4(),
            "acc": account[0],
            "h": token_hash(token),
            "fp": fingerprint_of(token),
            "mfa": now if mfa_verified else None,
            "now": now,
            "exp": now.replace(microsecond=0)
            + __import__("datetime").timedelta(seconds=ttl_seconds),
        },
    )
    return token


def resolve_session(session: Session, token: str, clock: Clock | None = None) -> Principal | None:
    now = (clock or SystemClock()).now()
    row = session.execute(
        text(
            "SELECT a.account_id, a.id, a.account_type, s.fingerprint, s.mfa_verified_at, "
            "s.reauth_at "
            "FROM account_sessions s JOIN accounts a ON a.id = s.account_id "
            "WHERE s.session_token_hash = :h AND s.revoked_at IS NULL AND s.expires_at > :now "
            "AND a.status = 'ACTIVE'"
        ),
        {"h": token_hash(token), "now": now},
    ).first()
    if row is None:
        return None
    return Principal(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        "session",
        mfa_verified=row[4] is not None,
        reauth_at=row[5].isoformat() if row[5] is not None else None,
    )


def revoke_session(session: Session, token: str, clock: Clock | None = None) -> bool:
    now = (clock or SystemClock()).now()
    row = session.execute(
        text(
            "UPDATE account_sessions SET revoked_at = :now WHERE session_token_hash = :h "
            "AND revoked_at IS NULL RETURNING id"
        ),
        {"h": token_hash(token), "now": now},
    ).first()
    return row is not None


# ------------------------------------------------------------------ spoof guard
def detect_actor_claims(
    body: Mapping[str, Any] | None, headers: Mapping[str, str] | None
) -> list[str]:
    """Names (never values) of identity claims found in a body or custom headers."""
    found: list[str] = []
    for key in body or {}:
        if str(key).lower() in SPOOF_BODY_KEYS:
            found.append(f"body:{key}")
    for name in headers or {}:
        if str(name).lower() in SPOOF_HEADERS:
            found.append(f"header:{name}")
    return found


def assert_no_actor_claims(
    session: Session,
    principal: Principal,
    body: Mapping[str, Any] | None,
    headers: Mapping[str, str] | None,
    *,
    correlation_id: str,
    workspace_id: uuid.UUID | None = None,
    clock: Clock | None = None,
) -> Principal:
    """Audit any actor claim and return the credential principal unchanged (claims are ignored)."""
    claims = detect_actor_claims(body, headers)
    if claims:
        append_audit(
            session,
            action="identity.spoof_attempt",
            target_type="account",
            target_id=principal.account_id,
            result="IGNORED",
            actor_label=principal.account_id,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            actor_account_id=uuid.UUID(principal.account_uuid),
            metadata={"claims": claims, "credential_kind": principal.credential_kind},
            clock=clock,
        )
    return principal
