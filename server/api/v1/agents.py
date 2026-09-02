"""Agent Registry REST (P3-01; development plan §7.2 Agents, spec §5.1)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.agents import limits as lim
from server.agents import registry as reg
from server.api.deps import current_principal
from server.api.dispatch import dispatch
from server.api.errors import ApiError
from server.application import agents as ag
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class RegisterBody(BaseModel):
    agent_id: str = Field(pattern=r"^agent-[a-z0-9][a-z0-9-]{1,62}$")
    display_name: str = Field(min_length=1, max_length=120)
    adapter_type: str = Field(pattern="^(mcp|webhook|mattermost_bot)$")
    endpoint: dict[str, Any] = Field(default_factory=dict)
    credential_ref: str | None = None
    owner_account_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    channel_ids: list[str] = Field(default_factory=list)
    limits: dict[str, int] = Field(default_factory=dict)
    runtime_metadata: dict[str, Any] = Field(default_factory=dict)
    delivery_modes: list[str] = Field(default_factory=lambda: ["pull"])


class UpdateBody(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    endpoint: dict[str, Any] | None = None
    credential_ref: str | None = None
    limits: dict[str, int] | None = None
    runtime_metadata: dict[str, Any] | None = None
    delivery_modes: list[str] | None = None
    capabilities: list[str] | None = None


class ActivateBody(BaseModel):
    probe: dict[str, Any] | None = None


class ReasonBody(BaseModel):
    reason_code: str = Field(default="ADMIN_ACTION", pattern="^[A-Z][A-Z0-9_]{1,63}$")
    security_revoke: bool = False


class HeartbeatBody(BaseModel):
    health: str = Field(default="ok", pattern="^(ok|degraded|draining)$")
    capacity: int = Field(default=1, ge=0)
    usage: dict[str, Any] | None = None
    usage_unavailable: str | None = None
    capabilities: list[str] = Field(default_factory=list)


def _ws(request: Request, principal: Principal, session: Any) -> uuid.UUID:
    return uuid.UUID(request.app.state.runtime.resolve_workspace(session, principal.account_uuid))


@router.post("", status_code=201)
def register(body: RegisterBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    data = body.model_dump()
    for key in ("roles", "capabilities", "channel_ids", "delivery_modes"):
        data[key] = tuple(data[key])
    return dispatch(request, principal, ag.RegisterAgent(**data))


@router.get("")
def list_agents(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ws = _ws(request, principal, session)
        return {"items": [reg.public_view(r) for r in reg.list_agents(session, ws)]}


@router.get("/{agent_id}")
def get_agent(agent_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        row = reg.load_agent(session, _ws(request, principal, session), agent_id)
        if row is None:
            raise ApiError(404, "NOT_FOUND", "agent not found")
        view = reg.public_view(row)
        view["limit_counters"] = lim.limits_view(session, row, runtime.clock.now())["current"]
        return view


@router.patch("/{agent_id}")
def update(
    agent_id: str, body: UpdateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    data = body.model_dump()
    for key in ("delivery_modes", "capabilities"):
        if data[key] is not None:
            data[key] = tuple(data[key])
    return dispatch(request, principal, ag.UpdateAgent(agent_id=agent_id, **data))


@router.post("/{agent_id}/test-connection")
def test_connection(agent_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, ag.TestAgentConnection(agent_id=agent_id))


@router.post("/{agent_id}/activate")
def activate(
    agent_id: str, body: ActivateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, ag.ActivateAgent(agent_id=agent_id, probe=body.probe))


@router.post("/{agent_id}/suspend")
def suspend(
    agent_id: str, body: ReasonBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, ag.SuspendAgent(agent_id=agent_id, reason_code=body.reason_code)
    )


@router.post("/{agent_id}/revoke")
def revoke(
    agent_id: str, body: ReasonBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        ag.RevokeAgent(
            agent_id=agent_id, reason_code=body.reason_code, security_revoke=body.security_revoke
        ),
    )


@router.post("/{agent_id}/heartbeat")
def heartbeat(
    agent_id: str, body: HeartbeatBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        ag.RecordHeartbeat(
            agent_id=agent_id,
            health=body.health,
            capacity=body.capacity,
            usage=body.usage,
            usage_unavailable=body.usage_unavailable,
            capabilities=tuple(body.capabilities),
        ),
    )


@router.post("/sweep-offline")
def sweep(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, ag.SweepOffline())


@router.get("/{agent_id}/lifecycle")
def lifecycle(agent_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ws = _ws(request, principal, session)
        row = reg.load_agent(session, ws, agent_id)
        if row is None:
            raise ApiError(404, "NOT_FOUND", "agent not found")
        state = reg.lifecycle_history(runtime.store_for(session), str(ws), agent_id)
        return {
            "agent_id": agent_id,
            "lifecycle_hash": state.lifecycle_hash,
            "status": state.status,
            "online": state.online,
            "history": list(state.history),
        }
