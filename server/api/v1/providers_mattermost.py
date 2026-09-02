"""Mattermost provider callbacks (development plan §7A.1): slash commands (P2-01/P2-10) and the
interactive-action endpoint placeholder (P2-12). The slash payload is validated before any
normalization: per-command verification token (constant-time hash compare), provider instance by
team, one-time ``trigger_id`` nonce; failures are 401/403 with zero domain side effects.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from server.api.errors import ApiError
from server.channels.mattermost import provider as prov
from server.channels.router import Router, SlashRequest
from server.db.engine import session_scope

router = APIRouter(prefix="/api/v1/providers/mattermost", tags=["providers"])


@router.post("/commands")
def slash_command(  # nosec B107 - empty default, value comes from the environment
    request: Request,
    token: Annotated[str, Form()] = "",
    team_id: Annotated[str, Form()] = "",
    channel_id: Annotated[str, Form()] = "",
    user_id: Annotated[str, Form()] = "",
    user_name: Annotated[str, Form()] = "",
    command: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    trigger_id: Annotated[str, Form()] = "",
    response_url: Annotated[str, Form()] = "",
    post_id: Annotated[str, Form()] = "",
    root_id: Annotated[str, Form()] = "",
) -> JSONResponse:
    runtime = request.app.state.runtime
    if runtime is None:
        raise ApiError(503, "DATABASE_UNAVAILABLE", "database not configured")
    trigger = command.strip().lstrip("/").split(" ", 1)[0] or "colab"
    with session_scope(runtime.session_factory) as session:
        inst = prov.load_instance_by_team(session, team_id)
        if inst is None or inst.status != "active":
            raise ApiError(403, "PROVIDER_INSTANCE_UNKNOWN", "unknown provider instance")
        if not prov.verify_command_token(session, inst.id, trigger, token):
            raise ApiError(401, "CALLBACK_SIGNATURE_INVALID", "command token mismatch")
        clock = getattr(request.app.state, "clock", None)
        if trigger_id and not prov.consume_nonce(session, inst.id, f"slash:{trigger_id}", clock):
            raise ApiError(403, "CALLBACK_NONCE_REUSED", "trigger id already used")
        provider_instance_id = inst.provider_instance_id
    req = SlashRequest(
        provider_instance_id=provider_instance_id,
        team_id=team_id,
        channel_id=channel_id,
        user_id=user_id,
        user_name=user_name,
        command=command,
        text=text,
        trigger_id=trigger_id,
        response_url=response_url,
        post_id=post_id or None,
        root_id=root_id or None,
    )
    response = Router(runtime, clock).route(req)
    body: dict[str, Any] = response.as_mattermost()
    if response.response_type == "in_channel" and response.post_id:
        # the public reply is already in the thread; acknowledge ephemerally (no duplicate post)
        body = {"response_type": "ephemeral", "text": response.text}
    return JSONResponse(body)
