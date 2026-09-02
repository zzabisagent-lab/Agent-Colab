"""Channel membership and per-channel document template REST (P2-02) on the command bus.

Mounted by the parent in ``server/main.py``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.dispatch import (
    Runtime,
    command_error_to_api,
    dispatch,
    execute_command,
    to_bus_principal,
)
from server.application import channel_members as cm
from server.application.bus import CommandContext, CommandError
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/channels", tags=["channel-members"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class MemberBody(BaseModel):
    account_id: str
    permissions: list[str] = Field(default_factory=lambda: ["read", "write"])


class PermissionsBody(BaseModel):
    permissions: list[str]


class TemplateBody(BaseModel):
    documentation_template: str | None = None


@router.post("/{channel_id}/members", status_code=201)
def add_member(
    channel_id: str, body: MemberBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    cmd = cm.AddChannelMember(channel_id, body.account_id, tuple(body.permissions))
    return dispatch(request, principal, cmd)


@router.delete("/{channel_id}/members/{account_id}")
def remove_member(
    channel_id: str, account_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, cm.RemoveChannelMember(channel_id, account_id))


@router.put("/{channel_id}/members/{account_id}/permissions")
def set_permissions(
    channel_id: str,
    account_id: str,
    body: PermissionsBody,
    request: Request,
    principal: PrincipalDep,
) -> dict[str, Any]:
    cmd = cm.SetMemberPermissions(channel_id, account_id, tuple(body.permissions))
    return dispatch(request, principal, cmd)


@router.put("/{channel_id}/document-template")
def set_document_template(
    channel_id: str, body: TemplateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    cmd = cm.SetChannelDocumentTemplate(channel_id, body.documentation_template)
    return dispatch(request, principal, cmd)


@router.get("/{channel_id}/members")
def list_members(channel_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ctx = CommandContext(
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
            return {"items": cm.members_of(ctx, channel_id)}
        except CommandError as exc:
            raise command_error_to_api(exc) from exc


__all__ = ["execute_command", "router"]
