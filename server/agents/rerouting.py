"""§7D.3 re-routing (development plan §7B.4; P3-14).

On rejection (``CAPABILITY_UNSUPPORTED|CAPACITY|POLICY|OTHER``), accept timeout (120 s), assignee
offline/revocation/suspension or budget overrun, the next-scored eligible candidate is assigned
exactly once (``TASK_REASSIGNED`` with a new ``task_assignments`` revision and ``resume_context``).
When no candidate exists the Task goes to ``WAITING`` (``TASK_WAITING``; the notification rule
reaches the delegator and the channel). Side effects already started are handed over in
``resume_context`` and never re-executed: the superseded work item is cancelled and the new one
carries the completed steps, Artifacts and last progress.

Re-routing is a server action performed by the Workspace's system service Account.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.agents import routing
from server.application import bus
from server.application import tasks as tasks_app
from server.application import work as work_app
from server.domain import defaults
from server.domain.clock import Clock
from server.domain.task import TaskStatus
from server.events.store import EventStore
from server.work import inbox
from server.work.state import WorkItemError
from server.work.timeouts import SweepReport

log = logging.getLogger(__name__)


REROUTE_REASONS = frozenset(
    {
        "CAPABILITY_UNSUPPORTED",
        "CAPACITY",
        "POLICY",
        "OTHER",
        "ACCEPT_TIMEOUT",
        "AGENT_OFFLINE",
        "AGENT_REVOKED",
        "AGENT_SUSPENDED",
        "BUDGET_EXCEEDED",
    }
)
REROUTABLE = frozenset(
    {TaskStatus.DELEGATED, TaskStatus.ACCEPTED, TaskStatus.RUNNING, TaskStatus.WAITING}
)
REROUTE_PREFIX = "REROUTE_"


@dataclass(frozen=True)
class RerouteOutcome:
    task_id: str
    code: str  # REASSIGNED | WAITING | NOOP
    reason_code: str
    assignee_account_id: str | None = None
    event_id: str | None = None
    work_item_id: str | None = None
    resume_context: dict[str, Any] | None = None


def system_principal(session: Session, workspace_id: str) -> bus.Principal:
    """The Workspace's system service Account (deterministic: lowest service account_id)."""
    row = session.execute(
        text(
            "SELECT id, account_id FROM accounts WHERE workspace_id = :ws "
            "AND account_type = 'service' ORDER BY account_id LIMIT 1"
        ),
        {"ws": uuid.UUID(workspace_id)},
    ).first()
    if row is None:
        raise bus.CommandError("SYSTEM_ACCOUNT_MISSING", "no service Account", status=409)
    return bus.Principal(str(row[1]), str(row[0]), "service", f"system:{row[1]}")


def _ctx(
    session: Session,
    store: EventStore,
    *,
    clock: Clock,
    workspace_id: str,
    actor: bus.Principal,
    authorizer: Any,
    correlation_id: str,
    idempotency_key: str,
) -> bus.CommandContext:
    return bus.CommandContext(
        session=session,
        store=store,
        authorizer=authorizer,
        clock=clock,
        principal=actor,
        workspace_id=workspace_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
    )


def reroute_count(session: Session, task_id: str) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM task_assignments WHERE task_id = :t AND reason_code LIKE :p"
            ),
            {"t": task_id, "p": REROUTE_PREFIX + "%"},
        ).scalar_one()
    )


def previous_assignees(session: Session, task_id: str) -> list[str]:
    rows = session.execute(
        text("SELECT assignee_account_id FROM task_assignments WHERE task_id = :t"),
        {"t": task_id},
    ).all()
    return [str(r[0]) for r in rows]


def build_resume_context(
    session: Session, store: EventStore, workspace_id: str, task_id: str
) -> dict[str, Any]:
    """Completed steps, Artifacts and last progress from the Task's own Events (no secrets)."""
    steps: list[dict[str, Any]] = []
    started = False
    last_progress: str | None = None
    for ev in store.stream(workspace_id, "task", task_id):
        if ev["type"] == "TASK_STARTED":
            started = True
        elif ev["type"] == "TASK_PROGRESS_REPORTED":
            summary = str(ev.get("payload", {}).get("summary", ""))
            steps.append({"event_id": ev["event_id"], "summary": summary})
            last_progress = summary
    rows = session.execute(
        text(
            "SELECT a.artifact_id FROM artifacts a JOIN events e ON e.event_id = a.source_event_id "
            "WHERE e.task_id = :t ORDER BY a.artifact_id"
        ),
        {"t": task_id},
    ).all()
    return {
        "started": started,
        "completed_steps": steps,
        "artifacts": [str(r[0]) for r in rows],
        "last_progress": last_progress,
        "handover": "do not repeat completed steps; continue from last_progress",
    }


def reroute_task(
    session: Session,
    store: EventStore,
    *,
    clock: Clock,
    workspace_id: str,
    task_id: str,
    reason_code: str,
    actor: bus.Principal,
    authorizer: Any,
    correlation_id: str,
    required_capability: str | None = None,
    needs_secret_handles: bool = False,
    exclude_accounts: Iterable[str] = (),
) -> RerouteOutcome:
    """Assign the next-scored eligible candidate once, else WAITING (§7D.3)."""
    if reason_code not in REROUTE_REASONS:
        raise bus.CommandError("REROUTE_REASON_INVALID", reason_code, status=400)
    probe = _ctx(
        session,
        store,
        clock=clock,
        workspace_id=workspace_id,
        actor=actor,
        authorizer=authorizer,
        correlation_id=correlation_id,
        idempotency_key=f"reroute-probe:{task_id}",
    )
    state = tasks_app.load_task(probe, task_id)
    if not state.exists or state.status not in REROUTABLE:
        return RerouteOutcome(task_id, "NOOP", reason_code)
    current = state.assignee_account_id
    exclude = set(previous_assignees(session, task_id)) | set(exclude_accounts)
    if current:
        exclude.add(current)
    count = reroute_count(session, task_id)
    candidate = None
    if count < defaults.TASK_ASSIGNMENT_REROUTES:
        candidate = routing.select_assignee(
            session,
            workspace_id=workspace_id,
            task_id=task_id,
            channel_uuid=_channel_uuid(session, workspace_id, state.channel_id),
            required_capability=required_capability,
            domain=state.domain or None,
            correlation_id=correlation_id,
            actor_label=actor.account_id,
            purpose="reroute",
            needs_secret_handles=needs_secret_handles,
            authorizer=authorizer,
            exclude_accounts=exclude,
            actor_account_uuid=actor.account_uuid,
            clock=clock,
        )
    else:
        routing.record_decision(
            session,
            workspace_id=workspace_id,
            purpose="reroute",
            candidate_set=[],
            selected=None,
            reason_code="REROUTE_LIMIT",
            correlation_id=correlation_id,
            actor_label=actor.account_id,
            task_id=task_id,
            actor_account_uuid=actor.account_uuid,
            clock=clock,
        )
    revision = state.assignment_revision + 1
    if candidate is not None:
        resume = build_resume_context(session, store, workspace_id, task_id)
        resume["previous_assignee_account_id"] = current
        resume["reroute_reason"] = reason_code
        ctx = _ctx(
            session,
            store,
            clock=clock,
            workspace_id=workspace_id,
            actor=actor,
            authorizer=authorizer,
            correlation_id=correlation_id,
            idempotency_key=f"reroute:{task_id}:{revision}",
        )
        res = bus.execute(
            tasks_app.ReassignTask(
                task_id,
                candidate.account_id,
                reason_code=REROUTE_PREFIX + reason_code,
                resume_context=resume,
            ),
            ctx,
        )
        item = _open_assignment_for(session, task_id, candidate.agent_id)
        return RerouteOutcome(
            task_id,
            "REASSIGNED",
            reason_code,
            assignee_account_id=candidate.account_uuid,
            event_id=res.event_id,
            work_item_id=item,
            resume_context=resume,
        )
    # no candidate: WAITING (the notification rule for TASK_WAITING reaches delegator + channel)
    cancel_reason = "REROUTE_" + reason_code
    _cancel_assignments(session, store, task_id, cancel_reason, actor, clock)
    if state.status is TaskStatus.WAITING:
        return RerouteOutcome(task_id, "WAITING", reason_code)
    ctx = _ctx(
        session,
        store,
        clock=clock,
        workspace_id=workspace_id,
        actor=actor,
        authorizer=authorizer,
        correlation_id=correlation_id,
        idempotency_key=f"reroute-wait:{task_id}:{revision}",
    )
    res = bus.execute(tasks_app.MarkWaiting(task_id, f"NO_CANDIDATE_{reason_code}"), ctx)
    return RerouteOutcome(task_id, "WAITING", reason_code, event_id=res.event_id)


def _channel_uuid(session: Session, workspace_id: str, channel_id: str | None) -> str | None:
    if not channel_id:
        return None
    row = session.execute(
        text(
            "SELECT id FROM channels WHERE workspace_id = :ws "
            "AND (channel_id = :c OR id::text = :c)"
        ),
        {"ws": uuid.UUID(workspace_id), "c": channel_id},
    ).first()
    return str(row[0]) if row else None


def _open_assignment_for(session: Session, task_id: str, agent_id: str) -> str | None:
    for item in inbox.open_items(session, agent_id=agent_id):
        if item.task_id == task_id and item.kind in ("task_assignment", "subtask_assignment"):
            return item.work_item_id
    return None


def _cancel_assignments(
    session: Session,
    store: EventStore,
    task_id: str,
    reason_code: str,
    actor: bus.Principal,
    clock: Clock,
) -> list[str]:
    out: list[str] = []
    for item in inbox.open_items(session):
        if item.task_id != task_id or item.kind not in ("task_assignment", "subtask_assignment"):
            continue
        try:
            inbox.cancel(
                session,
                store,
                item.work_item_id,
                reason_code,
                actor_account_id=actor.account_uuid,
                clock=clock,
            )
            out.append(item.work_item_id)
        except WorkItemError:
            continue
    return out


# ---------------------------------------------------------------- triggers


def process_sweep(
    session: Session,
    store: EventStore,
    report: SweepReport,
    *,
    clock: Clock,
    workspace_id: str,
    actor: bus.Principal,
    authorizer: Any,
) -> list[RerouteOutcome]:
    """Apply the inbox sweep's ``REROUTE_REQUIRED``/``WAITING_REQUIRED`` outcomes (120 s)."""
    outcomes: list[RerouteOutcome] = []
    for outcome in report.reroute_required + report.waiting_required:
        try:
            item = inbox.load(session, outcome.work_item_id)
        except WorkItemError:
            continue
        if item.task_id is None:
            continue
        outcomes.append(
            reroute_task(
                session,
                store,
                clock=clock,
                workspace_id=workspace_id,
                task_id=item.task_id,
                reason_code="ACCEPT_TIMEOUT",
                actor=actor,
                authorizer=authorizer,
                correlation_id=item.correlation_id,
            )
        )
    return outcomes


def on_work_rejected(
    session: Session,
    store: EventStore,
    item: inbox.WorkItem,
    reason_code: str,
    *,
    clock: Clock,
    workspace_id: str,
    actor: bus.Principal,
    authorizer: Any,
) -> RerouteOutcome | None:
    if item.kind not in ("task_assignment", "subtask_assignment") or item.task_id is None:
        return None
    try:
        return reroute_task(
            session,
            store,
            clock=clock,
            workspace_id=workspace_id,
            task_id=item.task_id,
            reason_code=reason_code,
            actor=actor,
            authorizer=authorizer,
            correlation_id=item.correlation_id,
        )
    except bus.CommandError as exc:
        if exc.code != "TASK_NOT_FOUND":
            raise
        # an orphan work item (its Task is gone from the projection): the rejection itself is
        # recorded; there is nothing left to re-route
        log.warning(
            "reject of %s: task %s not found, no re-routing", item.work_item_id, item.task_id
        )
        return None


def on_agent_unavailable(
    session: Session,
    store: EventStore,
    agent_id: str,
    reason: str,
    *,
    clock: Clock,
    actor: bus.Principal | None = None,
    authorizer: Any = None,
) -> list[RerouteOutcome]:
    """Registry hook (P3-01): the Agent went offline / was suspended or revoked.

    Every non-terminal Task assigned to the Agent's Account is re-routed once (before or during
    execution); ``reason`` is ``AGENT_OFFLINE|AGENT_SUSPENDED|AGENT_REVOKED``.
    """
    row = session.execute(
        text("SELECT account_id, workspace_id FROM agents WHERE agent_id = :a"), {"a": agent_id}
    ).first()
    if row is None:
        return []
    account_uuid, workspace_id = str(row[0]), str(row[1])
    tasks = session.execute(
        text(
            "SELECT task_id FROM tasks_projection WHERE assignee_account_id = :a "
            "AND status = ANY(:st) ORDER BY task_id"
        ),
        {"a": uuid.UUID(account_uuid), "st": [s.value for s in REROUTABLE]},
    ).all()
    principal = actor or system_principal(session, workspace_id)
    return [
        reroute_task(
            session,
            store,
            clock=clock,
            workspace_id=workspace_id,
            task_id=str(t[0]),
            reason_code=reason,
            actor=principal,
            authorizer=authorizer,
            correlation_id=f"agent-{reason.lower()}:{agent_id}",
        )
        for t in tasks
    ]


def on_budget_exceeded(
    session: Session,
    store: EventStore,
    task_id: str,
    *,
    clock: Clock,
    workspace_id: str,
    actor: bus.Principal | None = None,
    authorizer: Any = None,
    correlation_id: str = "-",
) -> RerouteOutcome:
    return reroute_task(
        session,
        store,
        clock=clock,
        workspace_id=workspace_id,
        task_id=task_id,
        reason_code="BUDGET_EXCEEDED",
        actor=actor or system_principal(session, workspace_id),
        authorizer=authorizer,
        correlation_id=correlation_id,
    )


def _rejection_hook(ctx: bus.CommandContext, item: inbox.WorkItem, reason_code: str) -> None:
    """Bus hook: a rejected assignment is re-routed by the "
    "system Account in the same transaction."""
    if item.kind not in ("task_assignment", "subtask_assignment"):
        return
    try:
        actor = system_principal(ctx.session, ctx.workspace_id)
    except bus.CommandError:
        return  # no system Account: the sweep/administrator handles it later
    on_work_rejected(
        ctx.session,
        ctx.store,
        item,
        reason_code,
        clock=ctx.clock,
        workspace_id=ctx.workspace_id,
        actor=actor,
        authorizer=ctx.authorizer,
    )


if _rejection_hook not in work_app.REJECTION_HOOKS:
    work_app.REJECTION_HOOKS.append(_rejection_hook)
