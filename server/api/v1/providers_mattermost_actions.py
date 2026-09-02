"""Interactive action callbacks from Mattermost (development plan §7A.1, P2-12)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from server.api.errors import ApiError
from server.channels.actions import ActionError, ActionHandler, ActionRequest
from server.channels.mattermost import provider as prov
from server.db.engine import session_scope

router = APIRouter(prefix="/api/v1/providers/mattermost", tags=["mattermost"])


class ActionCallback(BaseModel):
    user_id: str
    channel_id: str
    post_id: str = ""
    team_id: str = ""
    trigger_id: str = ""
    type: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


@router.post("/actions")
def mattermost_action(body: ActionCallback, request: Request) -> dict[str, Any]:
    runtime = request.app.state.runtime
    if runtime is None:
        raise ApiError(503, "DATABASE_UNAVAILABLE", "database not configured")
    with session_scope(runtime.session_factory) as session:
        inst = prov.load_instance_by_team(session, body.team_id) if body.team_id else None
        instance_id = (
            inst.provider_instance_id if inst else str(body.context.get("provider_instance_id", ""))
        )
    handler = ActionHandler(runtime, runtime.clock)
    req = ActionRequest(
        provider_instance_id=instance_id,
        user_id=body.user_id,
        channel_id=body.channel_id,
        post_id=body.post_id,
        context=body.context,
        trigger_id=body.trigger_id,
    )
    try:
        return handler.handle(req).as_mattermost()
    except ActionError as exc:
        raise ApiError(exc.status, exc.code, exc.detail) from exc
