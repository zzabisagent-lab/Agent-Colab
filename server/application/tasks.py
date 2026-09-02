"""Task command handlers on the common command bus (P1-04; spec §8.2, development plan §6.8).

Every handler: policy check → state folded from the Event streams (never the projection) →
transition validation (zero side effects on rejection) → exactly one Event append (idempotency
scope ``task:<verb>``) → synchronous projection update (read-after-write) → append-only
assignment/edge rows where the transition requires them.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    handles,
    require_permission,
)
from server.domain.task import (
    TaskState,
    TaskStatus,
    TaskTransitionError,
    completion_prerequisites,
    fold,
    next_status,
)
from server.events.canonical import canonical_json
from server.events.store import AppendRequest, AppendResult, EventStoreError
from server.projections.tasks import write_state

# ---------------------------------------------------------------- commands


@dataclass(frozen=True)
class CreateTask(Command):
    title: str
    channel_id: str
    domain: str
    risk: str = "LOW"
    task_id: str | None = None
    join_policy: dict[str, Any] = field(default_factory=dict)
    idempotency_scope: str = "task:create"


@dataclass(frozen=True)
class CreateSubtask(Command):
    parent_task_id: str
    title: str
    domain: str
    risk: str = "LOW"
    task_id: str | None = None
    idempotency_scope: str = "task:create_subtask"


@dataclass(frozen=True)
class DelegateTask(Command):
    task_id: str
    assignee_account_id: str  # public account_id
    reason_code: str = "DELEGATED"
    idempotency_scope: str = "task:delegate"


@dataclass(frozen=True)
class ReassignTask(Command):
    task_id: str
    assignee_account_id: str
    reason_code: str
    resume_context: dict[str, Any] | None = None
    idempotency_scope: str = "task:reassign"


@dataclass(frozen=True)
class AcceptTask(Command):
    task_id: str
    idempotency_scope: str = "task:accept"


@dataclass(frozen=True)
class StartTask(Command):
    task_id: str
    idempotency_scope: str = "task:start"


@dataclass(frozen=True)
class ReportProgress(Command):
    task_id: str
    summary: str
    idempotency_scope: str = "task:progress"


@dataclass(frozen=True)
class MarkWaiting(Command):
    task_id: str
    reason_code: str
    idempotency_scope: str = "task:wait"


@dataclass(frozen=True)
class SubmitImplementation(Command):
    task_id: str
    evidence_refs: tuple[str, ...]
    criteria_revision: int = 0
    idempotency_scope: str = "task:submit"


@dataclass(frozen=True)
class StartVerification(Command):
    task_id: str
    verification_id: str
    idempotency_scope: str = "task:verify"


@dataclass(frozen=True)
class RecordVerificationResult(Command):
    """Appends the result on the ``verification_run`` aggregate (task_id on the envelope)."""

    task_id: str
    verification_id: str
    result: str  # PASSED | FAILED | BLOCKED
    revision: int = 1
    evidence_refs: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    reason_code: str = "EXTERNAL_CONDITION"
    idempotency_scope: str = "verification_run:result"


@dataclass(frozen=True)
class CompleteTask(Command):
    task_id: str
    document_id: str
    idempotency_scope: str = "task:complete"


@dataclass(frozen=True)
class RequestCancel(Command):
    task_id: str
    reason_code: str = "REQUESTED"
    idempotency_scope: str = "task:cancel_request"


@dataclass(frozen=True)
class CancelTask(Command):
    task_id: str
    reason_code: str = "CANCELLED"
    idempotency_scope: str = "task:cancel"


# ---------------------------------------------------------------- hooks for later packages
PRE_SUBMIT_CHECKS: list[Any] = []
"""Callables ``(ctx, state, command) -> str | None`` (error code). P1-11 adds the criteria gate."""


def pre_submit_checks(ctx: CommandContext, state: TaskState, cmd: SubmitImplementation) -> None:
    for check in PRE_SUBMIT_CHECKS:
        code = check(ctx, state, cmd)
        if code:
            raise CommandError(
                code, f"submission rejected by {getattr(check, '__name__', 'check')}"
            )


# ---------------------------------------------------------------- helpers


def _new_task_id() -> str:
    return "task-" + uuid.uuid4().hex[:16]


def load_task(ctx: CommandContext, task_id: str) -> TaskState:
    """Fold the Task from its Event stream plus the results of the verifications started on it."""
    events = ctx.store.stream(ctx.workspace_id, "task", task_id)
    if not events:
        raise CommandError("TASK_NOT_FOUND", task_id, status=404)
    verification_ids = [
        e["payload"].get("verification_id")
        for e in events
        if e["type"] == "TASK_VERIFICATION_STARTED" and e.get("payload")
    ]
    merged = list(events)
    for vid in verification_ids:
        if vid:
            merged.extend(
                e
                for e in ctx.store.stream(ctx.workspace_id, "verification_run", vid)
                if e.get("task_id") == task_id
            )
    return fold(task_id, merged)


def _replay_from_stream(ctx: CommandContext, task_id: str, scope: str) -> AppendResult | None:
    """Return the Event this exact command (actor, scope, key) already produced, if any."""
    for ev in ctx.store.stream(ctx.workspace_id, "task", task_id):
        if (
            ev.get("idempotency_scope") == scope
            and ev.get("idempotency_key") == ctx.idempotency_key
            and ev.get("actor_account_id") == ctx.principal.account_uuid
        ):
            return AppendResult(
                ev["event_id"],
                ev["aggregate_seq"],
                ev["content_hash"],
                int(ev.get("recorded_seq", 0)),
                True,
            )
    return None


def _occurred_at(ctx: CommandContext, event_id: str) -> Any:
    """Timestamp of the appended Event (the projection must use it, never the wall clock)."""
    getter = getattr(ctx.store, "get", None)
    event = getter(event_id) if getter else None
    return event["occurred_at"] if event else ctx.clock.now()


def _append(ctx: CommandContext, req: AppendRequest) -> AppendResult:
    try:
        return ctx.store.append(req)
    except EventStoreError as exc:
        status = 409 if exc.code in ("IDEMPOTENCY_CONFLICT", "SEQUENCE_CONFLICT") else 422
        raise CommandError(exc.code, exc.detail, status=status) from exc


def _transition(
    ctx: CommandContext,
    cmd: Command,
    state: TaskState,
    event_type: str,
    payload: dict[str, Any],
    *,
    caused_by: str | None = None,
) -> tuple[AppendResult, TaskState]:
    replay = _replay_from_stream(ctx, state.task_id, cmd.idempotency_scope)
    if replay is not None:
        return replay, state
    try:
        new_status = next_status(state.status, event_type)
    except TaskTransitionError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc
    res = _append(
        ctx,
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type="task",
            aggregate_id=state.task_id,
            type=event_type,
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            payload={"task_id": state.task_id, **payload},
            channel_id=state.channel_id,
            task_id=state.task_id,
            caused_by=caused_by or state.last_event_id,
            expected_seq=state.last_aggregate_seq + 1,
        ),
    )
    if res.replayed:
        return res, state
    getter = getattr(ctx.store, "get", None)
    event = getter(res.event_id) if getter else None
    if event is None:
        event = {
            "event_id": res.event_id,
            "type": event_type,
            "aggregate_type": "task",
            "aggregate_id": state.task_id,
            "aggregate_seq": res.aggregate_seq,
            "recorded_seq": res.recorded_seq,
            "task_id": state.task_id,
            "actor_account_id": ctx.principal.account_uuid,
            "payload": {"task_id": state.task_id, **payload},
            "occurred_at": ctx.clock.now().isoformat(),
        }
    from server.domain.task import apply_event

    apply_event(state, event)
    state.status = new_status
    write_state(ctx.session, state, event["occurred_at"])
    return res, state


def _result(res: AppendResult, task_id: str, state: TaskState, **data: Any) -> Any:
    from server.application.bus import CommandResult

    return CommandResult(
        resource_id=task_id,
        event_id=res.event_id,
        aggregate_seq=res.aggregate_seq,
        aggregate_type="task",
        replayed=res.replayed,
        data={"status": state.status.value, **data},
    )


def _policy_snapshot_hash(ctx: CommandContext) -> str:
    snapshot = ctx.extras.get("policy_snapshot", {})
    return hashlib.sha256(canonical_json(snapshot)).hexdigest()


def _account_uuid(ctx: CommandContext, public_id: str) -> str:
    row = ctx.session.execute(
        text("SELECT id FROM accounts WHERE account_id = :a AND workspace_id = :w"),
        {"a": public_id, "w": uuid.UUID(ctx.workspace_id)},
    ).first()
    if row is None:
        raise CommandError("ACCOUNT_NOT_FOUND", public_id, status=404)
    return str(row[0])


def _record_assignment(
    ctx: CommandContext, state: TaskState, cmd: DelegateTask | ReassignTask, event_id: str
) -> None:
    import json

    ctx.session.execute(
        text(
            "INSERT INTO task_assignments (task_id, revision, delegator_account_id, "
            "assignee_account_id, reason_code, policy_snapshot_hash, resume_context, event_id) "
            "VALUES (:t, :r, :d, :a, :reason, :h, CAST(:rc AS jsonb), :e)"
        ),
        {
            "t": state.task_id,
            "r": state.assignment_revision,
            "d": uuid.UUID(ctx.principal.account_uuid),
            "a": uuid.UUID(str(state.assignee_account_id)),
            "reason": cmd.reason_code,
            "h": state.policy_snapshot_hash or "",
            "rc": json.dumps(getattr(cmd, "resume_context", None)),
            "e": event_id,
        },
    )


# ---------------------------------------------------------------- handlers


def _create(
    ctx: CommandContext,
    cmd: CreateTask | CreateSubtask,
    task_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> Any:
    res = _append(
        ctx,
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type="task",
            aggregate_id=task_id,
            type=event_type,
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            payload=payload,
            channel_id=payload["channel_id"],
            task_id=task_id,
            expected_seq=1,
        ),
    )
    state = load_task(ctx, task_id)
    if not res.replayed:
        write_state(ctx.session, state, _occurred_at(ctx, res.event_id))
    return res, state


@handles(CreateTask)
def create_task(cmd: CreateTask, ctx: CommandContext) -> Any:
    require_permission(ctx, "task.create", channel_id=cmd.channel_id, domain=cmd.domain)
    if cmd.risk not in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
        raise CommandError("TASK_RISK_INVALID", cmd.risk, status=400)
    if not cmd.title.strip():
        raise CommandError("TASK_TITLE_REQUIRED", "title is empty", status=400)
    task_id = cmd.task_id or _new_task_id()
    payload = {
        "task_id": task_id,
        "root_task_id": task_id,
        "channel_id": cmd.channel_id,
        "title": cmd.title,
        "domain": cmd.domain,
        "risk": cmd.risk,
        "join_policy": cmd.join_policy,
        "policy_snapshot_hash": _policy_snapshot_hash(ctx),
    }
    res, state = _create(ctx, cmd, task_id, "TASK_CREATED", payload)
    return _result(res, task_id, state)


@handles(CreateSubtask)
def create_subtask(cmd: CreateSubtask, ctx: CommandContext) -> Any:
    parent = load_task(ctx, cmd.parent_task_id)
    require_permission(ctx, "task.delegate", channel_id=parent.channel_id, domain=cmd.domain)
    if parent.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
        raise CommandError("TASK_TERMINAL", f"parent {cmd.parent_task_id} is terminal", status=409)
    task_id = cmd.task_id or _new_task_id()
    if task_id == cmd.parent_task_id:
        raise CommandError("TASK_GRAPH_CYCLE", "a Task cannot be its own parent", status=409)
    root_task_id = parent.root_task_id or cmd.parent_task_id
    depth = parent.delegation_depth + 1
    payload = {
        "task_id": task_id,
        "parent_task_id": cmd.parent_task_id,
        "root_task_id": root_task_id,
        "depth": depth,
        "channel_id": parent.channel_id,
        "title": cmd.title,
        "domain": cmd.domain,
        "risk": cmd.risk,
        "policy_snapshot_hash": _policy_snapshot_hash(ctx),
    }
    res, state = _create(ctx, cmd, task_id, "SUBTASK_CREATED", payload)
    if not res.replayed:
        link_subtask_edge(
            ctx,
            task_id,
            cmd.parent_task_id,
            root_task_id,
            depth,
            res.event_id,
        )
    return _result(res, task_id, state, parent_task_id=cmd.parent_task_id)


def link_subtask_edge(
    ctx: CommandContext, child: str, parent: str, root: str, depth: int, event_id: str
) -> None:
    """Append-only task_edges row; the DB trigger rejects self/ancestor cycles."""
    try:
        with ctx.session.begin_nested():
            ctx.session.execute(
                text(
                    "INSERT INTO task_edges (child_task_id, parent_task_id, root_task_id, "
                    "workspace_id, depth, created_event_id) VALUES (:c, :p, :r, :w, :d, :e)"
                ),
                {
                    "c": child,
                    "p": parent,
                    "r": root,
                    "w": uuid.UUID(ctx.workspace_id),
                    "d": depth,
                    "e": event_id,
                },
            )
    except DBAPIError as exc:
        message = str(exc.orig)
        cyclic = "TASK_GRAPH_CYCLE" in message or "TASK_GRAPH_TOO_DEEP" in message
        code = "TASK_GRAPH_CYCLE" if cyclic else "TASK_EDGE_INVALID"
        raise CommandError(code, message.splitlines()[0], status=409) from exc


@handles(DelegateTask)
def delegate_task(cmd: DelegateTask, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.delegate", channel_id=state.channel_id, domain=state.domain)
    assignee = _account_uuid(ctx, cmd.assignee_account_id)
    payload = {
        "assignee_account_id": assignee,
        "assignment_revision": state.assignment_revision + 1,
        "policy_snapshot_hash": _policy_snapshot_hash(ctx),
        "reason_code": cmd.reason_code,
    }
    res, state = _transition(ctx, cmd, state, "TASK_DELEGATED", payload)
    if not res.replayed:
        _record_assignment(ctx, state, cmd, res.event_id)
    return _result(res, cmd.task_id, state, assignee_account_id=assignee)


@handles(ReassignTask)
def reassign_task(cmd: ReassignTask, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.reassign", channel_id=state.channel_id, domain=state.domain)
    assignee = _account_uuid(ctx, cmd.assignee_account_id)
    payload = {
        "assignee_account_id": assignee,
        "assignment_revision": state.assignment_revision + 1,
        "reason_code": cmd.reason_code,
        "policy_snapshot_hash": state.policy_snapshot_hash or _policy_snapshot_hash(ctx),
    }
    res, state = _transition(ctx, cmd, state, "TASK_REASSIGNED", payload)
    if not res.replayed:
        _record_assignment(ctx, state, cmd, res.event_id)
    return _result(res, cmd.task_id, state, assignee_account_id=assignee)


@handles(AcceptTask)
def accept_task(cmd: AcceptTask, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.accept", channel_id=state.channel_id, domain=state.domain)
    if _replay_from_stream(ctx, cmd.task_id, cmd.idempotency_scope) is None:
        try:
            next_status(state.status, "TASK_ACCEPTED")
        except TaskTransitionError as exc:
            raise CommandError(exc.code, exc.detail, status=409) from exc
        if state.assignee_account_id != ctx.principal.account_uuid:
            raise CommandError("TASK_NOT_ASSIGNEE", "only the assignee can accept", status=403)
    res, state = _transition(
        ctx, cmd, state, "TASK_ACCEPTED", {"assignee_account_id": ctx.principal.account_uuid}
    )
    return _result(res, cmd.task_id, state)


@handles(StartTask)
def start_task(cmd: StartTask, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.progress", channel_id=state.channel_id, domain=state.domain)
    res, state = _transition(ctx, cmd, state, "TASK_STARTED", {})
    return _result(res, cmd.task_id, state)


@handles(ReportProgress)
def report_progress(cmd: ReportProgress, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.progress", channel_id=state.channel_id, domain=state.domain)
    res, state = _transition(ctx, cmd, state, "TASK_PROGRESS_REPORTED", {"summary": cmd.summary})
    return _result(res, cmd.task_id, state)


@handles(MarkWaiting)
def mark_waiting(cmd: MarkWaiting, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.progress", channel_id=state.channel_id, domain=state.domain)
    res, state = _transition(ctx, cmd, state, "TASK_WAITING", {"reason_code": cmd.reason_code})
    return _result(res, cmd.task_id, state)


@handles(SubmitImplementation)
def submit_implementation(cmd: SubmitImplementation, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.submit", channel_id=state.channel_id, domain=state.domain)
    if _replay_from_stream(ctx, cmd.task_id, cmd.idempotency_scope) is None:
        try:
            next_status(state.status, "IMPLEMENTATION_SUBMITTED")
        except TaskTransitionError as exc:
            raise CommandError(exc.code, exc.detail, status=409) from exc
        pre_submit_checks(ctx, state, cmd)
    payload = {"evidence_refs": list(cmd.evidence_refs), "criteria_revision": cmd.criteria_revision}
    res, state = _transition(ctx, cmd, state, "IMPLEMENTATION_SUBMITTED", payload)
    return _result(res, cmd.task_id, state)


@handles(StartVerification)
def start_verification(cmd: StartVerification, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "verification.assign", channel_id=state.channel_id, domain=state.domain)
    res, state = _transition(
        ctx, cmd, state, "TASK_VERIFICATION_STARTED", {"verification_id": cmd.verification_id}
    )
    return _result(res, cmd.task_id, state, verification_id=cmd.verification_id)


@handles(RecordVerificationResult)
def record_verification_result(cmd: RecordVerificationResult, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "verification.submit", channel_id=state.channel_id, domain=state.domain)
    if cmd.result not in ("PASSED", "FAILED", "BLOCKED"):
        raise CommandError("VERIFICATION_RESULT_INVALID", cmd.result, status=400)
    event_type = f"VERIFICATION_{cmd.result}"
    # replay: the verification stream already carries this command
    for ev in ctx.store.stream(ctx.workspace_id, "verification_run", cmd.verification_id):
        if (
            ev.get("idempotency_scope") == cmd.idempotency_scope
            and ev.get("idempotency_key") == ctx.idempotency_key
        ):
            return _result(
                AppendResult(ev["event_id"], ev["aggregate_seq"], ev["content_hash"], 0, True),
                cmd.task_id,
                state,
            )
    if (
        state.status is not TaskStatus.VERIFYING
        or state.active_verification_id != cmd.verification_id
    ):
        raise CommandError(
            "TASK_TRANSITION_INVALID",
            f"{event_type} is not allowed in {state.status} for {cmd.verification_id}",
            status=409,
        )
    payload: dict[str, Any] = {"verification_id": cmd.verification_id, "revision": cmd.revision}
    if cmd.result == "PASSED":
        payload["evidence_refs"] = list(cmd.evidence_refs)
    elif cmd.result == "FAILED":
        payload["finding_ids"] = list(cmd.finding_ids)
    else:
        payload["reason_code"] = cmd.reason_code
    vr_stream = ctx.store.stream(ctx.workspace_id, "verification_run", cmd.verification_id)
    res = _append(
        ctx,
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type="verification_run",
            aggregate_id=cmd.verification_id,
            type=event_type,
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            payload=payload,
            channel_id=state.channel_id,
            task_id=cmd.task_id,
            caused_by=state.last_event_id,
            expected_seq=(vr_stream[-1]["aggregate_seq"] + 1) if vr_stream else 1,
        ),
    )
    state = load_task(ctx, cmd.task_id)
    write_state(ctx.session, state, _occurred_at(ctx, res.event_id))
    return _result(res, cmd.task_id, state, verification_id=cmd.verification_id)


@handles(CompleteTask)
def complete_task(cmd: CompleteTask, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.complete", channel_id=state.channel_id, domain=state.domain)
    if _replay_from_stream(ctx, cmd.task_id, cmd.idempotency_scope) is None:
        if state.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            raise CommandError("TASK_TERMINAL", f"{state.status} is terminal", status=409)
        missing = completion_prerequisites(state, ctx.session)
        if missing:
            raise CommandError(
                missing[0],
                "completion prerequisites not met: " + ", ".join(missing),
                status=409,
                extra={"missing": missing},
            )
    payload = {
        "verification_id": state.active_verification_id or "",
        "document_id": cmd.document_id,
    }
    res, state = _transition(ctx, cmd, state, "TASK_COMPLETED", payload)
    return _result(res, cmd.task_id, state)


@handles(RequestCancel)
def request_cancel(cmd: RequestCancel, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.cancel", channel_id=state.channel_id, domain=state.domain)
    res, state = _transition(
        ctx, cmd, state, "TASK_CANCEL_REQUESTED", {"reason_code": cmd.reason_code}
    )
    return _result(res, cmd.task_id, state)


@handles(CancelTask)
def cancel_task(cmd: CancelTask, ctx: CommandContext) -> Any:
    state = load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.cancel", channel_id=state.channel_id, domain=state.domain)
    res, state = _transition(ctx, cmd, state, "TASK_CANCELLED", {"reason_code": cmd.reason_code})
    return _result(res, cmd.task_id, state)
