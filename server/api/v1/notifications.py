"""Notification preferences and an operator drain endpoint (P2-17)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from server.api.deps import current_principal
from server.api.dispatch import dispatch
from server.api.errors import ApiError
from server.application import bus
from server.application.notification_prefs import SetNotificationPreferences
from server.db.engine import session_scope
from server.identity.principals import Principal
from server.notifications.outbox import drain
from server.notifications.routing import get_preferences

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class PreferencesBody(BaseModel):
    muted: bool | None = None
    digest: bool | None = None


@router.get("/preferences")
def read_preferences(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        prefs = get_preferences(session, principal.account_uuid)
    return {"account_id": principal.account_id, "muted": prefs.muted, "digest": prefs.digest}


@router.put("/preferences")
def write_preferences(
    body: PreferencesBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, SetNotificationPreferences(**body.model_dump()))


@router.post("/drain")
def drain_once(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    """Run the notification outbox drain once (``ops.manage``); used by operators and tests."""
    runtime = request.app.state.runtime
    provider = getattr(request.app.state, "notification_provider", None)
    if provider is None:
        raise ApiError(503, "NOTIFICATION_PROVIDER_UNCONFIGURED", "no notification provider")
    with session_scope(runtime.session_factory) as session:
        if runtime.authorizer is None:
            raise ApiError(404, "POLICY_DENIED", "no authorizer configured")
        try:
            runtime.authorizer.require(
                session, principal.account_id, "ops.manage", action="api:notification_drain"
            )
        except bus.CommandError as exc:
            raise ApiError(404, exc.code, exc.detail) from exc
        result = drain(
            session,
            provider,
            runtime.store_for(session),
            runtime.clock,
            principal.account_uuid,
            runtime.resolve_workspace(session, principal.account_uuid),
        )
    return {"sent": result.sent, "failed": result.failed, "dead": result.dead}
