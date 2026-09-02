"""Identity endpoints (P1-05). Thin handlers over ``server.identity`` services (parent mounts)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from server.api.deps import (
    correlation_id_of,
    current_principal,
    guard_body_claims,
    require_idempotency_key,
)
from server.api.errors import ApiError
from server.events.store import EventStore, InMemoryEventStore
from server.identity.external_links import sql_service
from server.identity.principals import IdentityError, Principal

router = APIRouter(prefix="/api/v1/identity", tags=["identity"])
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]

_STATUS = {
    "EXTERNAL_IDENTITY_DUPLICATE": 409,
    "EXTERNAL_IDENTITY_LOCKED": 429,
    "EXTERNAL_IDENTITY_NOT_FOUND": 404,
    "ACCOUNT_NOT_FOUND": 404,
    "PROVIDER_INSTANCE_UNKNOWN": 404,
    "EXTERNAL_IDENTITY_TRANSITION_INVALID": 409,
}


def _store(request: Request) -> EventStore:
    store = getattr(request.app.state, "event_store", None)
    if store is None:
        store = InMemoryEventStore()
        request.app.state.event_store = store
    return store


def _raise(exc: IdentityError) -> None:
    raise ApiError(_STATUS.get(exc.code, 400), exc.code, exc.detail) from exc


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow"
    )  # extra keys are inspected for spoof claims, then ignored
    provider_instance_id: str
    external_user_id: str


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider_instance_id: str
    external_user_id: str
    code: str
    account_id: str
    path: str = "web"


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    reason_code: str = "ADMIN"


@router.get("/me")
async def me(principal: CurrentPrincipal) -> dict[str, Any]:
    return {
        "account_id": principal.account_id,
        "account_type": principal.account_type,
        "credential_kind": principal.credential_kind,
        "credential_fingerprint": principal.credential_fingerprint,
        "mfa_verified": principal.mfa_verified,
    }


@router.post("/links/challenge", status_code=201)
async def challenge(
    body: ChallengeRequest,
    request: Request,
    principal: CurrentPrincipal,
    _key: IdempotencyKey,
) -> dict[str, Any]:
    factory = request.app.state.session_factory
    with factory() as session:
        try:
            guard_body_claims(session, principal, body.model_dump(), request)
            svc = sql_service(session, _store(request), getattr(request.app.state, "clock", None))
            issued = svc.start_challenge(
                body.provider_instance_id,
                body.external_user_id,
                actor_account_uuid=uuid.UUID(principal.account_uuid),
                correlation_id=correlation_id_of(request),
            )
            session.commit()
        except IdentityError as exc:
            session.rollback()
            _raise(exc)
            raise  # pragma: no cover
    # the code is delivered out of band (DM); returned only to the caller that sends the DM
    return {
        "link_id": issued.link_id,
        "expires_at": issued.expires_at.isoformat(),
        "code": issued.code,
    }


@router.post("/links/confirm", status_code=200)
async def confirm(
    body: ConfirmRequest,
    request: Request,
    principal: CurrentPrincipal,
    _key: IdempotencyKey,
) -> dict[str, Any]:
    factory = request.app.state.session_factory
    with factory() as session:
        try:
            guard_body_claims(session, principal, body.model_dump(), request)
            svc = sql_service(session, _store(request), getattr(request.app.state, "clock", None))
            account_id = body.account_id if body.path == "command" else principal.account_id
            link = svc.confirm_challenge(
                body.provider_instance_id,
                body.external_user_id,
                body.code,
                account_id,
                path=body.path,
                actor_account_uuid=uuid.UUID(principal.account_uuid),
                correlation_id=correlation_id_of(request),
            )
            session.commit()
        except IdentityError as exc:
            session.rollback()
            _raise(exc)
            raise  # pragma: no cover
    return {
        "link_id": link.link_id,
        "status": link.status,
        "account_id": link.account_id,
        "verification_method": link.verification_method,
    }


async def _transition(
    kind: str, link_id: str, body: TransitionRequest, request: Request, principal: Principal
) -> dict[str, Any]:
    factory = request.app.state.session_factory
    with factory() as session:
        try:
            guard_body_claims(session, principal, body.model_dump(), request)
            svc = sql_service(session, _store(request), getattr(request.app.state, "clock", None))
            fn = svc.suspend_link if kind == "suspend" else svc.revoke_link
            link = fn(
                link_id,
                body.reason_code,
                actor_account_uuid=uuid.UUID(principal.account_uuid),
                correlation_id=correlation_id_of(request),
            )
            session.commit()
        except IdentityError as exc:
            session.rollback()
            _raise(exc)
            raise  # pragma: no cover
    return {"link_id": link.link_id, "status": link.status}


@router.post("/links/{link_id}/suspend")
async def suspend(
    link_id: str,
    body: TransitionRequest,
    request: Request,
    principal: CurrentPrincipal,
    _key: IdempotencyKey,
) -> dict[str, Any]:
    return await _transition("suspend", link_id, body, request, principal)


@router.post("/links/{link_id}/revoke")
async def revoke(
    link_id: str,
    body: TransitionRequest,
    request: Request,
    principal: CurrentPrincipal,
    _key: IdempotencyKey,
) -> dict[str, Any]:
    return await _transition("revoke", link_id, body, request, principal)
