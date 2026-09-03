"""Schedule MCP tools (§7.4; P5-01).

`schedule_create|preview|get|pause|resume|disable|run_now|run_cancel` map onto the same command
handlers as REST. They are **hidden by default**: ``list_tools`` only shows a schedule tool to a
caller whose Roles carry the tool's permission (`schedule.manage` / `schedule.run` /
`schedule.read`), and calling an unadvertised tool answers ``CAPABILITY_UNSUPPORTED``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer

from server.api.dispatch import Runtime, execute_command
from server.api.errors import ApiError
from server.application import bus
from server.application import schedules as sch
from server.db.engine import session_scope
from server.identity.principals import Principal

log = logging.getLogger(__name__)
SCHEMA_ID_BASE = "https://agent-colab.dev/schemas/api/mcp"

# tool name -> permission required to see and call it (development plan §7.4: hidden by default)
HIDDEN_TOOL_PERMISSIONS: dict[str, str] = {
    "schedule_create": "schedule.manage",
    "schedule_pause": "schedule.manage",
    "schedule_resume": "schedule.manage",
    "schedule_disable": "schedule.manage",
    "schedule_run_now": "schedule.run",
    "schedule_run_cancel": "schedule.run",
    "schedule_preview": "schedule.read",
    "schedule_get": "schedule.read",
}
TOOL_ACTIONS: dict[str, str] = {
    "schedule_create": "tool:schedule_create",
    "schedule_pause": "tool:schedule_pause",
    "schedule_resume": "tool:schedule_resume",
    "schedule_disable": "tool:schedule_disable",
    "schedule_run_now": "tool:schedule_run_now",
    "schedule_run_cancel": "tool:schedule_run_cancel",
    "schedule_preview": "tool:schedule_preview",
    "schedule_get": "tool:schedule_get",
}


def _error(code: str, status: int, detail: str) -> dict[str, Any]:
    return {
        "error": {"code": code, "status": status, "detail": detail},
        "schema_id": f"{SCHEMA_ID_BASE}/error.v1",
    }


def allowed(runtime: Runtime, principal: Principal | None, tool: str) -> bool:
    """True when the caller may see/call ``tool`` (deny by default, never raises)."""
    permission = HIDDEN_TOOL_PERMISSIONS.get(tool)
    if permission is None:
        return True
    if principal is None or runtime.authorizer is None:
        return False
    try:
        with session_scope(runtime.session_factory) as session:
            runtime.authorizer.require(
                session,
                principal.account_id,
                permission,
                action=TOOL_ACTIONS[tool],
                correlation_id="mcp:list_tools",
            )
    except Exception:
        return False
    return True


def filter_hidden_tools(
    runtime: Runtime, tools: list[Any], principal_resolver: Callable[[], Principal]
) -> list[Any]:
    """Drop hidden schedule tools the caller has no permission for (§7.4)."""
    try:
        principal: Principal | None = principal_resolver()
    except Exception:
        principal = None
    visible = []
    for tool in tools:
        name = getattr(tool, "name", "")
        if name in HIDDEN_TOOL_PERMISSIONS and not allowed(runtime, principal, name):
            continue
        visible.append(tool)
    return visible


def _run(
    runtime: Runtime, principal: Principal, command: bus.Command, key: str | None
) -> dict[str, Any]:
    try:
        result = execute_command(
            runtime,
            principal,
            command,
            idempotency_key=key or f"mcp-{uuid.uuid4().hex}",
            correlation_id=f"corr-{uuid.uuid4().hex[:16]}",
        )
    except ApiError as exc:
        return _error(exc.code, exc.status, exc.detail)
    return {
        "schema_id": f"{SCHEMA_ID_BASE}/{type(command).__name__}.result.v1",
        "resource_id": result.resource_id,
        "event_id": result.event_id,
        "aggregate_type": result.aggregate_type,
        "aggregate_seq": result.aggregate_seq,
        "replayed": result.replayed,
        **result.data,
    }


def _read(
    runtime: Runtime, principal: Principal, fn: Callable[..., Any], *args: Any, **kw: Any
) -> Any:
    with session_scope(runtime.session_factory) as session:
        ctx = bus.CommandContext(
            session=session,
            store=runtime.store_for(session),
            authorizer=runtime.authorizer,
            clock=runtime.clock,
            principal=bus.Principal(
                principal.account_id,
                principal.account_uuid,
                principal.account_type,
                principal.credential_fingerprint,
            ),
            workspace_id=runtime.resolve_workspace(session, principal.account_uuid),
            correlation_id="mcp:read",
            idempotency_key="read",
        )
        return fn(ctx, *args, **kw)


def register_schedule_tools(
    server: MCPServer, runtime: Runtime, principal_resolver: Callable[[], Principal]
) -> None:
    """Register the §7.4 schedule tools; visibility is filtered per caller by permission."""

    def guard(tool: str) -> Principal:
        principal = principal_resolver()
        if not allowed(runtime, principal, tool):
            raise PermissionError(tool)
        return principal

    @server.tool(name="schedule_create")
    async def schedule_create(
        name: str,
        cron_expression: str,
        timezone: str,
        channel_id: str,
        execution_principal_id: str,
        agent_selection: dict[str, Any],
        action_template: dict[str, Any],
        concurrency_policy: str = "FORBID",
        missed_run_policy: str = "RUN_ONCE",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a Schedule with its first immutable version (permission schedule.manage)."""
        try:
            principal = guard("schedule_create")
        except PermissionError:
            return _error("CAPABILITY_UNSUPPORTED", 403, "schedule_create is not available")
        command = sch.CreateSchedule(
            name=name,
            cron_expression=cron_expression,
            timezone=timezone,
            channel_id=channel_id,
            execution_principal_id=execution_principal_id,
            agent_selection=agent_selection,
            action_template=action_template,
            concurrency_policy=concurrency_policy,
            missed_run_policy=missed_run_policy,
        )
        return await asyncio.to_thread(_run, runtime, principal, command, idempotency_key)

    def _lifecycle_tool(tool: str, factory: Callable[[str], bus.Command]) -> Callable[..., Any]:
        async def run_tool(schedule_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
            try:
                principal = guard(tool)
            except PermissionError:
                return _error("CAPABILITY_UNSUPPORTED", 403, f"{tool} is not available")
            return await asyncio.to_thread(
                _run, runtime, principal, factory(schedule_id), idempotency_key
            )

        run_tool.__name__ = tool
        run_tool.__doc__ = (
            f"{tool} (schedule lifecycle; permission {HIDDEN_TOOL_PERMISSIONS[tool]})"
        )
        return run_tool

    for tool, factory in (
        ("schedule_pause", lambda sid: sch.PauseSchedule(schedule_id=sid)),
        ("schedule_resume", lambda sid: sch.ResumeSchedule(schedule_id=sid)),
        ("schedule_disable", lambda sid: sch.DisableSchedule(schedule_id=sid)),
    ):
        server.tool(name=tool)(_lifecycle_tool(tool, factory))

    @server.tool(name="schedule_run_now")
    async def schedule_run_now(
        schedule_id: str, client_key: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Create a manual Run of a Schedule (permission schedule.run)."""
        try:
            principal = guard("schedule_run_now")
        except PermissionError:
            return _error("CAPABILITY_UNSUPPORTED", 403, "schedule_run_now is not available")
        command = sch.RunScheduleNow(schedule_id=schedule_id, client_key=client_key)
        return await asyncio.to_thread(_run, runtime, principal, command, idempotency_key)

    @server.tool(name="schedule_run_cancel")
    async def schedule_run_cancel(
        run_id: str, reason_code: str = "USER_CANCEL", idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Cancel a pending or running ScheduleRun (permission schedule.run)."""
        try:
            principal = guard("schedule_run_cancel")
        except PermissionError:
            return _error("CAPABILITY_UNSUPPORTED", 403, "schedule_run_cancel is not available")
        command = sch.CancelScheduleRun(run_id=run_id, reason_code=reason_code)
        return await asyncio.to_thread(_run, runtime, principal, command, idempotency_key)

    @server.tool(name="schedule_get")
    async def schedule_get(schedule_id: str) -> dict[str, Any]:
        """Read a Schedule with its versions and recent Runs (permission schedule.read)."""
        try:
            principal = guard("schedule_get")
        except PermissionError:
            return _error("CAPABILITY_UNSUPPORTED", 403, "schedule_get is not available")
        try:
            view = await asyncio.to_thread(_read, runtime, principal, sch.get_schedule, schedule_id)
        except bus.CommandError as exc:
            return _error(exc.code, exc.status, exc.detail)
        return {"schema_id": f"{SCHEMA_ID_BASE}/schedule_get.result.v1", **view}

    @server.tool(name="schedule_preview")
    async def schedule_preview(
        schedule_id: str | None = None,
        cron_expression: str | None = None,
        timezone: str | None = None,
        count: int = 10,
    ) -> dict[str, Any]:
        """Next occurrences in local time and UTC with DST reasons (permission schedule.read)."""
        try:
            principal = guard("schedule_preview")
        except PermissionError:
            return _error("CAPABILITY_UNSUPPORTED", 403, "schedule_preview is not available")
        try:
            view = await asyncio.to_thread(
                _read,
                runtime,
                principal,
                sch.preview,
                schedule_id=schedule_id,
                cron_expression=cron_expression,
                timezone=timezone,
                after=dt.datetime.now(dt.UTC),
                count=count,
            )
        except bus.CommandError as exc:
            return _error(exc.code, exc.status, exc.detail)
        return {"schema_id": f"{SCHEMA_ID_BASE}/schedule_preview.result.v1", **view}
