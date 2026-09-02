"""Telegram Bridge REST endpoints (development plan §7.2 Bridges) on the common command bus."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.dispatch import Runtime, command_error_to_api, dispatch, to_bus_principal
from server.application import bridges as b
from server.application import bus
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/channels", tags=["bridges"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class BridgeBody(BaseModel):
    provider_instance_id: str
    telegram_chat_id: str
    direction: str = Field(
        default="bidirectional",
        pattern="^(mattermost_to_telegram|telegram_to_mattermost|bidirectional)$",
    )
    telegram_thread_id: int | None = None
    thread_mode: str = Field(
        default="topic_per_root", pattern="^(topic_per_root|general|fixed_topic)$"
    )
    content_policy: dict[str, Any] = Field(default_factory=dict)
    redaction_policy: dict[str, Any] = Field(default_factory=dict)
    identity_display: dict[str, Any] = Field(default_factory=dict)
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    allow_commands: bool = False
    admin_exception: bool = False
    admin_exception_reason: str | None = None


class UpdateBody(BaseModel):
    changes: dict[str, Any]


def _read_ctx(request: Request, principal: Principal) -> tuple[Runtime, Any]:
    runtime: Runtime = request.app.state.runtime
    return runtime, principal


def _query(request: Request, principal: Principal, fn: Any, *args: Any) -> Any:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ctx = bus.CommandContext(
            session=session,
            store=runtime.store_for(session),
            authorizer=runtime.authorizer,
            clock=runtime.clock,
            principal=to_bus_principal(principal),
            workspace_id=runtime.resolve_workspace(session, principal.account_uuid),
            correlation_id=request.headers.get("X-Correlation-ID") or "read",
            idempotency_key="read",
        )
        try:
            return fn(ctx, *args)
        except bus.CommandError as exc:
            raise command_error_to_api(exc) from exc


@router.post("/{channel_id}/bridges", status_code=201)
def create(
    channel_id: str, body: BridgeBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, b.CreateBridge(channel_id=channel_id, **body.model_dump()))


@router.get("/{channel_id}/bridges")
def list_for_channel(channel_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return {"items": _query(request, principal, b.list_bridges, channel_id)}


@router.patch("/{channel_id}/bridges/{bridge_id}")
def update(
    channel_id: str, bridge_id: str, body: UpdateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, b.UpdateBridge(bridge_id=bridge_id, changes=body.changes))


@router.post("/{channel_id}/bridges/{bridge_id}/enable")
def enable(
    channel_id: str, bridge_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, b.EnableBridge(bridge_id=bridge_id))


@router.post("/{channel_id}/bridges/{bridge_id}/disable")
def disable(
    channel_id: str, bridge_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, b.DisableBridge(bridge_id=bridge_id))


@router.post("/{channel_id}/bridges/{bridge_id}/test")
def test(
    channel_id: str, bridge_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    client = getattr(request.app.state, "telegram_client", None)
    return dispatch(request, principal, b.TestBridge(bridge_id=bridge_id), telegram_client=client)


@router.get("/{channel_id}/bridges/{bridge_id}/status")
def status(
    channel_id: str, bridge_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dict(_query(request, principal, b.bridge_status, bridge_id))
