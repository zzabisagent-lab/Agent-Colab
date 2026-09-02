"""Role / Capability administration REST with effective-permission preview (P3-02)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.dispatch import dispatch
from server.api.errors import ApiError
from server.application import roles as rl
from server.application.bus import CommandError
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/roles", tags=["roles"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class RoleBody(BaseModel):
    role_id: str = Field(pattern=r"^role-[a-z0-9][a-z0-9-]{1,62}$")
    display_name: str = Field(min_length=1, max_length=120)
    permissions: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class VersionBody(BaseModel):
    permissions: list[str]
    deny: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class AssignBody(BaseModel):
    account_id: str
    scope: dict[str, Any] = Field(default_factory=dict)
    valid_to: str | None = None


class RevokeBody(BaseModel):
    account_id: str
    reason_code: str = Field(default="ADMIN_REVOKE", pattern="^[A-Z][A-Z0-9_]{1,63}$")


def _ws(request: Request, principal: Principal, session: Any) -> uuid.UUID:
    return uuid.UUID(request.app.state.runtime.resolve_workspace(session, principal.account_uuid))


def _require_admin(request: Request, principal: Principal, session: Any) -> None:
    """Reads are administrative too: normalized 404 on denial (§7.5 information disclosure)."""
    from server.policy.authorization import AuthorizationDenied

    runtime = request.app.state.runtime
    try:
        runtime.authorizer.require(
            session,
            principal.account_id,
            "admin.accounts",
            action="api:role_update",
            correlation_id=request.headers.get("X-Correlation-ID") or "-",
        )
    except AuthorizationDenied as exc:
        raise ApiError(404, "NOT_FOUND", "not found") from exc
    except CommandError as exc:
        raise ApiError(404, "NOT_FOUND", "not found") from exc


@router.post("", status_code=201)
def create(body: RoleBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        rl.CreateRole(
            role_id=body.role_id,
            display_name=body.display_name,
            permissions=tuple(body.permissions),
            deny=tuple(body.deny),
            constraints=body.constraints,
        ),
    )


@router.get("")
def list_roles(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        _require_admin(request, principal, session)
        return {"items": rl.list_roles(session, _ws(request, principal, session))}


@router.get("/effective")
def effective(
    request: Request,
    principal: PrincipalDep,
    account_id: Annotated[str, Query()],
    permission: Annotated[str | None, Query()] = None,
    domain: Annotated[str | None, Query()] = None,
    channel_id: Annotated[str | None, Query()] = None,
    resource: Annotated[str | None, Query()] = None,
    side_effect: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        _require_admin(request, principal, session)
        try:
            return rl.effective_preview(
                session,
                _ws(request, principal, session),
                account_id,
                runtime.clock.now(),
                permission=permission,
                domain=domain,
                channel_id=channel_id,
                resource=resource,
                side_effect=side_effect,
            )
        except CommandError as exc:
            raise ApiError(exc.status, exc.code, exc.detail) from exc


@router.get("/{role_id}")
def get_role(role_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        _require_admin(request, principal, session)
        view = rl.role_view(session, _ws(request, principal, session), role_id)
        if view is None:
            raise ApiError(404, "NOT_FOUND", "role not found")
        return view


@router.post("/{role_id}/versions", status_code=201)
def commit(
    role_id: str, body: VersionBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        rl.CommitRoleVersion(
            role_id=role_id,
            permissions=tuple(body.permissions),
            deny=tuple(body.deny),
            constraints=body.constraints,
        ),
    )


@router.post("/{role_id}/assign")
def assign(
    role_id: str, body: AssignBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        rl.AssignRole(
            account_id=body.account_id, role_id=role_id, scope=body.scope, valid_to=body.valid_to
        ),
    )


@router.post("/{role_id}/revoke")
def revoke(
    role_id: str, body: RevokeBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        rl.RevokeRole(account_id=body.account_id, role_id=role_id, reason_code=body.reason_code),
    )
