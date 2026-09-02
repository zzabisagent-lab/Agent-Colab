"""Session endpoints for the web admin console (Phase 2 minimum; Phase 4 adds password/TOTP).

``POST /api/v1/auth/sessions`` exchanges a service token for a session cookie; ``DELETE``
revokes it; ``GET /api/v1/auth/me`` returns the principal. Cookies are HttpOnly, SameSite=Strict
and, outside development, Secure (development plan §11.2).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.errors import ApiError
from server.db.engine import session_scope
from server.domain.clock import SystemClock
from server.identity.principals import (
    SESSION_COOKIE,
    Principal,
    create_session,
    resolve_service_token,
    revoke_session,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
SESSION_TTL_S = 8 * 3600


class SessionBody(BaseModel):
    service_token: str = Field(min_length=16, max_length=512)


@router.post("/sessions", status_code=201)
def create_session_endpoint(
    body: SessionBody, request: Request, response: Response
) -> dict[str, Any]:
    runtime = request.app.state.runtime
    clock = getattr(request.app.state, "clock", None) or SystemClock()
    with session_scope(runtime.session_factory) as session:
        principal = resolve_service_token(session, body.service_token)
        if principal is None or principal.account_type != "human":
            raise ApiError(401, "AUTH_INVALID", "unknown credential or not a human account")
        token = create_session(
            session,
            principal.account_id,
            ttl_seconds=SESSION_TTL_S,
            mfa_verified=False,
            clock=clock,
        )
    secure = not request.app.state.settings.base_url.startswith("http://127.0.0.1")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    return {"account_id": principal.account_id, "expires_in": SESSION_TTL_S}


@router.delete("/sessions", status_code=204)
def delete_session(request: Request, response: Response) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        runtime = request.app.state.runtime
        clock = getattr(request.app.state, "clock", None) or SystemClock()
        with session_scope(runtime.session_factory) as session:
            revoke_session(session, token, clock)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return Response(status_code=204)


@router.get("/me")
def me(principal: Annotated[Principal, Depends(current_principal)]) -> dict[str, Any]:
    return {
        "account_id": principal.account_id,
        "account_type": principal.account_type,
        "credential_kind": principal.credential_kind,
        "mfa_verified": principal.mfa_verified,
        "server_time": dt.datetime.now(dt.UTC).isoformat(),
    }
