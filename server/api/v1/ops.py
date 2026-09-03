"""Operations dashboard REST (P4-02): overview, dependency probes, backups."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from server.api.deps import current_principal
from server.api.dispatch import Runtime, command_error_to_api, to_bus_principal
from server.application.bus import CommandContext, CommandError, require_permission
from server.db.engine import session_scope
from server.identity.principals import Principal
from server.ops import dashboard, probes

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])
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
        require_permission(ctx, "admin.settings", action="api:ops_read")
    except CommandError as exc:
        raise command_error_to_api(exc) from exc
    return ctx


@router.get("/overview")
def overview(
    request: Request, principal: PrincipalDep, refresh: Annotated[int, Query()] = 0
) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ctx = _ctx(request, principal, session)
        return dashboard.overview(
            session, uuid.UUID(ctx.workspace_id), runtime.clock, refresh=bool(refresh)
        )


@router.get("/dependencies")
def dependencies(
    request: Request, principal: PrincipalDep, refresh: Annotated[int, Query()] = 0
) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        _ctx(request, principal, session)
        results = probes.run_probes(session, clock=runtime.clock, refresh=bool(refresh))
        return {"items": [probes.as_dict(r) for r in results], "alerts": probes.alerts(results)}


@router.get("/backups")
def backups(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        _ctx(request, principal, session)
        return {"items": dashboard.list_backups(session)}
