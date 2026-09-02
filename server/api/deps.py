"""FastAPI dependencies: the actor is resolved from the credential only (P1-05, V-P1-08)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from server.api.errors import ApiError
from server.identity.principals import (
    SESSION_COOKIE,
    Principal,
    assert_no_actor_claims,
    detect_actor_claims,
    resolve_service_token,
    resolve_session,
)
from server.observability.audit import append_audit


def correlation_id_of(request: Request) -> str:
    return request.headers.get("X-Correlation-ID") or ("corr-" + uuid.uuid4().hex[:16])


def _factory(request: Request) -> Any:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise ApiError(503, "DATABASE_UNAVAILABLE", "database not configured")
    return factory


def current_principal(request: Request) -> Principal:
    """Resolve the actor from ``Authorization: Bearer`` (service token) or the session cookie.

    Custom actor headers are ignored and audited; they never influence the result.
    """
    auth = request.headers.get("Authorization")
    cookie = request.cookies.get(SESSION_COOKIE)
    if not auth and not cookie:
        raise ApiError(401, "AUTH_REQUIRED", "credential required")
    clock = getattr(request.app.state, "clock", None)
    factory = _factory(request)
    session: Session = factory()
    try:
        principal: Principal | None = None
        if auth:
            if not auth.startswith("Bearer "):
                raise ApiError(401, "AUTH_INVALID", "unsupported authorization scheme")
            principal = resolve_service_token(session, auth.removeprefix("Bearer "))
        elif cookie:
            principal = resolve_session(session, cookie, clock)
        if principal is None:
            raise ApiError(401, "AUTH_INVALID", "unknown, expired or revoked credential")
        claims = detect_actor_claims(None, dict(request.headers))
        if claims:
            append_audit(
                session,
                action="identity.spoof_attempt",
                target_type="account",
                target_id=principal.account_id,
                result="IGNORED",
                actor_label=principal.account_id,
                correlation_id=correlation_id_of(request),
                actor_account_id=uuid.UUID(principal.account_uuid),
                metadata={"claims": claims, "credential_kind": principal.credential_kind},
                clock=clock,
            )
            session.commit()
        return principal
    finally:
        session.close()


def guard_body_claims(
    session: Session, principal: Principal, body: Mapping[str, Any] | None, request: Request
) -> Principal:
    """Audit identity claims inside a request body; returns the credential principal unchanged."""
    return assert_no_actor_claims(
        session,
        principal,
        body,
        None,
        correlation_id=correlation_id_of(request),
        clock=getattr(request.app.state, "clock", None),
    )


def require_idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header required")
    return key
