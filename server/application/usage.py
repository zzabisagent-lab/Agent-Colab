"""Usage commands on the common bus (development plan §7C, §7.5)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.usage.pricing import UsageError
from server.usage.records import SCOPE_TYPES, record_usage, usage_for


@dataclass(frozen=True)
class ReportUsage(Command):
    """Usage reported with a work result, invoke result, or heartbeat."""

    agent_id: str
    work_item_id: str | None
    usage: dict[str, Any] | None = None
    usage_unavailable_reason: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    brainstorm_id: str | None = None
    document_id: str | None = None
    idempotency_scope: str = field(default="usage:report", init=False)


@handles(ReportUsage)
def handle_report_usage(cmd: ReportUsage, ctx: CommandContext) -> CommandResult:
    # An Agent may report its own usage; anyone else needs the work.poll permission.
    if ctx.principal.agent_id != cmd.agent_id:
        require_permission(ctx, "work.poll", action="tool:usage_report")
    try:
        record = record_usage(
            ctx.session,
            workspace_id=ctx.workspace_id,
            account_id=ctx.principal.account_uuid,
            agent_id=cmd.agent_id,
            work_item_id=cmd.work_item_id,
            usage=cmd.usage,
            usage_unavailable_reason=cmd.usage_unavailable_reason,
            task_id=cmd.task_id,
            run_id=cmd.run_id,
            brainstorm_id=cmd.brainstorm_id,
            document_id=cmd.document_id,
            clock=ctx.clock,
        )
    except UsageError as exc:
        raise CommandError(exc.code, exc.detail, status=400) from exc
    return CommandResult(
        resource_id=str(record.record_id),
        event_id="",
        aggregate_seq=0,
        aggregate_type="usage_record",
        data={
            "cost_units": record.cost_units,
            "source": record.source,
            "pricing_version": record.pricing_version,
        },
    )


@dataclass(frozen=True)
class UsageSummary:
    scope_type: str
    scope_id: str
    day: dt.date | None
    cost_units: int


def usage_summary(
    session: Session, scope_type: str, scope_id: str, day: dt.date | None = None
) -> UsageSummary:
    if scope_type not in SCOPE_TYPES:
        raise CommandError("BUDGET_SCOPE_INVALID", scope_type, status=400)
    try:
        total = usage_for(session, scope_type, scope_id, day)
    except UsageError as exc:
        raise CommandError(exc.code, exc.detail, status=400) from exc
    return UsageSummary(scope_type, scope_id, day, total)
