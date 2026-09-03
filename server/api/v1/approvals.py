"""Approval REST endpoints (development plan §7.2 Approval) on the common command bus."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from server.api.deps import current_principal
from server.api.dispatch import dispatch
from server.api.errors import ApiError
from server.application import approvals as a
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class RequestBody(BaseModel):
    subject_type: str = Field(pattern="^(task|schedule|run|action)$")
    subject_id: str
    action: str
    risk: str | None = Field(default=None, pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    resource_scope: dict[str, Any] = Field(default_factory=dict)
    valid_for_seconds: int | None = Field(default=None, ge=60)
    max_uses: int | None = Field(default=None, ge=1)
    implementing_agent_account_uuid: str | None = None
    channel_uuid: str | None = None
    requires_human_approval: bool = False


class DecideBody(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT)$")
    reason_code: str = "REJECTED_BY_APPROVER"
    reauth_verified: bool = False  # deprecated: ignored since P4-14 (server-side proof)


class ReasonBody(BaseModel):
    reason_code: str = "CANCELLED_BY_REQUESTER"


@router.post("", status_code=201)
def request_approval(
    body: RequestBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, a.RequestApproval(**body.model_dump()))


@router.post("/{approval_id}/decide")
def decide(
    approval_id: str, body: DecideBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    cmd = a.DecideApproval(
        approval_id=approval_id, decision=body.decision, reason_code=body.reason_code
    )
    # P4-14: re-authentication is proven server-side (recent MFA proof); the body flag is ignored
    from server.api.v1.approvals_queue import reauth_verified

    return dispatch(request, principal, cmd, reauth_verified=reauth_verified(request, principal))


@router.post("/{approval_id}/cancel")
def cancel(
    approval_id: str, body: ReasonBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, a.CancelApproval(approval_id=approval_id, reason_code=body.reason_code)
    )


@router.post("/{approval_id}/revoke")
def revoke(
    approval_id: str, body: ReasonBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, a.RevokeApproval(approval_id=approval_id, reason_code=body.reason_code)
    )


@router.get("/{approval_id}")
def get_approval(approval_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        row = (
            session.execute(
                text(
                    "SELECT approval_id, subject_type, subject_id, action, risk, status, "
                    "valid_from, expires_at, max_uses, quorum_required FROM approval_grants "
                    "WHERE approval_id = :a AND workspace_id = :ws"
                ),
                {
                    "a": approval_id,
                    "ws": runtime.resolve_workspace(session, principal.account_uuid),
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", "approval not found")
        out = dict(row)
        for k in ("valid_from", "expires_at"):
            out[k] = out[k].isoformat()
        consumptions = session.execute(
            text("SELECT count(*) FROM approval_consumptions WHERE approval_id = :a"),
            {"a": approval_id},
        ).scalar_one()
        out["used_count"] = int(consumptions)
        return out
