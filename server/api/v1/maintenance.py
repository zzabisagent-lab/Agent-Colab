"""Maintenance mode REST (P4-13): enter/exit need ``admin.settings`` and a recent MFA re-auth."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.api.deps import correlation_id_of, current_principal
from server.api.errors import ApiError
from server.application.bus import CommandError
from server.identity.principals import Principal
from server.maintenance import mode
from server.security import reauth
from server.settings.store import SettingsStore

router = APIRouter(prefix="/api/v1/maintenance", tags=["maintenance"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class EnterBody(BaseModel):
    reason: str = Field(default="", max_length=500)
    retry_after_s: int | None = Field(default=None, ge=1, le=86400)


def _require_admin(
    request: Request, principal: Principal, session: Any, action: str = "api:maintenance_enter"
) -> None:
    from server.policy.authorization import AuthorizationDenied

    runtime = request.app.state.runtime
    try:
        runtime.authorizer.require(
            session,
            principal.account_id,
            "ops.manage",
            action=action,
            correlation_id=correlation_id_of(request),
        )
    except (AuthorizationDenied, CommandError) as exc:
        raise ApiError(404, "NOT_FOUND", "not found") from exc


def _reauth(request: Request, principal: Principal, action: str) -> None:
    runtime = request.app.state.runtime
    try:
        reauth.require_recent_mfa(
            principal.account_uuid, now=runtime.clock.now(), session_id=None, action=action
        )
    except CommandError as exc:
        raise ApiError(exc.status, exc.code, exc.detail, exc.extra) from exc


@router.get("")
def get_status(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with runtime.session_factory() as session:
        _require_admin(request, principal, session)
        return mode.status(session)


def _context(request: Request, principal: Principal, session: Any) -> tuple[uuid.UUID | None, str]:
    runtime = request.app.state.runtime
    ws = uuid.UUID(runtime.resolve_workspace(session, principal.account_uuid))
    store = SettingsStore(runtime.crypto, runtime.clock)
    return ws, str(store.value(session, "ops.channel_id") or "")


@router.post("/enter")
def enter(body: EnterBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with runtime.session_factory() as session, session.begin():
        _require_admin(request, principal, session)
        _reauth(request, principal, "maintenance_enter")
        ws, ops_channel = _context(request, principal, session)
        retry_after = body.retry_after_s or int(
            SettingsStore(runtime.crypto, runtime.clock).value(
                session, "ops.maintenance_retry_after_s"
            )
        )
        return mode.enter(
            session,
            actor_uuid=uuid.UUID(principal.account_uuid),
            actor_label=principal.account_id,
            reason=body.reason,
            retry_after_s=retry_after,
            workspace_id=ws,
            ops_channel=ops_channel,
            correlation_id=correlation_id_of(request),
            clock=runtime.clock,
        )


@router.post("/exit")
def exit_maintenance(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with runtime.session_factory() as session, session.begin():
        _require_admin(request, principal, session, "api:maintenance_exit")
        _reauth(request, principal, "maintenance_exit")
        ws, ops_channel = _context(request, principal, session)
        return mode.exit_mode(
            session,
            actor_uuid=uuid.UUID(principal.account_uuid),
            actor_label=principal.account_id,
            workspace_id=ws,
            ops_channel=ops_channel,
            correlation_id=correlation_id_of(request),
            clock=runtime.clock,
        )
