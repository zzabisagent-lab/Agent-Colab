"""Schedule Run execution (development plan §10A.2 steps 4-7, 10-13; P5-04..P5-07, P5-10).

``execute(run, ctx)`` takes a *claimed* Run and, in the caller's transaction: reads the action
template from the Run's pinned ScheduleVersion, re-checks the live authority (Schedule status,
execution principal, Roles/Capabilities, Channel membership, Agent selection, Approval, Secret
grants), reserves budget (§7C), decides concurrency (§8.6), creates the Task through the command
bus with the Run's deterministic idempotency key (Approval consumption in the same transaction),
records attempts/Events and posts the channel notice.

Run persistence belongs to the schedule core package: this module sees only the ``RunStore`` and
``SchedulerPorts`` protocols below, which the core adapts to its tables. Every outcome carries a
stable skip/error code; a transient failure schedules a retry (§10A.2 step 10) instead of failing.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.application import bus
from server.application import tasks as task_cmds
from server.approvals.model import ApprovalError
from server.approvals.service import consume_approval
from server.domain import defaults
from server.domain.clock import Clock
from server.events.store import AppendRequest, EventStore, EventStoreError
from server.identity.principals import Principal
from server.schedules import budget as run_budget
from server.schedules import notify, policy_check
from server.schedules.contract import (
    RUN_RUNNING,
    ConcurrencyDecision,
    ConcurrencyPolicy,
    RetryDecision,
    RetryPolicy,
    RunKind,
    RunStatus,
    ScheduleContractError,
    SkipCode,
    cancel_run,
    decide_concurrency,
    decide_retry,
    replace_cancel_confirmed,
    run_transition,
)
from server.secrets.broker import issue_lease
from server.secrets.provider import LeaseScope, SecretError

log = logging.getLogger(__name__)

TRANSIENT_CODES = frozenset({"TRANSIENT", "TIMEOUT_TRANSIENT", "PROVIDER_UNAVAILABLE"})
RUN_RUNNING_VALUES = frozenset(s.value for s in RUN_RUNNING)
TASK_SUCCESS_STATES = frozenset({"COMPLETED", "VERIFIED"})


class TransientExecutionError(Exception):
    """A retryable failure while creating the Run's Task (§10A.2 step 10)."""

    def __init__(self, code: str = "TRANSIENT", detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ------------------------------------------------------------------ row views (§6.6)


@dataclass
class ScheduleLike:
    schedule_id: str
    workspace_id: str
    name: str
    status: str
    current_version_id: str | None


@dataclass
class VersionLike:
    schedule_version_id: str
    schedule_id: str
    version: int
    channel_id: str  # channel uuid
    cron_expression: str
    timezone: str
    execution_principal_id: str  # account uuid
    agent_selection: dict[str, Any]
    action_template: dict[str, Any]
    concurrency_policy: str = "FORBID"
    missed_run_policy: str = "RUN_ONCE"
    backfill_limit: int = 0
    backfill_window_seconds: int = 0
    max_duration_seconds: int = 3600
    retry_policy: dict[str, Any] = field(default_factory=dict)
    budget_policy: dict[str, Any] = field(default_factory=dict)
    documentation_policy: dict[str, Any] = field(default_factory=dict)
    starts_at: dt.datetime | None = None
    ends_at: dt.datetime | None = None


@dataclass
class RunLike:
    run_id: str
    schedule_id: str
    schedule_version_id: str
    run_kind: str
    occurrence_key: str | None
    scheduled_for: dt.datetime
    status: str
    idempotency_key: str
    local_scheduled_for: dt.datetime | None = None
    retry_of_run_id: str | None = None
    attempt_count: int = 0
    task_id: str | None = None
    claimed_by: str | None = None
    lease_expires_at: dt.datetime | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    result_event_id: str | None = None
    error_code: str | None = None
    cancel_requested_at: dt.datetime | None = None
    cancelled_at: dt.datetime | None = None


class RunStore(Protocol):
    """What execution needs from Run persistence (the core package's ``DbRunStore``)."""

    def load_schedule(self, session: Session, schedule_id: str) -> ScheduleLike: ...

    def load_version(self, session: Session, schedule_version_id: str) -> VersionLike: ...

    def load_run(self, session: Session, run_id: str, *, for_update: bool = False) -> RunLike: ...

    def active_runs(
        self, session: Session, schedule_id: str, exclude_run_id: str
    ) -> list[RunLike]: ...

    def runs_for_task(self, session: Session, task_id: str) -> list[RunLike]: ...

    def runs_by_status(
        self, session: Session, workspace_id: str, statuses: Iterable[str]
    ) -> list[RunLike]: ...

    def run_ids_for_day(self, session: Session, schedule_id: str, day: dt.date) -> list[str]: ...

    def update_run(self, session: Session, run_id: str, **cols: Any) -> RunLike: ...

    def add_attempt(
        self,
        session: Session,
        run_id: str,
        attempt_no: int,
        *,
        started_at: dt.datetime | None,
        finished_at: dt.datetime | None,
        result: str | None,
        error_code: str | None,
    ) -> None: ...

    def create_run(self, session: Session, run: RunLike) -> RunLike: ...


class SchedulerPorts(Protocol):
    """The core package's planner/runner surface used by the periodic scheduler tick."""

    def expire_leases(self, session: Session, now: dt.datetime) -> int: ...

    def materialize(
        self, session: Session, store: EventStore, clock: Clock, workspace_id: str, horizon_s: int
    ) -> int: ...

    def claim_due(
        self, session: Session, runner_id: str, now: dt.datetime, lease_s: int, limit: int
    ) -> list[RunLike]: ...

    def heartbeat(
        self, session: Session, run_id: str, runner_id: str, now: dt.datetime, lease_s: int
    ) -> None: ...


# ------------------------------------------------------------------ context / outcome


@dataclass
class ExecutionContext:
    """Everything execution needs; one instance per tick, reused across Runs."""

    session: Session
    store: RunStore
    event_store: EventStore
    clock: Clock
    workspace_id: str
    runner_id: str
    actor: Principal  # the Workspace's system service Account (Run Events and budget actor)
    authorizer: Any = None
    # retry jitter only; never used for anything security-relevant
    rng: random.Random = field(  # nosec B311 - retry backoff jitter
        default_factory=lambda: random.Random(0)  # noqa: S311  # nosec B311
    )
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def now(self) -> dt.datetime:
        return self.clock.now()

    @property
    def workspace_uuid(self) -> uuid.UUID:
        return uuid.UUID(self.workspace_id)


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: str  # Run status after this call
    error_code: str | None = None
    task_id: str | None = None
    attempt_no: int = 0
    event_id: str | None = None
    deferred: bool = False  # REPLACE: waiting for the previous Run's cancel confirmation
    retry_at: dt.datetime | None = None
    detail: str = ""


def retry_policy_of(version: VersionLike) -> RetryPolicy:
    rp = dict(version.retry_policy or {})
    kwargs: dict[str, Any] = {}
    if "max_attempts" in rp:
        kwargs["max_attempts"] = int(rp["max_attempts"])
    if "backoff_seconds" in rp:
        kwargs["backoff_seconds"] = tuple(int(x) for x in rp["backoff_seconds"])
    if "jitter_ratio" in rp:
        kwargs["jitter_ratio"] = float(rp["jitter_ratio"])
    return RetryPolicy(**kwargs)


# ------------------------------------------------------------------ Events / transitions


def append_run_event(
    ctx: ExecutionContext,
    run: RunLike,
    event_type: str,
    payload: dict[str, Any],
    *,
    op: str,
    suffix: str = "",
) -> str | None:
    """Append one ``RUN_*`` Event (idempotent per run/op/suffix); None on an idempotent replay."""
    body = {"run_id": run.run_id, "schedule_id": run.schedule_id, **payload}
    key = f"{run.run_id}:{op}:{suffix}" if suffix else f"{run.run_id}:{op}"
    try:
        res = ctx.event_store.append(
            AppendRequest(
                workspace_id=ctx.workspace_id,
                aggregate_type="schedule_run",
                aggregate_id=run.run_id,
                type=event_type,
                actor_account_id=ctx.actor.account_uuid,
                correlation_id=run.run_id,
                idempotency_scope=f"schedule_run:{op}",
                idempotency_key=key,
                payload=body,
                task_id=run.task_id,
            )
        )
    except EventStoreError as exc:
        if exc.code != "IDEMPOTENCY_CONFLICT":
            raise
        return None
    return res.event_id


def _set(ctx: ExecutionContext, run: RunLike, target: RunStatus, **cols: Any) -> RunLike:
    """Transition through the contract table, then persist through the core store."""
    run_transition(RunStatus(run.status), target)
    return ctx.store.update_run(ctx.session, run.run_id, status=target.value, **cols)


# ------------------------------------------------------------------ execute (steps 4-7)


def coerce_run(run: Any) -> RunLike:
    """Accept a ``RunLike`` or any row object exposing the same §6.6 attributes."""
    if isinstance(run, RunLike):
        return run
    version_id = getattr(run, "version_public_id", None) or str(run.schedule_version_id)
    return RunLike(
        run_id=run.run_id,
        schedule_id=run.schedule_id,
        schedule_version_id=version_id,
        run_kind=run.run_kind,
        occurrence_key=run.occurrence_key,
        scheduled_for=run.scheduled_for,
        status=run.status,
        idempotency_key=run.idempotency_key,
        local_scheduled_for=getattr(run, "local_scheduled_for", None),
        retry_of_run_id=getattr(run, "retry_of_run_id", None),
        attempt_count=getattr(run, "attempt_count", 0),
        task_id=getattr(run, "task_id", None),
        claimed_by=getattr(run, "claimed_by", None),
        lease_expires_at=getattr(run, "lease_expires_at", None),
        started_at=getattr(run, "started_at", None),
        finished_at=getattr(run, "finished_at", None),
        result_event_id=getattr(run, "result_event_id", None),
        error_code=getattr(run, "error_code", None),
        cancel_requested_at=getattr(run, "cancel_requested_at", None),
        cancelled_at=getattr(run, "cancelled_at", None),
    )


def execute(run: Any, ctx: ExecutionContext) -> RunOutcome:
    """§10A.2 steps 4-7 for one claimed Run. Policy/budget/concurrency skips never raise."""
    run = coerce_run(run)
    if run.status != RunStatus.CLAIMED.value:
        raise ScheduleContractError(
            "RUN_STATUS_INVALID", f"execute needs CLAIMED, got {run.status}"
        )
    now = ctx.now
    schedule = ctx.store.load_schedule(ctx.session, run.schedule_id)
    version = ctx.store.load_version(ctx.session, run.schedule_version_id)

    check = policy_check.check(ctx, run, schedule, version)
    if not check.ok:
        return _skip(ctx, run, version, check.skip_code or SkipCode.SKIPPED_POLICY, check.detail)

    reserved = run_budget.reserve_for_run(ctx, run, version)
    if not reserved.ok:
        return _skip(ctx, run, version, SkipCode.BUDGET_EXCEEDED, reserved.detail)

    concurrency = _concurrency(ctx, run, version, now)
    if concurrency is not None:
        return concurrency

    attempt_no = run.attempt_count + 1
    ctx.store.add_attempt(
        ctx.session,
        run.run_id,
        attempt_no,
        started_at=now,
        finished_at=None,
        result=None,
        error_code=None,
    )
    try:
        task_id = _create_task(ctx, run, schedule, version, check)
    except TransientExecutionError as exc:
        return _transient(ctx, run, version, attempt_no, exc.code, exc.detail)
    except bus.CommandError as exc:
        if exc.code in TRANSIENT_CODES:
            return _transient(ctx, run, version, attempt_no, exc.code, exc.detail)
        return _fail(ctx, run, version, attempt_no, exc.code, exc.detail)
    except (SecretError, ApprovalError) as exc:
        return _fail(ctx, run, version, attempt_no, exc.code, exc.detail)

    started = _set(
        ctx,
        run,
        RunStatus.TASK_CREATED,
        task_id=task_id,
        started_at=now,
        attempt_count=attempt_no,
        error_code=None,
    )
    event_id = append_run_event(
        ctx,
        started,
        "RUN_STARTED",
        {"attempt_no": attempt_no, "task_id": task_id, "run_kind": run.run_kind},
        op="started",
        suffix=str(attempt_no),
    )
    notify.start(ctx, started, version)
    delay = (now - run.scheduled_for).total_seconds()
    if run.run_kind == RunKind.SCHEDULED.value and delay > defaults.SCHEDULE_START_DELAY_P95_S:
        notify.late(ctx, started, version, delay)
        run_budget.raise_alert(
            ctx.session,
            workspace_id=ctx.workspace_id,
            kind="start_delay",
            schedule_id=run.schedule_id,
            run_id=run.run_id,
            detail={"delay_s": int(delay), "target_s": defaults.SCHEDULE_START_DELAY_P95_S},
            now=now,
        )
    return RunOutcome(run.run_id, started.status, None, task_id, attempt_no, event_id)


def _concurrency(
    ctx: ExecutionContext, run: RunLike, version: VersionLike, now: dt.datetime
) -> RunOutcome | None:
    policy = ConcurrencyPolicy(version.concurrency_policy)
    active = ctx.store.active_runs(ctx.session, run.schedule_id, run.run_id)
    if not active:
        return None
    if policy is ConcurrencyPolicy.REPLACE:
        previous = active[0]
        if previous.cancel_requested_at is None:
            previous = request_cancel(ctx, previous, "REPLACED_BY_NEW_RUN")
        confirmed: bool | None = None
        terminal = previous.cancelled_at is not None or previous.status in (
            RunStatus.CANCELLED.value,
            RunStatus.TIMED_OUT.value,
        )
        if terminal:  # cancel acknowledged and cleaned up
            confirmed = replace_cancel_confirmed(
                previous.cancel_requested_at or now, previous.cancelled_at or previous.finished_at
            )
        elif (now - (previous.cancel_requested_at or now)).total_seconds() > (
            defaults.SCHEDULE_REPLACE_CANCEL_TIMEOUT_S
        ):
            confirmed = False  # the previous Run never confirmed within the window
        outcome = decide_concurrency(policy, True, confirmed)
        if outcome.decision is ConcurrencyDecision.REPLACE_CANCEL_EXISTING:
            ctx.store.update_run(  # keep the claim and lease; re-evaluate on the next tick
                ctx.session,
                run.run_id,
                lease_expires_at=now + dt.timedelta(seconds=defaults.SCHEDULER_CLAIM_LEASE_S),
            )
            return RunOutcome(run.run_id, run.status, None, deferred=True, detail="REPLACE_WAIT")
        if outcome.decision is ConcurrencyDecision.SKIP:
            run_budget.release_for_run(ctx, run)
            return _skip(
                ctx, run, version, SkipCode(outcome.error_code or ""), "replace cancel timeout"
            )
        return None
    outcome = decide_concurrency(policy, True)
    if outcome.decision is ConcurrencyDecision.SKIP:
        run_budget.release_for_run(ctx, run)
        return _skip(ctx, run, version, SkipCode(outcome.error_code or ""), "previous Run active")
    return None


def _create_task(
    ctx: ExecutionContext,
    run: RunLike,
    schedule: ScheduleLike,
    version: VersionLike,
    check: policy_check.PolicyResult,
) -> str:
    """Step 5: Task creation, Approval consumption and Secret leases in one transaction."""
    template = dict(version.action_template or {})
    action = str(template.get("action", "task_create"))
    if action != "task_create":
        raise bus.CommandError(
            "SCHEDULE_ACTION_UNSUPPORTED", f"{action} is not executable by the scheduler", 400
        )
    inp = dict(template.get("input", {}))
    principal = check.principal
    assert principal is not None
    if check.approval_id is not None and check.approval_subject is not None:
        consume_approval(
            ctx.session,
            ctx.event_store,
            ctx.clock,
            approval_id=check.approval_id,
            consumption_key=run.idempotency_key,
            consumed_by=uuid.UUID(principal.account_uuid),
            consumed_for=check.approval_subject,
            correlation_id=run.run_id,
        )
    criteria = tuple(
        inp.get("criteria")
        or [{"statement": "scheduled run completed", "check_type": "evidence", "required": True}]
    )
    title = str(inp.get("title") or f"{schedule.name} @ {run.scheduled_for.isoformat()}")
    result = bus.execute(
        task_cmds.CreateTask(
            title=title,
            channel_id=str(version.channel_id),
            domain=str(inp.get("domain", "general")),
            risk=str(inp.get("risk", "LOW")),
            criteria=criteria,
        ),
        _ctx_for(ctx, principal, run.idempotency_key, run.run_id),
    )
    task_id = str(result.resource_id)
    if check.agent is not None:
        bus.execute(
            task_cmds.DelegateTask(task_id, check.agent.account_id, reason_code="DELEGATED"),
            _ctx_for(ctx, principal, f"{run.idempotency_key}:delegate", run.run_id),
        )
    for ref in template.get("secret_refs", []):
        issue_lease(
            ctx.session,
            workspace_id=ctx.workspace_uuid,
            secret_ref=str(ref),
            scope=LeaseScope(
                agent_id=check.agent.agent_id if check.agent else "-",
                task_id=task_id,
                action=action,
            ),
            ttl=dt.timedelta(seconds=min(version.max_duration_seconds, 300)),
            single_use=True,
            now=ctx.now,
            actor_label=f"schedule:{schedule.schedule_id}",
            correlation_id=run.run_id,
        )
    return task_id


def _ctx_for(
    ctx: ExecutionContext, principal: Principal, idempotency_key: str, correlation_id: str
) -> bus.CommandContext:
    return bus.CommandContext(
        session=ctx.session,
        store=ctx.event_store,
        authorizer=ctx.authorizer,
        clock=ctx.clock,
        principal=bus.Principal(
            principal.account_id,
            principal.account_uuid,
            principal.account_type,
            principal.credential_fingerprint,
        ),
        workspace_id=ctx.workspace_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        extras=dict(ctx.extras),
    )


# ------------------------------------------------------------------ outcomes


def _skip(
    ctx: ExecutionContext,
    run: RunLike,
    version: VersionLike,
    code: SkipCode | str,
    detail: str,
) -> RunOutcome:
    code_s = code.value if isinstance(code, SkipCode) else str(code)
    now = ctx.now
    skipped = _set(ctx, run, RunStatus.SKIPPED, error_code=code_s, finished_at=now)
    event_id = append_run_event(
        ctx, skipped, "RUN_SKIPPED", {"error_code": code_s, "detail": detail[:200]}, op="skipped"
    )
    if event_id:
        skipped = ctx.store.update_run(ctx.session, run.run_id, result_event_id=event_id)
    notify.skip(ctx, skipped, version, code_s, detail)
    return RunOutcome(
        run.run_id,
        RunStatus.SKIPPED.value,
        code_s,
        None,
        run.attempt_count,
        event_id,
        detail=detail,
    )


def _fail(
    ctx: ExecutionContext,
    run: RunLike,
    version: VersionLike,
    attempt_no: int,
    code: str,
    detail: str,
) -> RunOutcome:
    now = ctx.now
    ctx.store.add_attempt(
        ctx.session,
        run.run_id,
        attempt_no,
        started_at=run.started_at or now,
        finished_at=now,
        result="FAILED",
        error_code=code,
    )
    failed = _set(
        ctx, run, RunStatus.FAILED, error_code=code, finished_at=now, attempt_count=attempt_no
    )
    event_id = append_run_event(
        ctx,
        failed,
        "RUN_FAILED",
        {"attempt_no": attempt_no, "error_code": code},
        op="failed",
        suffix=str(attempt_no),
    )
    if event_id:
        failed = ctx.store.update_run(ctx.session, run.run_id, result_event_id=event_id)
    run_budget.release_for_run(ctx, failed)
    clear_retry(ctx.session, run.run_id)
    notify.failure(ctx, failed, version, code, detail)
    return RunOutcome(
        run.run_id, RunStatus.FAILED.value, code, None, attempt_no, event_id, detail=detail
    )


def _transient(
    ctx: ExecutionContext,
    run: RunLike,
    version: VersionLike,
    attempt_no: int,
    code: str,
    detail: str,
) -> RunOutcome:
    """§10A.2 step 10: at most 3 attempts at 1/5/25 s (+0-20 % jitter), else terminal FAILED."""
    policy = retry_policy_of(version)
    normalized = code if code in policy.transient_error_codes else "TRANSIENT"
    outcome = decide_retry(policy, attempt_no, normalized)
    now = ctx.now
    ctx.store.add_attempt(
        ctx.session,
        run.run_id,
        attempt_no,
        started_at=run.started_at or now,
        finished_at=now,
        result="FAILED",
        error_code=code,
    )
    if outcome.decision is not RetryDecision.RETRY or outcome.next_attempt_no is None:
        failed_code = (
            "RETRY_EXHAUSTED" if outcome.decision is RetryDecision.FAIL_EXHAUSTED else code
        )
        failed = _set(
            ctx,
            run,
            RunStatus.FAILED,
            error_code=failed_code,
            finished_at=now,
            attempt_count=attempt_no,
        )
        event_id = append_run_event(
            ctx,
            failed,
            "RUN_FAILED",
            {"attempt_no": attempt_no, "error_code": failed_code},
            op="failed",
            suffix=str(attempt_no),
        )
        if event_id:
            failed = ctx.store.update_run(ctx.session, run.run_id, result_event_id=event_id)
        run_budget.release_for_run(ctx, failed)
        clear_retry(ctx.session, run.run_id)
        notify.failure(ctx, failed, version, failed_code, detail)
        return RunOutcome(
            run.run_id, RunStatus.FAILED.value, failed_code, None, attempt_no, event_id
        )
    lo, hi = outcome.delay_min_s or 0.0, outcome.delay_max_s or 0.0
    delay = lo + (hi - lo) * ctx.rng.random()
    retry_at = now + dt.timedelta(seconds=delay)
    # the Run keeps its claim until the retry is due; the lease covers the wait
    ctx.store.update_run(
        ctx.session,
        run.run_id,
        attempt_count=attempt_no,
        error_code=code,
        lease_expires_at=retry_at + dt.timedelta(seconds=defaults.SCHEDULER_CLAIM_LEASE_S),
    )
    schedule_retry(ctx.session, run.run_id, outcome.next_attempt_no, retry_at, code, now)
    return RunOutcome(
        run.run_id, run.status, code, None, attempt_no, None, retry_at=retry_at, detail=detail
    )


# ------------------------------------------------------------------ retries


def schedule_retry(
    session: Session,
    run_id: str,
    next_attempt_no: int,
    at: dt.datetime,
    code: str,
    now: dt.datetime,
) -> None:
    session.execute(
        text(
            "INSERT INTO schedule_run_retries (run_id, next_attempt_no, next_attempt_at, "
            "error_code, created_at) VALUES (:r, :n, :at, :c, :now) ON CONFLICT (run_id) DO UPDATE "
            "SET next_attempt_no = EXCLUDED.next_attempt_no, "
            "next_attempt_at = EXCLUDED.next_attempt_at, error_code = EXCLUDED.error_code"
        ),
        {"r": run_id, "n": next_attempt_no, "at": at, "c": code, "now": now},
    )


def due_retries(session: Session, now: dt.datetime, limit: int = 50) -> list[str]:
    rows = session.execute(
        text(
            "SELECT run_id FROM schedule_run_retries WHERE next_attempt_at <= :now "
            "ORDER BY next_attempt_at LIMIT :lim FOR UPDATE SKIP LOCKED"
        ),
        {"now": now, "lim": limit},
    ).all()
    return [str(r[0]) for r in rows]


def pending_retry(session: Session, run_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            "SELECT next_attempt_no, next_attempt_at, error_code FROM schedule_run_retries "
            "WHERE run_id = :r"
        ),
        {"r": run_id},
    ).first()
    if row is None:
        return None
    return {"next_attempt_no": int(row[0]), "next_attempt_at": row[1], "error_code": str(row[2])}


def clear_retry(session: Session, run_id: str) -> None:
    session.execute(text("DELETE FROM schedule_run_retries WHERE run_id = :r"), {"r": run_id})


# ------------------------------------------------------------------ cancel / cleanup


def request_cancel(ctx: ExecutionContext, run: RunLike, reason_code: str) -> RunLike:
    """Pending Runs cancel immediately; running Runs enter CANCEL_REQUESTED and their Task is
    asked to cancel (Adapter ack ≤ 10 s, cleanup ≤ 60 s — see ``recovery.handle_timeouts``)."""
    target = cancel_run(RunStatus(run.status))
    now = ctx.now
    if target is RunStatus.CANCELLED:
        cancelled = _set(
            ctx, run, RunStatus.CANCELLED, cancelled_at=now, finished_at=now, error_code=reason_code
        )
        event_id = append_run_event(
            ctx, cancelled, "RUN_CANCELLED", {"reason_code": reason_code}, op="cancelled"
        )
        if event_id:
            cancelled = ctx.store.update_run(ctx.session, run.run_id, result_event_id=event_id)
        run_budget.release_for_run(ctx, cancelled)
        clear_retry(ctx.session, run.run_id)
        return cancelled
    requested = _set(
        ctx, run, RunStatus.CANCEL_REQUESTED, cancel_requested_at=now, error_code=reason_code
    )
    append_run_event(
        ctx, requested, "RUN_CANCEL_REQUESTED", {"reason_code": reason_code}, op="cancel_requested"
    )
    if run.task_id:
        try:
            bus.execute(
                task_cmds.CancelTask(run.task_id, reason_code="SCHEDULE_RUN_CANCELLED"),
                _ctx_for(ctx, ctx.actor, f"{run.run_id}:cancel", run.run_id),
            )
        except bus.CommandError as exc:  # already terminal: the cleanup window decides
            log.info("cancel request for task %s: %s", run.task_id, exc.code)
    return requested


def finish_cancel(ctx: ExecutionContext, run: RunLike, *, timed_out: bool, reason: str) -> RunLike:
    """CANCEL_REQUESTED → CANCELLED (ack and cleanup confirmed) or TIMED_OUT, then cleans up."""
    now = ctx.now
    version = ctx.store.load_version(ctx.session, run.schedule_version_id)
    if timed_out:
        finished = _set(ctx, run, RunStatus.TIMED_OUT, finished_at=now, error_code=reason)
        event_id = append_run_event(
            ctx,
            finished,
            "RUN_TIMED_OUT",
            {"attempt_no": max(run.attempt_count, 1), "reason_code": reason},
            op="timed_out",
        )
    else:
        finished = _set(ctx, run, RunStatus.CANCELLED, cancelled_at=now, finished_at=now)
        event_id = append_run_event(
            ctx, finished, "RUN_CANCELLED", {"reason_code": reason}, op="cancelled"
        )
    if event_id:
        finished = ctx.store.update_run(ctx.session, run.run_id, result_event_id=event_id)
    cleanup(ctx, finished)
    notify.failure(ctx, finished, version, finished.error_code or reason, reason)
    return finished


def cleanup(ctx: ExecutionContext, run: RunLike) -> None:
    """Lease, budget and retry cleanup for a Run that ended (§9.3 revoke at Task end)."""
    if run.task_id:
        from server.secrets.broker import revoke_for_task

        try:
            revoke_for_task(
                ctx.session,
                workspace_id=ctx.workspace_uuid,
                task_id=run.task_id,
                now=ctx.now,
                actor_label=f"schedule:{run.schedule_id}",
                correlation_id=run.run_id,
                store=ctx.event_store,
            )
        except Exception:  # no leases for this Task
            log.debug("no leases to revoke for %s", run.task_id, exc_info=True)
    run_budget.settle_for_run(ctx, run)
    clear_retry(ctx.session, run.run_id)


# ------------------------------------------------------------------ Task terminal → Run terminal


def on_task_terminal(ctx: ExecutionContext, task_id: str, task_status: str) -> list[RunLike]:
    """Close the Run(s) whose Task reached a terminal state (§10A.2 step 7)."""
    closed: list[RunLike] = []
    for run in ctx.store.runs_for_task(ctx.session, task_id):
        if run.status not in RUN_RUNNING_VALUES:
            continue
        version = ctx.store.load_version(ctx.session, run.schedule_version_id)
        now = ctx.now
        attempt_no = max(run.attempt_count, 1)
        if run.status == RunStatus.CANCEL_REQUESTED.value:
            closed.append(finish_cancel(ctx, run, timed_out=False, reason="CANCELLED"))
            continue
        if run.status == RunStatus.TASK_CREATED.value:
            run = _set(ctx, run, RunStatus.RUNNING)
        if task_status in TASK_SUCCESS_STATES:
            done = _set(ctx, run, RunStatus.SUCCEEDED, finished_at=now, error_code=None)
            event_id = append_run_event(
                ctx,
                done,
                "RUN_SUCCEEDED",
                {"attempt_no": attempt_no, "task_id": task_id},
                op="succeeded",
                suffix=str(attempt_no),
            )
            result, code = "SUCCEEDED", None
        else:
            code = f"TASK_{task_status}"
            done = _set(ctx, run, RunStatus.FAILED, finished_at=now, error_code=code)
            event_id = append_run_event(
                ctx,
                done,
                "RUN_FAILED",
                {"attempt_no": attempt_no, "error_code": code, "task_id": task_id},
                op="failed",
                suffix=str(attempt_no),
            )
            result = "FAILED"
        if event_id:
            done = ctx.store.update_run(ctx.session, run.run_id, result_event_id=event_id)
        ctx.store.add_attempt(
            ctx.session,
            run.run_id,
            attempt_no,
            started_at=run.started_at or now,
            finished_at=now,
            result=result,
            error_code=code,
        )
        cleanup(ctx, done)
        if result == "SUCCEEDED":
            notify.result(ctx, done, version)
        else:
            notify.failure(ctx, done, version, code or "FAILED", task_status)
        closed.append(done)
    return closed
