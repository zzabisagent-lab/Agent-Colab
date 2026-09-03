"""MFA and re-authentication endpoints (P4-09) plus the CSRF token issuer (P4-08).

- ``POST /api/v1/auth/mfa/enroll`` → ``{otpauth_uri, recovery_codes}`` exactly once
- ``POST /api/v1/auth/mfa/confirm {code}`` → enrollment confirmed
- ``POST /api/v1/auth/mfa/verify {code}`` → re-authentication proof for this session/API client
- ``POST /api/v1/auth/mfa/recovery {recovery_code}`` → proof via a single-use recovery code
- ``GET /api/v1/auth/mfa`` → enrollment/requirement status
- ``GET /api/v1/auth/csrf`` → double-submit token (cookie + body)
Failures count toward the per-IP / per-account rate limit (429 after 6 in 15 minutes).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from server.api.deps import correlation_id_of, current_principal
from server.api.errors import ApiError
from server.application.bus import CommandError
from server.config import PRODUCT_NAME
from server.db.engine import session_scope
from server.domain.clock import SystemClock
from server.identity.principals import SESSION_COOKIE, Principal
from server.security import csrf, mfa, ratelimit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class CodeBody(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class RecoveryBody(BaseModel):
    recovery_code: str = Field(min_length=8, max_length=16)


def _clock(request: Request) -> Any:
    return (
        getattr(request.app.state, "clock", None)
        or getattr(request.app.state.runtime, "clock", None)
        or SystemClock()
    )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _guard(request: Request, principal: Principal, action: str) -> list[str]:
    keys = ratelimit.keys_for(action, _client_ip(request), principal.credential_fingerprint)
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ratelimit.check(
            session,
            keys,
            clock=_clock(request),
            action=action,
            actor_label=principal.account_id,
            correlation_id=correlation_id_of(request),
        )
    return keys


def _human(principal: Principal) -> None:
    if principal.account_type != "human":
        raise ApiError(403, "MFA_NOT_APPLICABLE", "MFA applies to Human accounts only")


def _workspace(request: Request, principal: Principal) -> str:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        return str(runtime.resolve_workspace(session, principal.account_uuid))


@router.get("/mfa")
def status(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    now = _clock(request).now()
    with session_scope(runtime.session_factory) as session:
        required = principal.account_type == "human" and mfa.mfa_required(
            session, mfa.principal_info(session, principal.account_uuid), now
        )
        st = mfa.enrollment_status(session, principal.account_uuid, required)
        proof = mfa.latest_proof(
            session,
            principal.account_uuid,
            mfa.session_uuid_for(session, request.cookies.get(SESSION_COOKIE))
            if principal.credential_kind == "session"
            else None,
            now,
        )
    return {
        "enrolled": st.enrolled,
        "confirmed": st.confirmed,
        "required": st.required,
        "verified_at": None if proof is None else proof[0].isoformat(),
        "method": None if proof is None else proof[1],
    }


@router.post("/mfa/enroll", status_code=201)
def enroll(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    _human(principal)
    runtime = request.app.state.runtime
    now = _clock(request).now()
    workspace = _workspace(request, principal)
    with session_scope(runtime.session_factory) as session:
        try:
            uri = mfa.enroll_totp(
                session,
                getattr(runtime, "crypto", None),
                workspace_id=workspace,
                account_uuid=principal.account_uuid,
                account_label=principal.account_id,
                issuer=PRODUCT_NAME,
                now=now,
            )
            codes = mfa.issue_recovery_codes(session, principal.account_uuid, now)
        except CommandError as exc:
            raise ApiError(exc.status, exc.code, exc.detail) from exc
    # shown exactly once; nothing below is persisted in plaintext
    return {"otpauth_uri": uri, "recovery_codes": codes, "method": "totp"}


def _verify_with(
    request: Request, principal: Principal, action: str, verify: Any, method: str
) -> dict[str, Any]:
    _human(principal)
    keys = _guard(request, principal, action)
    runtime = request.app.state.runtime
    clock = _clock(request)
    now = clock.now()
    with session_scope(runtime.session_factory) as session:
        try:
            verify(session, now)
        except CommandError as exc:
            ratelimit.record_failure(session, keys, clock=clock)
            session.commit()
            raise ApiError(exc.status, exc.code, exc.detail) from exc
        ratelimit.reset(session, keys)
        session_uuid = (
            mfa.session_uuid_for(session, request.cookies.get(SESSION_COOKIE))
            if principal.credential_kind == "session"
            else None
        )
        mfa.record_proof(session, principal.account_uuid, session_uuid, method, now)
    return {"verified": True, "method": method, "verified_at": now.isoformat()}


@router.post("/mfa/confirm")
def confirm(body: CodeBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime

    def _confirm(session: Any, now: Any) -> None:
        mfa.confirm_totp(
            session, getattr(runtime, "crypto", None), principal.account_uuid, body.code, now
        )

    return _verify_with(request, principal, "mfa_confirm", _confirm, "totp")


@router.post("/mfa/verify")
def verify(body: CodeBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime

    def _verify(session: Any, now: Any) -> None:
        mfa.verify_totp(
            session, getattr(runtime, "crypto", None), principal.account_uuid, body.code, now
        )

    return _verify_with(request, principal, "mfa_verify", _verify, "totp")


@router.post("/mfa/recovery")
def recovery(body: RecoveryBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    def _consume(session: Any, now: Any) -> None:
        mfa.consume_recovery_code(session, principal.account_uuid, body.recovery_code, now)

    return _verify_with(request, principal, "mfa_recovery", _consume, "recovery_code")


@router.get("/csrf")
def csrf_token(request: Request, response: Response) -> dict[str, str]:
    token = csrf.new_token()
    secure = not request.app.state.settings.base_url.startswith("http://127.0.0.1")
    response.set_cookie(
        csrf.CSRF_COOKIE, token, httponly=False, samesite="strict", secure=secure, path="/"
    )
    return {"csrf_token": token, "header": csrf.CSRF_HEADER}
