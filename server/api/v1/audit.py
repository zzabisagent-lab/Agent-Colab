"""Audit explorer REST (P4-02; V-P4-23): search with cursor pagination and CSV/JSONL export."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from server.api.deps import current_principal
from server.api.dispatch import Runtime, command_error_to_api, to_bus_principal
from server.api.errors import ApiError
from server.application.bus import CommandContext, CommandError, require_permission
from server.db.engine import session_scope
from server.identity.principals import Principal
from server.ops import audit_search as audit

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _parse(value: str | None, name: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "AUDIT_QUERY_INVALID", f"{name}: not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _query(
    request: Request,
    principal: Principal,
    session: Any,
    action_name: str,
    **filters: Any,
) -> audit.AuditQuery:
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
        require_permission(ctx, "admin.audit", action=action_name)
    except CommandError as exc:
        raise command_error_to_api(exc) from exc
    return audit.AuditQuery(workspace_id=uuid.UUID(ctx.workspace_id), **filters)


@router.get("")
def search(
    request: Request,
    principal: PrincipalDep,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    actor: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    target_type: Annotated[str | None, Query()] = None,
    target_id: Annotated[str | None, Query()] = None,
    result: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=audit.MAX_LIMIT)] = 50,
) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        q = _query(
            request,
            principal,
            session,
            "api:audit_search",
            since=_parse(from_, "from"),
            until=_parse(to, "to"),
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
            cursor=cursor,
            limit=limit,
        )
        return audit.search(session, q)


@router.get("/export")
def export(
    request: Request,
    principal: PrincipalDep,
    format: Annotated[str, Query(pattern="^(csv|jsonl)$")] = "jsonl",
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    actor: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    target_type: Annotated[str | None, Query()] = None,
    target_id: Annotated[str | None, Query()] = None,
    result: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        q = _query(
            request,
            principal,
            session,
            "api:audit_export",
            since=_parse(from_, "from"),
            until=_parse(to, "to"),
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            result=result,
        )
        # materialize inside the session: the export is bounded by the redacted rows themselves
        chunks = list(
            audit.export_csv(session, q) if format == "csv" else audit.export_jsonl(session, q)
        )
    media = "text/csv" if format == "csv" else "application/x-ndjson"
    filename = f"audit-export.{format}"
    return StreamingResponse(
        iter(chunks),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
