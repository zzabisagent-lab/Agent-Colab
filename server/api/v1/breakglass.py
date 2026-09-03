"""Break-glass API (spec §4.4; P4-10): ``/api/v1/breakglass``.

- ``POST /activate {recovery_code, totp_code, scope, reason}`` — System Owner only, both proofs
- ``GET /{session_id}`` — session state and recorded actions
- ``POST /{session_id}/terminate`` — ends the session, opens the post-hoc verification Task
- ``POST /sweep`` — administrator-triggered expiry sweep (the gateway also runs it)
Requests made under a session carry ``X-Break-Glass-Session`` and are recorded by the session
policy middleware.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from server.api.deps import correlation_id_of, current_principal
from server.api.errors import ApiError
from server.application.bus import CommandError
from server.db.engine import session_scope
from server.domain.clock import SystemClock
from server.identity.principals import SESSION_COOKIE, Principal
from server.policy.authorization import AuthorizationRequest
from server.security import breakglass, mfa, ratelimit

router = APIRouter(prefix="/api/v1/breakglass", tags=["breakglass"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class ActivateBody(BaseModel):
    recovery_code: str = Field(min_length=8, max_length=16)
    totp_code: str = Field(min_length=6, max_length=8)
    scope: str = Field(min_length=3, max_length=500)
    reason: str = Field(min_length=3, max_length=2000)


def _clock(request: Request) -> Any:
    return (
        getattr(request.app.state, "clock", None)
        or request.app.state.runtime.clock
        or SystemClock()
    )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else "?")
    )


def _view(
    bg: breakglass.BreakGlassSession, actions: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "session_id": bg.session_id,
        "scope": bg.scope,
        "reason": bg.reason,
        "started_at": bg.started_at.isoformat(),
        "expires_at": bg.expires_at.isoformat(),
        "ended_at": None if bg.ended_at is None else bg.ended_at.isoformat(),
        "ended_reason": bg.ended_reason,
        "active": bg.active,
        "posthoc_task_id": bg.posthoc_task_id,
        "actions": actions or [],
    }


def _require_owner(
    request: Request, session: Any, principal: Principal, correlation: str
) -> uuid.UUID:
    runtime = request.app.state.runtime
    if principal.account_type != "human":
        raise ApiError(403, "BREAK_GLASS_HUMAN_ONLY", "only the System Owner may use break-glass")
    engine_auth = getattr(runtime.authorizer, "authorizer", runtime.authorizer)
    auth = engine_auth.authorize(
        session,
        principal.account_id,
        AuthorizationRequest(
            "admin.break_glass",
            "api:breakglass_activate",
            correlation_id=correlation,
            target_type="break_glass",
            target_id="-",
        ),
    )
    if not auth.allowed:
        raise ApiError(404, "NOT_FOUND", "not found")  # normalized (§7.5)
    return uuid.UUID(str(runtime.resolve_workspace(session, principal.account_uuid)))


@router.post("/activate", status_code=201)
def activate(body: ActivateBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    clock = _clock(request)
    correlation = correlation_id_of(request)
    keys = ratelimit.keys_for("breakglass", _client_ip(request), principal.credential_fingerprint)
    with session_scope(runtime.session_factory) as session:
        ratelimit.check(
            session,
            keys,
            clock=clock,
            action="breakglass",
            actor_label=principal.account_id,
            correlation_id=correlation,
        )
        workspace = _require_owner(request, session, principal, correlation)
        now = clock.now()
        try:
            # both proofs: a single-use recovery code and a fresh TOTP (MFA re-authentication)
            mfa.consume_recovery_code(session, principal.account_uuid, body.recovery_code, now)
            mfa.verify_totp(
                session,
                getattr(runtime, "crypto", None),
                principal.account_uuid,
                body.totp_code,
                now,
            )
        except CommandError as exc:
            session.rollback()
            with session_scope(runtime.session_factory) as own:
                ratelimit.record_failure(own, keys, clock=clock)
            raise ApiError(exc.status, exc.code, exc.detail) from exc
        ratelimit.reset(session, keys)
        session_uuid = (
            mfa.session_uuid_for(session, request.cookies.get(SESSION_COOKIE))
            if principal.credential_kind == "session"
            else None
        )
        mfa.record_proof(session, principal.account_uuid, session_uuid, "totp", now)
        try:
            bg = breakglass.activate(
                session,
                runtime.store_for(session),
                workspace_id=workspace,
                account_uuid=principal.account_uuid,
                account_label=principal.account_id,
                scope=body.scope,
                reason=body.reason,
                correlation_id=correlation,
                clock=clock,
            )
        except CommandError as exc:
            raise ApiError(exc.status, exc.code, exc.detail) from exc
        return _view(bg)


@router.get("/{session_id}")
def get_session(session_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        _require_owner(request, session, principal, correlation_id_of(request))
        try:
            bg = breakglass.load(session, session_id)
        except CommandError as exc:
            raise ApiError(exc.status, exc.code, exc.detail) from exc
        rows = session.execute(
            text(
                "SELECT occurred_at, method, path, status_code, "
                "correlation_id FROM breakglass_actions "
                "WHERE session_id = :s ORDER BY id"
            ),
            {"s": session_id},
        ).all()
        actions = [
            {
                "occurred_at": r[0].isoformat(),
                "method": r[1],
                "path": r[2],
                "status": r[3],
                "correlation_id": r[4],
            }
            for r in rows
        ]
        return _view(bg, actions)


@router.post("/{session_id}/terminate")
def terminate(session_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    correlation = correlation_id_of(request)
    with session_scope(runtime.session_factory) as session:
        _require_owner(request, session, principal, correlation)
        try:
            breakglass.terminate(
                session,
                runtime.store_for(session),
                session_id,
                actor_uuid=principal.account_uuid,
                actor_label=principal.account_id,
                correlation_id=correlation,
                clock=_clock(request),
            )
        except CommandError as exc:
            raise ApiError(exc.status, exc.code, exc.detail) from exc
    # the post-hoc verification Task opens in its own transaction (independent audits inside)
    with session_scope(runtime.session_factory) as session:
        breakglass.open_posthoc(
            session,
            runtime.store_for(session),
            session_id,
            correlation_id=correlation,
            clock=_clock(request),
            runtime=runtime,
        )
        return _view(breakglass.load(session, session_id))


@router.post("/sweep")
def sweep(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        _require_owner(request, session, principal, correlation_id_of(request))
        ended = breakglass.expire_sessions(
            session, runtime.store_for(session), clock=_clock(request)
        )
    for sid in ended:
        with session_scope(runtime.session_factory) as session:
            breakglass.open_posthoc(
                session,
                runtime.store_for(session),
                sid,
                correlation_id=f"bg-expire:{sid}",
                clock=_clock(request),
                runtime=runtime,
            )
    return {"ended": ended}
