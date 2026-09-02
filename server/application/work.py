"""Work delivery commands on the command bus (development plan §7.4 work_poll/work_ack/
work_result, §7B; P1-12). Agents act on their own inbox only; the Agent identity is derived from
the credential's Account (``agents.account_id``), never from the request."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.work import inbox
from server.work.state import WorkItemError


@dataclass(frozen=True)
class WorkPoll(Command):
    """Pull the caller's inbox: QUEUED items are delivered, un-acked items redelivered."""

    agent_id: str
    max_items: int = 10
    idempotency_scope = "work_item:poll"


@dataclass(frozen=True)
class WorkAck(Command):
    work_item_id: str
    idempotency_scope = "work_item:ack"


@dataclass(frozen=True)
class WorkStart(Command):
    work_item_id: str
    idempotency_scope = "work_item:start"


@dataclass(frozen=True)
class WorkReject(Command):
    work_item_id: str
    reason_code: str
    idempotency_scope = "work_item:reject"


@dataclass(frozen=True)
class WorkResult(Command):
    """Submit the work result exactly once (``result`` is the §7B work-result document)."""

    work_item_id: str
    result: dict[str, Any]
    idempotency_scope = "work_item:result"


@dataclass(frozen=True)
class QueueWorkItem(Command):
    """Internal/admin: queue a durable work item for an Agent."""

    kind: str
    agent_id: str
    payload: dict[str, Any]
    deadline: str  # ISO-8601 UTC
    expected_result_schema: str = "colab.work-result.v1"
    task_id: str | None = None
    brainstorm_id: str | None = None
    secret_handles: list[str] = field(default_factory=list)
    idempotency_scope = "work_item:queue"


def caller_agent_id(ctx: CommandContext) -> str:
    """The Agent bound to the caller's Account (credential-derived, §3.1 Identity)."""
    if ctx.principal.agent_id:
        return ctx.principal.agent_id
    row = ctx.session.execute(
        text("SELECT agent_id FROM agents WHERE account_id = :a"),
        {"a": uuid.UUID(ctx.principal.account_uuid)},
    ).first()
    if row is None:
        raise CommandError("AGENT_NOT_FOUND", "caller is not an Agent account", status=404)
    return str(row[0])


def _wrap(exc: WorkItemError) -> CommandError:
    status = 404 if exc.code in ("WORK_ITEM_NOT_FOUND", "WORK_ITEM_NOT_OWNER") else 409
    if exc.code.endswith("SCHEMA_INVALID") or exc.code.endswith("_INVALID"):
        status = 422 if status != 404 else status
    return CommandError(exc.code, exc.detail, status=status)


def _item_data(item: inbox.WorkItem) -> dict[str, Any]:
    return {
        "status": item.status.value,
        "delivery_count": item.delivery_count,
        "kind": item.kind,
        "agent_id": item.agent_id,
        "task_id": item.task_id,
    }


@handles(WorkPoll)
def work_poll(cmd: WorkPoll, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "work.poll")
    agent_id = caller_agent_id(ctx)
    if cmd.agent_id != agent_id:
        raise CommandError("WORK_ITEM_NOT_OWNER", "poll is limited to the caller's inbox", 404)
    try:
        res = inbox.poll(
            ctx.session,
            ctx.store,
            agent_id,
            actor_account_id=ctx.principal.account_uuid,
            clock=ctx.clock,
            max_items=cmd.max_items,
        )
    except WorkItemError as exc:
        raise _wrap(exc) from exc
    return CommandResult(
        resource_id=agent_id,
        event_id=res.delivered_event_ids[-1] if res.delivered_event_ids else "",
        aggregate_seq=0,
        aggregate_type="work_item",
        data={
            "items": [i.to_delivery() for i in res.items],
            "delivered": len(res.delivered_event_ids),
        },
    )


@handles(WorkAck)
def work_ack(cmd: WorkAck, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "work.poll")
    agent_id = caller_agent_id(ctx)
    try:
        item = inbox.ack(
            ctx.session,
            ctx.store,
            cmd.work_item_id,
            agent_id,
            actor_account_id=ctx.principal.account_uuid,
            clock=ctx.clock,
        )
    except WorkItemError as exc:
        raise _wrap(exc) from exc
    return CommandResult(item.work_item_id, "", 0, "work_item", data=_item_data(item))


@handles(WorkStart)
def work_start(cmd: WorkStart, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "work.poll")
    agent_id = caller_agent_id(ctx)
    try:
        item = inbox.start(
            ctx.session,
            ctx.store,
            cmd.work_item_id,
            agent_id,
            actor_account_id=ctx.principal.account_uuid,
            clock=ctx.clock,
        )
    except WorkItemError as exc:
        raise _wrap(exc) from exc
    return CommandResult(item.work_item_id, "", 0, "work_item", data=_item_data(item))


@handles(WorkReject)
def work_reject(cmd: WorkReject, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "work.poll")
    agent_id = caller_agent_id(ctx)
    try:
        item = inbox.reject(
            ctx.session,
            ctx.store,
            cmd.work_item_id,
            agent_id,
            cmd.reason_code,
            actor_account_id=ctx.principal.account_uuid,
            clock=ctx.clock,
        )
    except WorkItemError as exc:
        raise _wrap(exc) from exc
    return CommandResult(item.work_item_id, "", 0, "work_item", data=_item_data(item))


@handles(WorkResult)
def work_result(cmd: WorkResult, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "work.poll")
    agent_id = caller_agent_id(ctx)
    try:
        outcome = inbox.result(
            ctx.session,
            ctx.store,
            cmd.work_item_id,
            agent_id,
            cmd.result,
            actor_account_id=ctx.principal.account_uuid,
            clock=ctx.clock,
        )
    except WorkItemError as exc:
        raise _wrap(exc) from exc
    return CommandResult(
        outcome.work_item_id,
        outcome.event_id or "",
        0,
        "work_item",
        replayed=outcome.code == "DUPLICATE_RESULT_IGNORED",
        data={"code": outcome.code, "result_ref": outcome.result_ref, **_item_data(outcome.item)},
    )


@handles(QueueWorkItem)
def queue_work_item(cmd: QueueWorkItem, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "agent.manage", task_id=cmd.task_id)
    try:
        deadline = dt.datetime.fromisoformat(cmd.deadline.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CommandError("WORK_ITEM_DEADLINE_INVALID", cmd.deadline, status=422) from exc
    try:
        item = inbox.enqueue(
            ctx.session,
            ctx.store,
            workspace_id=ctx.workspace_id,
            kind=cmd.kind,
            agent_id=cmd.agent_id,
            payload=cmd.payload,
            deadline=deadline,
            expected_result_schema=cmd.expected_result_schema,
            correlation_id=ctx.correlation_id,
            idempotency_key=ctx.idempotency_key,
            actor_account_id=ctx.principal.account_uuid,
            clock=ctx.clock,
            task_id=cmd.task_id,
            brainstorm_id=cmd.brainstorm_id,
            secret_handles=list(cmd.secret_handles),
        )
    except WorkItemError as exc:
        raise _wrap(exc) from exc
    return CommandResult(item.work_item_id, "", 0, "work_item", data=_item_data(item))
