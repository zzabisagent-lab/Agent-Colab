"""Administrator surface for external identity links (P2-02): list, approve, suspend, revoke.

Requires ``admin.accounts`` through the Policy Engine; every transition is audited and evented by
``ExternalLinkService``. Mounted by the parent in ``server/main.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from server.api.deps import correlation_id_of, current_principal, require_idempotency_key
from server.api.dispatch import command_error_to_api
from server.api.errors import ApiError
from server.application.bus import CommandError
from server.db.engine import session_scope
from server.identity import external_commands as ext
from server.identity.principals import IdentityError, Principal

router = APIRouter(prefix="/api/v1/identity/admin", tags=["identity-admin"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]
IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]

_STATUS = {
    "EXTERNAL_IDENTITY_NOT_FOUND": 404,
    "EXTERNAL_IDENTITY_TRANSITION_INVALID": 409,
    "EXTERNAL_IDENTITY_LOCKED": 429,
    "EXTERNAL_IDENTITY_DUPLICATE": 409,
}


class TransitionBody(BaseModel):
    reason_code: str = "ADMIN"


def _require_admin(request: Request, session: Any, principal: Principal) -> None:
    runtime = request.app.state.runtime
    if runtime is None or runtime.authorizer is None:
        raise ApiError(503, "POLICY_UNAVAILABLE", "no authorizer configured")
    try:
        runtime.authorizer.require(
            session,
            principal.account_id,
            "admin.accounts",
            action="api:account_update",
            correlation_id=correlation_id_of(request),
        )
    except CommandError as exc:
        raise command_error_to_api(exc) from exc


@router.get("/links")
def list_links(
    request: Request,
    principal: PrincipalDep,
    status: str | None = Query(default=None),
    provider_instance_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        _require_admin(request, session, principal)
        ws = uuid.UUID(runtime.resolve_workspace(session, principal.account_uuid))
        links = ext.list_links(
            session, ws, status=status, provider_instance_id=provider_instance_id, limit=limit
        )
        return {"items": [asdict(link) for link in links]}


def _transition(
    kind: str, link_id: str, body: TransitionBody, request: Request, principal: Principal
) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        _require_admin(request, session, principal)
        try:
            return ext.admin_transition(
                session,
                runtime.store_for(session),
                runtime.clock,
                kind=kind,
                link_id=link_id,
                admin_account_uuid=uuid.UUID(principal.account_uuid),
                correlation_id=correlation_id_of(request),
                reason_code=body.reason_code,
            )
        except IdentityError as exc:
            raise ApiError(_STATUS.get(exc.code, 400), exc.code, exc.detail) from exc


@router.post("/links/{link_id}/approve")
def approve(
    link_id: str, request: Request, principal: PrincipalDep, _key: IdempotencyKey
) -> dict[str, Any]:
    return _transition("approve", link_id, TransitionBody(), request, principal)


@router.post("/links/{link_id}/suspend")
def suspend(
    link_id: str,
    body: TransitionBody,
    request: Request,
    principal: PrincipalDep,
    _key: IdempotencyKey,
) -> dict[str, Any]:
    return _transition("suspend", link_id, body, request, principal)


@router.post("/links/{link_id}/revoke")
def revoke(
    link_id: str,
    body: TransitionBody,
    request: Request,
    principal: PrincipalDep,
    _key: IdempotencyKey,
) -> dict[str, Any]:
    return _transition("revoke", link_id, body, request, principal)
