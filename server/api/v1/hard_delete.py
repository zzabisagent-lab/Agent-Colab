"""Hard-delete workflow REST (P4-11): request → two MFA-re-authenticated Human approvals →
waiting period → execute. Mounted by ``server/main.py``."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.dispatch import Runtime, command_error_to_api, dispatch, to_bus_principal
from server.api.errors import ApiError
from server.application import hard_delete as hd
from server.application.bus import CommandContext, CommandError, require_permission
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/hard-delete", tags=["hard-delete"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class RequestBody(BaseModel):
    target_type: str = Field(pattern="^(account|conversation|artifact|document)$")
    target_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=2000)


class DecisionBody(BaseModel):
    decision: str = Field(default="APPROVE", pattern="^(APPROVE|REJECT)$")
    reason_code: str = Field(default="REJECTED_BY_APPROVER", pattern="^[A-Z][A-Z0-9_]{1,63}$")


class CancelBody(BaseModel):
    reason_code: str = Field(default="CANCELLED_BY_ADMIN", pattern="^[A-Z][A-Z0-9_]{1,63}$")


def _session_id(request: Request) -> str | None:
    return request.cookies.get("agent_colab_session") or request.headers.get("X-Session-Id")


@router.post("/requests", status_code=202)
def request_hard_delete(
    body: RequestBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, hd.RequestHardDelete(body.target_type, body.target_id, body.reason)
    )


@router.get("/requests")
def list_requests(request: Request, principal: PrincipalDep) -> dict[str, Any]:
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
            require_permission(ctx, "admin.hard_delete", action="api:hard_delete_request")
        except CommandError as exc:
            raise command_error_to_api(exc) from exc
        return {"items": hd.list_requests(session, uuid.UUID(ctx.workspace_id))}


@router.get("/requests/{request_id}")
def get_request(request_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
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
            require_permission(ctx, "admin.hard_delete", action="api:hard_delete_request")
        except CommandError as exc:
            raise command_error_to_api(exc) from exc
        view = hd.request_view(session, request_id)
        if view is None or str(view["workspace_id"]) != ctx.workspace_id:
            raise ApiError(404, "HARD_DELETE_NOT_FOUND", request_id)
        return view


@router.post("/requests/{request_id}/decide")
def decide(
    request_id: str, body: DecisionBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    """One Human decision; MFA re-authentication is checked server-side (REAUTH_REQUIRED)."""
    return dispatch(
        request,
        principal,
        hd.ApproveHardDelete(request_id, body.decision, body.reason_code),
        session_id=_session_id(request),
    )


@router.post("/requests/{request_id}/cancel")
def cancel(
    request_id: str, body: CancelBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, hd.CancelHardDelete(request_id, body.reason_code))


@router.post("/requests/{request_id}/execute")
def execute(request_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(
        request, principal, hd.ExecuteHardDelete(request_id), session_id=_session_id(request)
    )


@router.delete("/targets/{target_type}/{target_id}", status_code=405)
def direct_delete(
    target_type: str, target_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    raise ApiError(
        405,
        "HARD_DELETE_WORKFLOW_REQUIRED",
        f"{target_type} {target_id}: use POST /api/v1/hard-delete/requests "
        "(dual approval + waiting period)",
    )
