"""Schedule REST endpoints (P5-01; development plan §7.2, §10A.5) on the common command bus."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.dispatch import Runtime, command_error_to_api, dispatch, to_bus_principal
from server.application import bus
from server.application import schedules as sch
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]

POLICY_PATTERN_CONCURRENCY = "^(FORBID|ALLOW|REPLACE)$"
POLICY_PATTERN_MISSED = "^(SKIP|RUN_ONCE|BACKFILL_LIMITED)$"


class CreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cron_expression: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=64)
    channel_id: str
    execution_principal_id: str
    agent_selection: dict[str, Any]
    action_template: dict[str, Any]
    concurrency_policy: str = Field(default="FORBID", pattern=POLICY_PATTERN_CONCURRENCY)
    missed_run_policy: str = Field(default="RUN_ONCE", pattern=POLICY_PATTERN_MISSED)
    backfill_limit: int = Field(default=0, ge=0, le=1000)
    backfill_window_seconds: int = Field(default=0, ge=0)
    max_duration_seconds: int = Field(default=3600, ge=0, le=86400)
    min_interval_minutes: int = Field(default=5, ge=1)
    retry_policy: dict[str, Any] | None = None
    budget_policy: dict[str, Any] | None = None
    documentation_policy: dict[str, Any] | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    schedule_id: str | None = None


class UpdateBody(BaseModel):
    changes: dict[str, Any]


class RunNowBody(BaseModel):
    client_key: str | None = None


class CancelBody(BaseModel):
    reason_code: str = Field(default="USER_CANCEL", pattern="^[A-Z][A-Z0-9_]{1,63}$")


class PreviewBody(BaseModel):
    cron_expression: str | None = None
    timezone: str | None = None
    schedule_id: str | None = None
    after: str | None = None
    count: int = Field(default=10, ge=1, le=50)


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


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)
    except ValueError as exc:
        from server.api.errors import ApiError

        raise ApiError(400, "TIMESTAMP_INVALID", value) from exc


@router.post("", status_code=201)
def create(body: CreateBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    return dispatch(request, principal, sch.CreateSchedule(**data))


@router.get("")
def list_all(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return {"items": _query(request, principal, sch.list_schedules)}


@router.post("/preview")
def preview(body: PreviewBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dict(
        _query(
            request,
            principal,
            sch.preview,
            schedule_id=body.schedule_id,
            cron_expression=body.cron_expression,
            timezone=body.timezone,
            after=_parse_ts(body.after),
            count=body.count,
        )
    )


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dict(_query(request, principal, sch.run_view, run_id))


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str, body: CancelBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, sch.CancelScheduleRun(run_id=run_id, reason_code=body.reason_code)
    )


@router.post("/runs/{run_id}/retry")
def retry_run(run_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, sch.RetryScheduleRun(run_id=run_id))


@router.get("/{schedule_id}")
def get(schedule_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dict(_query(request, principal, sch.get_schedule, schedule_id))


@router.patch("/{schedule_id}")
def update(
    schedule_id: str, body: UpdateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, sch.CommitScheduleVersion(schedule_id=schedule_id, changes=body.changes)
    )


@router.post("/{schedule_id}/enable")
def enable(schedule_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, sch.EnableSchedule(schedule_id=schedule_id))


@router.post("/{schedule_id}/pause")
def pause(schedule_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, sch.PauseSchedule(schedule_id=schedule_id))


@router.post("/{schedule_id}/resume")
def resume(schedule_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, sch.ResumeSchedule(schedule_id=schedule_id))


@router.post("/{schedule_id}/disable")
def disable(schedule_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, sch.DisableSchedule(schedule_id=schedule_id))


@router.post("/{schedule_id}/run-now")
def run_now(
    schedule_id: str, body: RunNowBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, sch.RunScheduleNow(schedule_id=schedule_id, client_key=body.client_key)
    )


@router.get("/{schedule_id}/runs")
def list_runs(
    schedule_id: str,
    request: Request,
    principal: PrincipalDep,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    return dict(
        _query(
            request,
            principal,
            sch.list_runs,
            schedule_id,
            status=status,
            limit=limit,
            before=_parse_ts(before),
        )
    )


@router.get("/{schedule_id}/history")
def history(
    schedule_id: str,
    request: Request,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    return dict(_query(request, principal, sch.run_history, schedule_id, limit=limit))
