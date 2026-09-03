"""Brainstorm REST endpoints (P6-02/P6-09; development plan §7.2, §7F) on the command bus."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.dispatch import Runtime, command_error_to_api, dispatch, to_bus_principal
from server.application import brainstorm as bs
from server.application import bus
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/brainstorms", tags=["brainstorm"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]
CONTRIBUTION_PATTERN = "^(IDEA|CHALLENGE|QUESTION|GUIDANCE)$"


class StartBody(BaseModel):
    channel_id: str
    topic: str = Field(min_length=1, max_length=500)
    participants: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    brainstorm_id: str | None = None


class JoinBody(BaseModel):
    account_id: str


class ContributeBody(BaseModel):
    body: str = Field(min_length=1, max_length=8000)
    contribution_type: str | None = Field(default=None, pattern=CONTRIBUTION_PATTERN)
    work_item_id: str | None = None


class PauseBody(BaseModel):
    reason_code: str = Field(default="FACILITATOR_PAUSE", pattern="^[A-Z][A-Z0-9_]{1,63}$")


class ResumeBody(BaseModel):
    limits: dict[str, Any] = Field(default_factory=dict)


class SummarizeBody(BaseModel):
    body: str | None = None


class ApproveBody(BaseModel):
    post: bool = True


class DecideBody(BaseModel):
    statement: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=4000)
    source_event_ids: list[str] = Field(default_factory=list)
    action_items: list[dict[str, Any]] = Field(default_factory=list)
    vote: dict[str, Any] | None = None
    decision_id: str | None = None


class TaskifyBody(BaseModel):
    domain: str = Field(default="general", min_length=1, max_length=64)
    risk: str = Field(default="LOW", pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")


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


@router.post("", status_code=201)
def start(body: StartBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        bs.StartBrainstorm(
            channel_id=body.channel_id,
            topic=body.topic,
            participants=tuple(body.participants),
            limits=body.limits,
            brainstorm_id=body.brainstorm_id,
        ),
    )


@router.get("")
def list_all(
    request: Request,
    principal: PrincipalDep,
    status: Annotated[str | None, Query(pattern="^(OPEN|PAUSED|CLOSED)$")] = None,
) -> dict[str, Any]:
    return {"items": _query(request, principal, bs.list_brainstorms, status)}


@router.get("/{brainstorm_id}")
def get_one(brainstorm_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dict(_query(request, principal, bs.brainstorm_view, brainstorm_id))


@router.get("/{brainstorm_id}/transcript")
def transcript(brainstorm_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return {"items": _query(request, principal, bs.transcript_view, brainstorm_id)}


@router.post("/{brainstorm_id}/participants", status_code=201)
def join(
    brainstorm_id: str, body: JoinBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        bs.JoinBrainstorm(brainstorm_id=brainstorm_id, account_id=body.account_id),
    )


@router.post("/{brainstorm_id}/contributions", status_code=201)
def contribute(
    brainstorm_id: str, body: ContributeBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        bs.ContributeTurn(
            brainstorm_id=brainstorm_id,
            body=body.body,
            contribution_type=body.contribution_type,
            work_item_id=body.work_item_id,
        ),
    )


@router.post("/{brainstorm_id}/pause")
def pause(
    brainstorm_id: str, body: PauseBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        bs.PauseBrainstorm(brainstorm_id=brainstorm_id, reason_code=body.reason_code),
    )


@router.post("/{brainstorm_id}/resume")
def resume(
    brainstorm_id: str, body: ResumeBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, bs.ResumeBrainstorm(brainstorm_id=brainstorm_id, limits=body.limits)
    )


@router.post("/{brainstorm_id}/close")
def close(brainstorm_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, bs.CloseBrainstorm(brainstorm_id=brainstorm_id))


@router.post("/{brainstorm_id}/summaries", status_code=201)
def summarize(
    brainstorm_id: str, body: SummarizeBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, bs.SummarizeBrainstorm(brainstorm_id=brainstorm_id, body=body.body)
    )


@router.post("/summaries/{summary_id}/approve")
def approve(
    summary_id: str, body: ApproveBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, bs.ApproveSummary(summary_id=summary_id, post=body.post))


@router.post("/{brainstorm_id}/decisions", status_code=201)
def decide(
    brainstorm_id: str, body: DecideBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        bs.RecordDecision(
            brainstorm_id=brainstorm_id,
            statement=body.statement,
            rationale=body.rationale,
            source_event_ids=tuple(body.source_event_ids),
            action_items=tuple(body.action_items),
            vote=body.vote,
            decision_id=body.decision_id,
        ),
    )


@router.get("/decisions/{decision_id}")
def get_decision(decision_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dict(_query(request, principal, bs.decision_view, decision_id))


@router.post("/decisions/{decision_id}/taskify")
def taskify(
    decision_id: str, body: TaskifyBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request,
        principal,
        bs.TaskifyDecision(decision_id=decision_id, domain=body.domain, risk=body.risk),
    )
