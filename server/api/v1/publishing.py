"""Publish destination, review and publishing endpoints (P6-06/P6-07; development plan §10.3)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.dispatch import Runtime, command_error_to_api, dispatch, to_bus_principal
from server.application import bus
from server.application import publishing as pub
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/publishing", tags=["publishing"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class DestinationBody(BaseModel):
    destination_id: str = Field(min_length=3, max_length=120)
    kind: str = Field(pattern="^(filesystem|git|bookstack|wikijs)$")
    display_name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    credential_ref: str | None = None


class ReviewBody(BaseModel):
    document_id: str
    version: int = Field(ge=1)
    decision: str = Field(pattern="^(APPROVED|REJECTED)$")
    reason: str = Field(min_length=1, max_length=500)


class PublishBody(BaseModel):
    document_id: str
    version: int = Field(ge=1)
    destination_id: str
    correction_of_version: int | None = Field(default=None, ge=1)
    correction_reason: str | None = Field(default=None, max_length=500)


class TargetBody(BaseModel):
    document_id: str
    version: int = Field(ge=1)
    destination_id: str


def _query(request: Request, principal: Principal, fn: Any, *args: Any, **kwargs: Any) -> Any:
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
            return fn(ctx, *args, **kwargs)
        except bus.CommandError as exc:
            raise command_error_to_api(exc) from exc


@router.post("/destinations", status_code=201)
def register_destination(
    body: DestinationBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, pub.RegisterPublishDestination(**body.model_dump()))


@router.get("/destinations")
def list_destinations(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return {"items": _query(request, principal, pub.list_destinations)}


@router.post("/reviews", status_code=201)
def review(body: ReviewBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, pub.ReviewDocumentPublish(**body.model_dump()))


@router.get("/documents/{document_id}/reviews")
def reviews(document_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return {"items": _query(request, principal, pub.reviews_of, document_id)}


@router.post("", status_code=201)
def publish(body: PublishBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, pub.PublishDocument(**body.model_dump()))


@router.post("/verify")
def verify(body: TargetBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, pub.VerifyPublishedDocument(**body.model_dump()))


@router.post("/archive")
def archive(body: TargetBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, pub.ArchivePublishedDocument(**body.model_dump()))


@router.get("/documents/{document_id}")
def published(document_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return {"items": _query(request, principal, pub.published_versions, document_id)}


@router.get("/documents/{document_id}/versions/{version}/attempts")
def attempts(
    document_id: str, version: int, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return {"items": _query(request, principal, pub.publish_attempts, document_id, version)}
