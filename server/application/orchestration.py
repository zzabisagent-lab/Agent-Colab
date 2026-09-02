"""Multi-Agent orchestration on the command bus (spec §4.2; development plan §16 P3-09).

- sub-Task graph validation: self/ancestor cycles, cross-Workspace parents, depth, fan-out and
  concurrent sub-Task limits from the channel template (stable errors, zero side effects);
- assignment work items: a delegation/reassignment to an Agent Account enqueues a durable
  ``task_assignment``/``subtask_assignment`` work item (idempotent per assignment revision) that
  carries ``resume_context`` so a new assignee never repeats started side effects;
- join evaluation (``ALL``/``ANY``/``QUORUM``) over the children's verified results, recorded
  once as ``TASK_JOIN_SATISFIED`` on the parent, and the parent completion gate.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.application.bus import CommandContext, CommandError
from server.domain.task import TaskState, TaskStatus, register_completion_check
from server.events.store import AppendRequest, EventStoreError
from server.tasks.graph import (
    ChildView,
    GraphError,
    GraphLimits,
    JoinResult,
    ParentView,
    evaluate_join,
    limits_from_policy,
    validate_new_child,
)
from server.work import inbox
from server.work.state import WorkItemError

ASSIGNMENT_DEADLINE = dt.timedelta(hours=24)


# ---------------------------------------------------------------- channel limits


def channel_limits(session: Session, workspace_id: str, channel_id: str | None) -> GraphLimits:
    """Graph limits of the channel's template (defaults when the channel has no template)."""
    if not channel_id:
        return GraphLimits()
    row = session.execute(
        text(
            "SELECT t.definition FROM channels c JOIN channel_templates t "
            "ON t.workspace_id = c.workspace_id AND t.template_id = c.template_id "
            "WHERE c.workspace_id = :ws AND (c.channel_id = :cid OR c.id::text = :cid) "
            "AND t.status = 'active'"
        ),
        {"ws": uuid.UUID(workspace_id), "cid": channel_id},
    ).first()
    if row is None:
        return GraphLimits()
    definition = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
    return limits_from_policy(definition.get("limits"))


# ---------------------------------------------------------------- graph checks


def ancestors_of(session: Session, task_id: str) -> list[str]:
    out: list[str] = []
    cur: str | None = task_id
    while cur is not None and len(out) < 128:
        row = session.execute(
            text("SELECT parent_task_id FROM task_edges WHERE child_task_id = :c"), {"c": cur}
        ).first()
        cur = str(row[0]) if row else None
        if cur is None or cur in out:
            break
        out.append(cur)
    return out


def children_of(session: Session, parent_task_id: str) -> list[ChildView]:
    rows = session.execute(
        text(
            "SELECT e.child_task_id, p.status, p.verification_status FROM task_edges e "
            "LEFT JOIN tasks_projection p ON p.task_id = e.child_task_id "
            "WHERE e.parent_task_id = :p ORDER BY e.child_task_id"
        ),
        {"p": parent_task_id},
    ).all()
    return [ChildView(str(r[0]), str(r[1] or "OPEN"), r[2]) for r in rows]


def check_subtask_creation(ctx: CommandContext, parent: TaskState, child_task_id: str) -> None:
    """Raise a stable ``CommandError`` (409, zero side effects) when the graph rules forbid it."""
    limits = channel_limits(ctx.session, ctx.workspace_id, parent.channel_id)
    siblings = children_of(ctx.session, parent.task_id)
    active = [c for c in siblings if c.status not in ("COMPLETED", "CANCELLED")]
    try:
        validate_new_child(
            ParentView(
                parent.task_id,
                str(parent.workspace_id or ctx.workspace_id),
                parent.delegation_depth,
                parent.status.value,
            ),
            child_task_id,
            actor_workspace_id=ctx.workspace_id,
            ancestors=[*ancestors_of(ctx.session, parent.task_id), parent.task_id],
            sibling_count=len(siblings),
            active_sibling_count=len(active),
            limits=limits,
        )
    except GraphError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc


# ---------------------------------------------------------------- assignment work items


def agent_for_account(session: Session, account_uuid: str) -> str | None:
    row = session.execute(
        text("SELECT agent_id FROM agents WHERE account_id = :a"), {"a": uuid.UUID(account_uuid)}
    ).first()
    return str(row[0]) if row else None


def cancel_open_assignments(
    session: Session,
    store: Any,
    task_id: str,
    *,
    keep_agent_id: str | None,
    reason_code: str,
    actor_account_id: str,
    clock: Any,
) -> list[str]:
    """Cancel open assignment items of other Agents for the Task (a reassignment supersedes)."""
    cancelled: list[str] = []
    for item in inbox.open_items(session):
        if item.task_id != task_id or item.kind not in ("task_assignment", "subtask_assignment"):
            continue
        if keep_agent_id is not None and item.agent_id == keep_agent_id:
            continue
        try:
            inbox.cancel(
                session,
                store,
                item.work_item_id,
                reason_code,
                actor_account_id=actor_account_id,
                clock=clock,
            )
            cancelled.append(item.work_item_id)
        except WorkItemError:
            continue
    return cancelled


def after_assignment(
    ctx: CommandContext,
    state: TaskState,
    event_id: str,
    *,
    resume_context: dict[str, Any] | None = None,
) -> str | None:
    """Enqueue the assignment work item when the assignee is an Agent (idempotent per revision)."""
    if state.assignee_account_id is None:
        return None
    agent_id = agent_for_account(ctx.session, state.assignee_account_id)
    if agent_id is None:
        return None  # Humans/services are notified through the channel card/DM, not the inbox
    cancel_open_assignments(
        ctx.session,
        ctx.store,
        state.task_id,
        keep_agent_id=agent_id,
        reason_code="REASSIGNED",
        actor_account_id=ctx.principal.account_uuid,
        clock=ctx.clock,
    )
    kind = "subtask_assignment" if state.parent_task_id else "task_assignment"
    payload: dict[str, Any] = {
        "task_id": state.task_id,
        "root_task_id": state.root_task_id,
        "parent_task_id": state.parent_task_id,
        "channel_id": state.channel_id,
        "title": state.title,
        "domain": state.domain,
        "risk": state.risk,
        "assignment_revision": state.assignment_revision,
        "criteria_revision": state.criteria_revision,
        "assignment_event_id": event_id,
        "resume_context": resume_context or {},
    }
    try:
        item = inbox.enqueue(
            ctx.session,
            ctx.store,
            workspace_id=ctx.workspace_id,
            kind=kind,
            agent_id=agent_id,
            payload=payload,
            deadline=ctx.clock.now() + ASSIGNMENT_DEADLINE,
            expected_result_schema="colab.work-result.v1",
            correlation_id=ctx.correlation_id,
            idempotency_key=f"assign:{state.task_id}:{state.assignment_revision}",
            actor_account_id=ctx.principal.account_uuid,
            clock=ctx.clock,
            task_id=state.task_id,
        )
    except WorkItemError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc
    return item.work_item_id


# ---------------------------------------------------------------- join evaluation


def join_state(session: Session, task_id: str) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT join_policy, satisfied, satisfied_children, event_id FROM task_join_state "
                "WHERE task_id = :t"
            ),
            {"t": task_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def evaluate_parent_join(session: Session, parent: TaskState) -> JoinResult:
    return evaluate_join(parent.join_policy, children_of(session, parent.task_id))


def record_join_if_satisfied(
    ctx: CommandContext, parent_task_id: str
) -> tuple[JoinResult, str | None]:
    """Evaluate the parent's join; append ``TASK_JOIN_SATISFIED`` exactly once when it holds."""
    from server.application.tasks import load_task
    from server.projections.tasks import write_state

    parent = load_task(ctx, parent_task_id)
    if not parent.exists:
        return JoinResult("ALL", False, ()), None
    result = evaluate_parent_join(ctx.session, parent)
    if not result.satisfied or parent.join_satisfied:
        return result, None
    try:
        res = ctx.store.append(
            AppendRequest(
                workspace_id=ctx.workspace_id,
                aggregate_type="task",
                aggregate_id=parent.task_id,
                type="TASK_JOIN_SATISFIED",
                actor_account_id=ctx.principal.account_uuid,
                correlation_id=ctx.correlation_id,
                idempotency_scope="task:join",
                idempotency_key=f"join:{parent.task_id}",
                payload={
                    "task_id": parent.task_id,
                    # string form per the pinned schema: ALL | ANY | QUORUM(n)
                    "join_policy": _join_policy_label(result),
                    "satisfied_children": list(result.satisfied_children),
                },
                channel_id=parent.channel_id,
                task_id=parent.task_id,
                caused_by=parent.last_event_id,
                expected_seq=parent.last_aggregate_seq + 1,
            )
        )
    except EventStoreError as exc:
        if exc.code == "IDEMPOTENCY_CONFLICT":
            return result, None
        raise CommandError(exc.code, exc.detail, status=409) from exc
    now = ctx.clock.now()
    ctx.session.execute(
        text(
            "INSERT INTO task_join_state (task_id, workspace_id, join_policy, satisfied, "
            "satisfied_children, event_id, updated_at) VALUES (:t, :ws, CAST(:p AS jsonb), true, "
            "CAST(:c AS jsonb), :e, :now) ON CONFLICT (task_id) DO UPDATE SET satisfied = true, "
            "satisfied_children = EXCLUDED.satisfied_children, event_id = EXCLUDED.event_id, "
            "updated_at = EXCLUDED.updated_at"
        ),
        {
            "t": parent.task_id,
            "ws": uuid.UUID(ctx.workspace_id),
            "p": json.dumps({"mode": result.mode, **parent.join_policy}),
            "c": json.dumps(list(result.satisfied_children)),
            "e": res.event_id,
            "now": now,
        },
    )
    parent = load_task(ctx, parent_task_id)
    write_state(ctx.session, parent, now.isoformat())
    return result, res.event_id


def _join_policy_label(result: JoinResult) -> str:
    return f"QUORUM({result.quorum})" if result.mode == "QUORUM" else result.mode


def on_child_terminal(ctx: CommandContext, child: TaskState) -> None:
    """Called after a child is verified/completed/cancelled: re-evaluate the parent's join."""
    if child.parent_task_id:
        record_join_if_satisfied(ctx, child.parent_task_id)


def parent_join_check(state: TaskState, session: Any) -> str | None:
    """Completion prerequisite: a parent completes only when its join condition is met."""
    if session is None or not hasattr(session, "execute"):
        return None
    children = children_of(session, state.task_id)
    if not children:
        return None
    if state.status not in (TaskStatus.VERIFIED, TaskStatus.COMPLETED):
        return None
    result = evaluate_join(state.join_policy, children)
    return None if result.satisfied else "TASK_JOIN_UNSATISFIED"


register_completion_check(parent_join_check)
