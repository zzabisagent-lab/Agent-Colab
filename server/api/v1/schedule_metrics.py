"""Scheduler metrics REST (P5-09): the dashboard reads the same numbers the history holds."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from server.api.deps import current_principal
from server.api.dispatch import Runtime, command_error_to_api, to_bus_principal
from server.application.bus import CommandContext, CommandError, require_permission
from server.db.engine import session_scope
from server.identity.principals import Principal
from server.schedules import metrics

# NOTE: mount this router before the schedule CRUD router so that ``/metrics`` is not captured by
# ``/{schedule_id}``.
router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _ctx(request: Request, principal: Principal, session: Any) -> CommandContext:
    runtime: Runtime = request.app.state.runtime
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
        require_permission(ctx, "schedule.manage", action="api:schedule_metrics_read")
    except CommandError:
        try:  # operators without schedule rights read the same numbers from the ops dashboard
            require_permission(ctx, "admin.settings", action="api:ops_read")
        except CommandError as exc:
            raise command_error_to_api(exc) from exc
    return ctx


@router.get("/metrics")
def all_metrics(
    request: Request,
    principal: PrincipalDep,
    window_s: Annotated[int, Query(ge=60, le=30 * 24 * 3600)] = metrics.DEFAULT_WINDOW_S,
) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ctx = _ctx(request, principal, session)
        snap = metrics.snapshot(session, ctx.workspace_id, runtime.clock.now(), window_s=window_s)
        return {**snap, "alerts": metrics.alerts(snap)}


@router.get("/{schedule_id}/metrics")
def one_metrics(
    schedule_id: str,
    request: Request,
    principal: PrincipalDep,
    window_s: Annotated[int, Query(ge=60, le=30 * 24 * 3600)] = metrics.DEFAULT_WINDOW_S,
) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ctx = _ctx(request, principal, session)
        snap = metrics.schedule_snapshot(
            session, ctx.workspace_id, schedule_id, runtime.clock.now(), window_s=window_s
        )
        return {**snap, "alerts": metrics.alerts(snap)}
