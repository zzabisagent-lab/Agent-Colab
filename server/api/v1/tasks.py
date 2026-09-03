"""Task REST endpoints (development plan §7.2 Tasks) on the common command bus."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from server.api.deps import current_principal
from server.api.dispatch import dispatch
from server.api.errors import ApiError
from server.application import tasks as t
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class CreateTaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    channel_id: str
    domain: str = "general"
    risk: str = Field(default="LOW", pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    task_id: str | None = None
    join_policy: dict[str, Any] = Field(default_factory=dict)
    criteria: list[dict[str, Any]] = Field(default_factory=list)


class SubtaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    domain: str = "general"
    risk: str = Field(default="LOW", pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    task_id: str | None = None
    criteria: list[dict[str, Any]] = Field(default_factory=list)


class DelegateBody(BaseModel):
    assignee_account_id: str
    reason_code: str = "DELEGATED"


class ReassignBody(BaseModel):
    assignee_account_id: str
    reason_code: str
    resume_context: dict[str, Any] | None = None


class ProgressBody(BaseModel):
    summary: str = Field(min_length=1, max_length=16_000)


class WaitingBody(BaseModel):
    reason_code: str


class SubmitBody(BaseModel):
    evidence_refs: list[str] = Field(default_factory=list)
    criteria_revision: int = 0


class CompleteBody(BaseModel):
    document_id: str


class CancelBody(BaseModel):
    reason_code: str = "REQUESTED"


def _with_criteria(kwargs: dict[str, Any], criteria: list[dict[str, Any]]) -> dict[str, Any]:
    # P1-11 extends CreateTask/CreateSubtask with `criteria`; pass it only when supported
    if criteria and "criteria" in {f for f in t.CreateTask.__dataclass_fields__}:
        kwargs["criteria"] = tuple(criteria)
    return kwargs


@router.post("", status_code=201)
def create_task(body: CreateTaskBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    kwargs = _with_criteria(body.model_dump(exclude={"criteria"}), body.criteria)
    return dispatch(request, principal, t.CreateTask(**kwargs))


@router.post("/{task_id}/subtasks", status_code=201)
def create_subtask(
    task_id: str, body: SubtaskBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    kwargs = _with_criteria(
        {"parent_task_id": task_id, **body.model_dump(exclude={"criteria"})}, body.criteria
    )
    return dispatch(request, principal, t.CreateSubtask(**kwargs))


@router.post("/{task_id}/delegate")
def delegate(
    task_id: str, body: DelegateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, t.DelegateTask(task_id=task_id, **body.model_dump()))


@router.post("/{task_id}/reassign")
def reassign(
    task_id: str, body: ReassignBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, t.ReassignTask(task_id=task_id, **body.model_dump()))


@router.post("/{task_id}/accept")
def accept(task_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, t.AcceptTask(task_id=task_id))


@router.post("/{task_id}/start")
def start(task_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, t.StartTask(task_id=task_id))


@router.post("/{task_id}/progress")
def progress(
    task_id: str, body: ProgressBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, t.ReportProgress(task_id=task_id, summary=body.summary))


@router.post("/{task_id}/waiting")
def waiting(
    task_id: str, body: WaitingBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, t.MarkWaiting(task_id=task_id, reason_code=body.reason_code)
    )


@router.post("/{task_id}/submit")
def submit(
    task_id: str, body: SubmitBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    cmd = t.SubmitImplementation(
        task_id=task_id,
        evidence_refs=tuple(body.evidence_refs),
        criteria_revision=body.criteria_revision,
    )
    return dispatch(request, principal, cmd)


@router.post("/{task_id}/complete")
def complete(
    task_id: str, body: CompleteBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, t.CompleteTask(task_id=task_id, document_id=body.document_id)
    )


@router.post("/{task_id}/cancel")
def cancel(
    task_id: str, body: CancelBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, t.RequestCancel(task_id=task_id, reason_code=body.reason_code)
    )


@router.get("/{task_id}")
def get_task(task_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        row = (
            session.execute(
                text(
                    "SELECT task_id, root_task_id, parent_task_id, title, domain, risk, status, "
                    "verification_status, delegation_depth, criteria_revision, latest_progress, "
                    "last_event_id, last_aggregate_seq "
                    "FROM tasks_projection WHERE task_id = :t AND workspace_id = :ws"
                ),
                {"t": task_id, "ws": runtime.resolve_workspace(session, principal.account_uuid)},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", "task not found")
        return dict(row)


@router.get("")
def list_tasks(
    request: Request,
    principal: PrincipalDep,
    status: str | None = None,
    after: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    limit = min(max(limit, 1), 100)
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        rows = (
            session.execute(
                text(
                    "SELECT task_id, title, status, risk, domain FROM tasks_projection "
                    "WHERE workspace_id = :ws AND (CAST(:status AS text) IS NULL "
                    "OR status = CAST(:status AS text)) "
                    "AND task_id > :after ORDER BY task_id LIMIT :lim"
                ),
                {
                    "ws": runtime.resolve_workspace(session, principal.account_uuid),
                    "status": status,
                    "after": after,
                    "lim": limit,
                },
            )
            .mappings()
            .all()
        )
    items = [dict(r) for r in rows]
    return {"items": items, "next_after": items[-1]["task_id"] if len(items) == limit else None}
