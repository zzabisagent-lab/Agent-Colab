"""Account administration REST (P4-01) on the command bus; mounted by ``server/main.py``.

Deletion is never a direct DELETE: ``DELETE /api/v1/accounts/{id}`` answers 405
``HARD_DELETE_WORKFLOW_REQUIRED``; ``POST .../deletion-request`` opens the dual-approval workflow.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.dispatch import (
    Runtime,
    command_error_to_api,
    dispatch,
    to_bus_principal,
)
from server.api.errors import ApiError
from server.application import accounts as acc
from server.application import roles
from server.application.bus import CommandContext, CommandError, require_permission
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class CreateBody(BaseModel):
    account_id: str = Field(pattern=r"^acct-[a-z0-9][a-z0-9-]{1,62}$")
    display_name: str = Field(min_length=1, max_length=120)
    account_type: str = Field(default="human", pattern="^(human|service|agent)$")
    auth_subject: str | None = None
    roles: list[str] = Field(default_factory=list)
    issue_token: bool = False


class UpdateBody(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    auth_subject: str | None = None


class ReasonBody(BaseModel):
    reason_code: str = Field(default="ADMIN_ACTION", pattern="^[A-Z][A-Z0-9_]{1,63}$")
    force: bool = False


class DeletionBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class RotateBody(BaseModel):
    old_fingerprint: str


class RevokeBody(BaseModel):
    fingerprint: str


def _read_ctx(request: Request, principal: Principal, session: Any) -> CommandContext:
    runtime: Runtime = request.app.state.runtime
    return CommandContext(
        session=session,
        store=runtime.store_for(session),
        authorizer=runtime.authorizer,
        clock=runtime.clock,
        principal=to_bus_principal(principal),
        workspace_id=runtime.resolve_workspace(session, principal.account_uuid),
        correlation_id=request.headers.get("X-Correlation-ID") or "read",
        idempotency_key="read",
    )


@router.post("", status_code=201)
def create(body: CreateBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    cmd = acc.CreateAccount(
        body.account_id,
        body.display_name,
        body.account_type,
        body.auth_subject,
        tuple(body.roles),
        body.issue_token,
    )
    return dispatch(request, principal, cmd)


@router.get("")
def list_all(
    request: Request,
    principal: PrincipalDep,
    account_type: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ctx = _read_ctx(request, principal, session)
        try:
            require_permission(ctx, "admin.accounts", action="api:account_create")
            items = acc.list_accounts(
                session, uuid.UUID(ctx.workspace_id), account_type=account_type
            )
        except CommandError as exc:
            raise command_error_to_api(exc) from exc
        return {"items": items}


@router.get("/{account_id}")
def get_one(account_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ctx = _read_ctx(request, principal, session)
        try:
            require_permission(ctx, "admin.accounts", action="api:account_create")
        except CommandError as exc:
            raise command_error_to_api(exc) from exc
        view = acc.account_view(session, uuid.UUID(ctx.workspace_id), account_id)
        if view is None:
            raise ApiError(404, "ACCOUNT_NOT_FOUND", account_id)
        return view


@router.get("/{account_id}/roles")
def roles_of(account_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    """Common principal role view: assignments + effective permissions (Phase 3 roles engine)."""
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ctx = _read_ctx(request, principal, session)
        try:
            require_permission(ctx, "admin.accounts", action="api:role_assign")
            preview = roles.effective_preview(
                session, uuid.UUID(ctx.workspace_id), account_id, runtime.clock.now()
            )
        except CommandError as exc:
            raise command_error_to_api(exc) from exc
        return preview


@router.patch("/{account_id}")
def update(
    account_id: str, body: UpdateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, acc.UpdateAccount(account_id, body.display_name, body.auth_subject)
    )


@router.post("/{account_id}/suspend")
def suspend(
    account_id: str, body: ReasonBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, acc.SuspendAccount(account_id, body.reason_code, body.force)
    )


@router.post("/{account_id}/reinstate")
def reinstate(
    account_id: str, body: ReasonBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, acc.ReinstateAccount(account_id, body.reason_code))


@router.post("/{account_id}/deletion-request", status_code=202)
def request_deletion(
    account_id: str, body: DeletionBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, acc.RequestAccountDeletion(account_id, body.reason))


@router.delete("/{account_id}", status_code=405)
def direct_delete(account_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    raise ApiError(
        405,
        "HARD_DELETE_WORKFLOW_REQUIRED",
        "accounts are deleted only through the dual-approval hard-delete workflow "
        f"(POST /api/v1/accounts/{account_id}/deletion-request)",
    )


@router.post("/{account_id}/credentials", status_code=201)
def issue_credential(account_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, acc.IssueCredential(account_id))


@router.post("/{account_id}/credentials/rotate")
def rotate_credential(
    account_id: str, body: RotateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, acc.RotateCredential(account_id, body.old_fingerprint))


@router.post("/{account_id}/credentials/revoke")
def revoke_credential(
    account_id: str, body: RevokeBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, acc.RevokeCredential(account_id, body.fingerprint))
