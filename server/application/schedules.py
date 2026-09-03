"""Schedule commands and reads on the command bus (P5-01; development plan §10A, §6.6).

Writes: create, version (PATCH = new immutable version), enable/pause/resume/disable, run now,
cancel a Run, retry a terminal Run. Every write appends exactly one Event (`SCHEDULE_*` on the
``schedule`` aggregate, `RUN_*` on the ``schedule_run`` aggregate) and is idempotent on the
caller's key. Planning and claiming live in ``server.schedules.planner`` / ``runner``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
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
from server.artifacts import links as artifact_links
from server.domain import defaults
from server.events.store import AppendRequest, AppendResult
from server.observability.audit import append_audit
from server.schedules import contract as sc
from server.schedules import cron
from server.schedules import store as st
from server.schedules.contract import ScheduleContractError
from server.schedules.links import ScheduleRunSubjectHandler
from server.schedules.occurrence import manual_idempotency_key, retry_idempotency_key
from server.schedules.validate import validate_schedule_version

DEFAULT_RETRY_POLICY: dict[str, Any] = {
    "max_attempts": defaults.SCHEDULE_RETRY_MAX_ATTEMPTS,
    "backoff_seconds": list(defaults.SCHEDULE_RETRY_BACKOFF_S),
    "jitter_ratio": defaults.SCHEDULE_RETRY_JITTER_MAX_RATIO,
}
DEFAULT_BUDGET_POLICY: dict[str, Any] = {
    "per_run_cost_units": 1_000_000,
    "daily_cost_units": 10_000_000,
}
DEFAULT_DOCUMENTATION_POLICY: dict[str, Any] = {"draft": True}
RETRYABLE_TERMINAL: frozenset[str] = frozenset({"FAILED", "TIMED_OUT", "CANCELLED", "SKIPPED"})

# activate the ScheduleRun ArtifactLink subject (§6.8) as soon as this module is imported
artifact_links.REGISTRY.register(ScheduleRunSubjectHandler())


# ------------------------------------------------------------------------------- commands
@dataclass(frozen=True)
class CreateSchedule(Command):
    name: str
    cron_expression: str
    timezone: str
    channel_id: str  # public channel_id or channel uuid
    execution_principal_id: str  # public account_id or account uuid
    agent_selection: dict[str, Any]
    action_template: dict[str, Any]
    concurrency_policy: str = sc.DEFAULT_CONCURRENCY.value
    missed_run_policy: str = sc.DEFAULT_MISSED_RUN.value
    backfill_limit: int = 0
    backfill_window_seconds: int = 0
    max_duration_seconds: int = 3600
    min_interval_minutes: int = defaults.SCHEDULE_MIN_INTERVAL_MINUTES_DEFAULT
    retry_policy: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RETRY_POLICY))
    budget_policy: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_BUDGET_POLICY))
    documentation_policy: dict[str, Any] = field(
        default_factory=lambda: dict(DEFAULT_DOCUMENTATION_POLICY)
    )
    starts_at: str | None = None
    ends_at: str | None = None
    schedule_id: str | None = None
    idempotency_scope: str = "schedule:create"


@dataclass(frozen=True)
class CommitScheduleVersion(Command):
    schedule_id: str
    changes: dict[str, Any]
    idempotency_scope: str = "schedule:update"


@dataclass(frozen=True)
class EnableSchedule(Command):
    schedule_id: str
    idempotency_scope: str = "schedule:enable"


@dataclass(frozen=True)
class PauseSchedule(Command):
    schedule_id: str
    idempotency_scope: str = "schedule:pause"


@dataclass(frozen=True)
class ResumeSchedule(Command):
    schedule_id: str
    idempotency_scope: str = "schedule:resume"


@dataclass(frozen=True)
class DisableSchedule(Command):
    schedule_id: str
    idempotency_scope: str = "schedule:disable"


@dataclass(frozen=True)
class RunScheduleNow(Command):
    schedule_id: str
    client_key: str | None = None  # defaults to the caller's Idempotency-Key
    idempotency_scope: str = "schedule_run:run_now"


@dataclass(frozen=True)
class CancelScheduleRun(Command):
    run_id: str
    reason_code: str = "USER_CANCEL"
    idempotency_scope: str = "schedule_run:cancel"


@dataclass(frozen=True)
class RetryScheduleRun(Command):
    run_id: str
    idempotency_scope: str = "schedule_run:retry"


# -------------------------------------------------------------------------------- helpers
def _ws(ctx: CommandContext) -> uuid.UUID:
    return uuid.UUID(ctx.workspace_id)


def _contract(exc: ScheduleContractError | cron.CronError) -> CommandError:
    return CommandError(exc.code, exc.detail, status=400)


def _load_schedule(ctx: CommandContext, schedule_id: str, *, lock: bool = True) -> st.ScheduleRow:
    row = st.load_schedule(ctx.session, _ws(ctx), schedule_id, for_update=lock)
    if row is None:
        raise CommandError("SCHEDULE_NOT_FOUND", schedule_id, status=404)
    return row


def _current_version(ctx: CommandContext, schedule: st.ScheduleRow) -> st.VersionRow:
    if schedule.current_version_id is None:
        raise CommandError("SCHEDULE_VERSION_MISSING", schedule.schedule_id, status=409)
    version = st.load_version(ctx.session, schedule.current_version_id)
    if version is None:  # pragma: no cover - FK guarantees the row
        raise CommandError("SCHEDULE_VERSION_MISSING", schedule.schedule_id, status=409)
    return version


def _replay(
    ctx: CommandContext, aggregate: str, aggregate_id: str, scope: str
) -> AppendResult | None:
    for ev in ctx.store.stream(ctx.workspace_id, aggregate, aggregate_id):
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
                replayed=True,
            )
    return None


def _append(
    ctx: CommandContext,
    aggregate: str,
    aggregate_id: str,
    scope: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> AppendResult:
    return ctx.store.append(
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type=aggregate,
            aggregate_id=aggregate_id,
            type=event_type,
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=scope,
            idempotency_key=idempotency_key or ctx.idempotency_key,
            payload=payload,
        )
    )


def _audit(ctx: CommandContext, action: str, target_type: str, target_id: str, **meta: Any) -> None:
    append_audit(
        ctx.session,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result="OK",
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=_ws(ctx),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata=meta,
        clock=ctx.clock,
    )


def _resolve_channel(ctx: CommandContext, ref: str) -> uuid.UUID:
    row = ctx.session.execute(
        text(
            "SELECT id FROM channels WHERE workspace_id = :w AND (channel_id = :r "
            "OR CAST(id AS text) = :r)"
        ),
        {"w": _ws(ctx), "r": ref},
    ).first()
    if row is None:
        raise CommandError("CHANNEL_NOT_FOUND", ref, status=404)
    return uuid.UUID(str(row[0]))


def _resolve_account(ctx: CommandContext, ref: str) -> uuid.UUID:
    row = ctx.session.execute(
        text(
            "SELECT id FROM accounts WHERE workspace_id = :w AND (account_id = :r "
            "OR CAST(id AS text) = :r)"
        ),
        {"w": _ws(ctx), "r": ref},
    ).first()
    if row is None:
        raise CommandError("ACCOUNT_NOT_FOUND", ref, status=404)
    return uuid.UUID(str(row[0]))


def _validated_content(
    ctx: CommandContext, schedule_id: str, version: int, content: dict[str, Any]
) -> dict[str, Any]:
    """Resolve refs, fill defaults and validate the version body against the Phase 0 schema."""
    body = dict(content)
    body["channel_id"] = str(_resolve_channel(ctx, str(body["channel_id"])))
    body["execution_principal_id"] = str(_resolve_account(ctx, str(body["execution_principal_id"])))
    body.setdefault("retry_policy", dict(DEFAULT_RETRY_POLICY))
    body.setdefault("budget_policy", dict(DEFAULT_BUDGET_POLICY))
    body.setdefault("documentation_policy", dict(DEFAULT_DOCUMENTATION_POLICY))
    body.setdefault("min_interval_minutes", defaults.SCHEDULE_MIN_INTERVAL_MINUTES_DEFAULT)
    body.setdefault("starts_at", None)
    body.setdefault("ends_at", None)
    try:  # grammar and timezone first: their codes are the contract's stable ones (V-P5-01)
        cron.load_zone(str(body["timezone"]))
        cron.validate(str(body["cron_expression"]), int(body["min_interval_minutes"]))
    except cron.CronError as exc:
        raise _contract(exc) from exc
    probe = {
        "schedule_version_id": "schv-000000000000",
        "schedule_id": schedule_id,
        "version": version,
        **{k: body.get(k) for k in st.VERSION_CONTENT_FIELDS},
        "snapshot_hash": st.snapshot_hash(body),
        "created_at": st.iso_ms(ctx.clock.now()),
    }
    try:
        validate_schedule_version(probe)
    except ScheduleContractError as exc:
        raise _contract(exc) from exc
    return {k: body.get(k) for k in st.VERSION_CONTENT_FIELDS}


def _schedule_result(
    ctx: CommandContext, res: AppendResult, schedule_id: str, **data: Any
) -> CommandResult:
    view = schedule_view(ctx, schedule_id)
    return CommandResult(
        schedule_id,
        res.event_id,
        res.aggregate_seq,
        "schedule",
        replayed=res.replayed,
        data={**view, **data},
    )


def _run_result(
    ctx: CommandContext, res: AppendResult, run: st.RunRow, **data: Any
) -> CommandResult:
    fresh = st.load_run(ctx.session, run.run_id) or run
    return CommandResult(
        fresh.run_id,
        res.event_id,
        res.aggregate_seq,
        "schedule_run",
        replayed=res.replayed,
        data={**fresh.view(), **data},
    )


def _new_schedule_id(ctx: CommandContext) -> str:
    material = (
        f"{ctx.workspace_id}|{ctx.principal.account_uuid}|schedule:create|{ctx.idempotency_key}"
    )
    return "sch-" + hashlib.sha256(material.encode()).hexdigest()[:12]


def _next_run_at(version: st.VersionRow, after: dt.datetime) -> dt.datetime | None:
    occ = cron.next_occurrences(
        version.cron_expression,
        version.timezone,
        after,
        count=1,
        schedule_id=version.schedule_id,
        include_gaps=False,
    )
    return occ[0].utc if occ else None


def _cancel_pending_runs(ctx: CommandContext, schedule_id: str, reason_code: str) -> list[str]:
    cancelled: list[str] = []
    now = ctx.clock.now()
    for run in st.pending_runs(ctx.session, schedule_id, for_update=True):
        st.update_run(
            ctx.session,
            run.run_id,
            now,
            status="CANCELLED",
            cancelled_at=now,
            error_code=reason_code,
        )
        _append(
            ctx,
            "schedule_run",
            run.run_id,
            "schedule_run:cancel",
            "RUN_CANCELLED",
            {"run_id": run.run_id, "reason_code": reason_code},
            idempotency_key=f"{ctx.idempotency_key}:{run.run_id}",
        )
        cancelled.append(run.run_id)
    return cancelled


# ------------------------------------------------------------------------------- handlers
@handles(CreateSchedule)
def create_schedule(cmd: CreateSchedule, ctx: CommandContext) -> CommandResult:
    channel_uuid = _resolve_channel(ctx, cmd.channel_id)
    require_permission(
        ctx, "schedule.manage", action="api:schedule_create", channel_id=str(channel_uuid)
    )
    schedule_id = cmd.schedule_id or _new_schedule_id(ctx)
    replay = _replay(ctx, "schedule", schedule_id, cmd.idempotency_scope)
    if replay is not None:
        return _schedule_result(ctx, replay, schedule_id)
    if st.load_schedule(ctx.session, _ws(ctx), schedule_id) is not None:
        raise CommandError("SCHEDULE_ALREADY_EXISTS", schedule_id, status=409)
    if not cmd.schedule_id and not schedule_id.startswith("sch-"):
        raise CommandError("SCHEDULE_ID_INVALID", schedule_id, status=400)
    if cmd.schedule_id and not cmd.schedule_id.startswith("sch-"):
        raise CommandError("SCHEDULE_ID_INVALID", "schedule ids start with sch-", status=400)
    content = _validated_content(
        ctx,
        schedule_id,
        1,
        {
            "name": cmd.name,
            "cron_expression": cmd.cron_expression,
            "timezone": cmd.timezone,
            "channel_id": cmd.channel_id,
            "execution_principal_id": cmd.execution_principal_id,
            "agent_selection": cmd.agent_selection,
            "action_template": cmd.action_template,
            "concurrency_policy": cmd.concurrency_policy,
            "missed_run_policy": cmd.missed_run_policy,
            "backfill_limit": cmd.backfill_limit,
            "backfill_window_seconds": cmd.backfill_window_seconds,
            "max_duration_seconds": cmd.max_duration_seconds,
            "min_interval_minutes": cmd.min_interval_minutes,
            "retry_policy": cmd.retry_policy,
            "budget_policy": cmd.budget_policy,
            "documentation_policy": cmd.documentation_policy,
            "starts_at": cmd.starts_at,
            "ends_at": cmd.ends_at,
        },
    )
    now = ctx.clock.now()
    actor = uuid.UUID(ctx.principal.account_uuid)
    st.insert_schedule(
        ctx.session,
        workspace_id=_ws(ctx),
        schedule_id=schedule_id,
        name=cmd.name,
        created_by=actor,
        now=now,
    )
    version_id = st.new_version_id()
    res = _append(
        ctx,
        "schedule",
        schedule_id,
        cmd.idempotency_scope,
        "SCHEDULE_CREATED",
        {
            "schedule_id": schedule_id,
            "schedule_version_id": version_id,
            "version": 1,
            "snapshot_hash": st.snapshot_hash(content),
            "channel_id": str(channel_uuid),
        },
    )
    version = st.insert_version(
        ctx.session,
        schedule_id=schedule_id,
        version=1,
        content=content,
        created_by=actor,
        event_id=res.event_id,
        now=now,
        schedule_version_id=version_id,
    )
    st.update_schedule(
        ctx.session, schedule_id, now, current_version_id=version.id, last_event_id=res.event_id
    )
    _audit(
        ctx,
        "schedule.create",
        "schedule",
        schedule_id,
        version=1,
        snapshot_hash=version.snapshot_hash,
    )
    return _schedule_result(ctx, res, schedule_id)


@handles(CommitScheduleVersion)
def commit_version(cmd: CommitScheduleVersion, ctx: CommandContext) -> CommandResult:
    schedule = _load_schedule(ctx, cmd.schedule_id)
    current = _current_version(ctx, schedule)
    require_permission(
        ctx, "schedule.manage", action="api:schedule_update", channel_id=str(current.channel_id)
    )
    replay = _replay(ctx, "schedule", schedule.schedule_id, cmd.idempotency_scope)
    if replay is not None:
        return _schedule_result(ctx, replay, schedule.schedule_id)
    unknown = set(cmd.changes) - set(st.VERSION_CONTENT_FIELDS)
    if unknown:
        raise CommandError("SCHEDULE_FIELD_UNKNOWN", ", ".join(sorted(unknown)), status=400)
    if not cmd.changes:
        raise CommandError("SCHEDULE_NO_CHANGES", schedule.schedule_id, status=400)
    if schedule.status == "DISABLED":
        raise CommandError("SCHEDULE_STATUS_INVALID", "disabled schedules are frozen", status=409)
    merged = {**current.content(), **cmd.changes}
    content = _validated_content(ctx, schedule.schedule_id, current.version + 1, merged)
    if st.snapshot_hash(content) == current.snapshot_hash:
        raise CommandError("SCHEDULE_NO_CHANGES", "identical to the current version", status=400)
    now = ctx.clock.now()
    actor = uuid.UUID(ctx.principal.account_uuid)
    version_id = st.new_version_id()
    res = _append(
        ctx,
        "schedule",
        schedule.schedule_id,
        cmd.idempotency_scope,
        "SCHEDULE_UPDATED",
        {
            "schedule_id": schedule.schedule_id,
            "schedule_version_id": version_id,
            "version": current.version + 1,
            "snapshot_hash": st.snapshot_hash(content),
            "changed_fields": sorted(cmd.changes),
        },
    )
    version = st.insert_version(
        ctx.session,
        schedule_id=schedule.schedule_id,
        version=current.version + 1,
        content=content,
        created_by=actor,
        event_id=res.event_id,
        now=now,
        schedule_version_id=version_id,
    )
    cols: dict[str, Any] = {
        "current_version_id": version.id,
        "last_event_id": res.event_id,
        "name": content["name"],
    }
    if schedule.status == "ENABLED":
        cols["next_run_at"] = _next_run_at(version, now)
    st.update_schedule(ctx.session, schedule.schedule_id, now, **cols)
    _audit(
        ctx,
        "schedule.update",
        "schedule",
        schedule.schedule_id,
        version=version.version,
        changed_fields=sorted(cmd.changes),
        previous_version=current.version,
    )
    return _schedule_result(ctx, res, schedule.schedule_id)


def _transition(
    cmd: Command,
    ctx: CommandContext,
    schedule_id: str,
    target: sc.ScheduleStatus,
    event_type: str,
    action: str,
) -> CommandResult:
    schedule = _load_schedule(ctx, schedule_id)
    version = _current_version(ctx, schedule)
    require_permission(ctx, "schedule.manage", action=action, channel_id=str(version.channel_id))
    replay = _replay(ctx, "schedule", schedule.schedule_id, cmd.idempotency_scope)
    if replay is not None:
        return _schedule_result(ctx, replay, schedule.schedule_id)
    try:
        sc.schedule_transition(sc.ScheduleStatus(schedule.status), target)
    except ScheduleContractError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc
    now = ctx.clock.now()
    res = _append(
        ctx,
        "schedule",
        schedule.schedule_id,
        cmd.idempotency_scope,
        event_type,
        {"schedule_id": schedule.schedule_id, "from_status": schedule.status},
    )
    cols: dict[str, Any] = {"status": target.value, "last_event_id": res.event_id}
    cancelled: list[str] = []
    if target is sc.ScheduleStatus.ENABLED:
        # planning starts now: occurrences before enabling/resuming are never materialized
        cols["last_planned_until"] = now
        cols["next_run_at"] = _next_run_at(version, now)
    else:
        cols["next_run_at"] = None
        cancelled = _cancel_pending_runs(ctx, schedule.schedule_id, f"SCHEDULE_{target.value}")
    st.update_schedule(ctx.session, schedule.schedule_id, now, **cols)
    _audit(
        ctx,
        f"schedule.{target.value.lower()}",
        "schedule",
        schedule.schedule_id,
        from_status=schedule.status,
        cancelled_runs=cancelled,
    )
    return _schedule_result(ctx, res, schedule.schedule_id, cancelled_runs=cancelled)


@handles(EnableSchedule)
def enable_schedule(cmd: EnableSchedule, ctx: CommandContext) -> CommandResult:
    return _transition(
        cmd,
        ctx,
        cmd.schedule_id,
        sc.ScheduleStatus.ENABLED,
        "SCHEDULE_ENABLED",
        "api:schedule_enable",
    )


@handles(PauseSchedule)
def pause_schedule(cmd: PauseSchedule, ctx: CommandContext) -> CommandResult:
    return _transition(
        cmd, ctx, cmd.schedule_id, sc.ScheduleStatus.PAUSED, "SCHEDULE_PAUSED", "api:schedule_pause"
    )


@handles(ResumeSchedule)
def resume_schedule(cmd: ResumeSchedule, ctx: CommandContext) -> CommandResult:
    schedule = _load_schedule(ctx, cmd.schedule_id, lock=False)
    if schedule.status != "PAUSED":
        raise CommandError(
            "SCHEDULE_TRANSITION_INVALID", f"{schedule.status} -> ENABLED", status=409
        )
    return _transition(
        cmd,
        ctx,
        cmd.schedule_id,
        sc.ScheduleStatus.ENABLED,
        "SCHEDULE_RESUMED",
        "api:schedule_resume",
    )


@handles(DisableSchedule)
def disable_schedule(cmd: DisableSchedule, ctx: CommandContext) -> CommandResult:
    return _transition(
        cmd,
        ctx,
        cmd.schedule_id,
        sc.ScheduleStatus.DISABLED,
        "SCHEDULE_DISABLED",
        "api:schedule_disable",
    )


def _safe_key(value: str) -> str:
    return value.replace(":", "_")


@handles(RunScheduleNow)
def run_now(cmd: RunScheduleNow, ctx: CommandContext) -> CommandResult:
    schedule = _load_schedule(ctx, cmd.schedule_id)
    version = _current_version(ctx, schedule)
    require_permission(
        ctx, "schedule.run", action="api:schedule_run_now", channel_id=str(version.channel_id)
    )
    request_key = ctx.idempotency_key
    existing = st.find_run_by_request_key(ctx.session, schedule.schedule_id, "MANUAL", request_key)
    if existing is not None:
        replay = _replay(ctx, "schedule_run", existing.run_id, cmd.idempotency_scope)
        if replay is not None:
            return _run_result(ctx, replay, existing)
    if schedule.status not in ("ENABLED", "PAUSED"):
        raise CommandError(
            "SCHEDULE_STATUS_INVALID", f"run now is not allowed in {schedule.status}", status=409
        )
    now = ctx.clock.now()
    run = st.insert_run(
        ctx.session,
        workspace_id=_ws(ctx),
        schedule_id=schedule.schedule_id,
        version=version,
        run_kind="MANUAL",
        scheduled_for=now,
        idempotency_key=manual_idempotency_key(
            schedule.schedule_id, ctx.principal.account_id, _safe_key(cmd.client_key or request_key)
        ),
        status="DUE",
        now=now,
        request_key=request_key,
        requested_by=uuid.UUID(ctx.principal.account_uuid),
    )
    if run is None:  # same manual key by the same requester: the earlier Run stands
        existing = st.find_run_by_request_key(
            ctx.session, schedule.schedule_id, "MANUAL", request_key
        )
        if existing is None:
            raise CommandError("RUN_DUPLICATE", "manual run already requested", status=409)
        run = existing
    res = _append(
        ctx,
        "schedule_run",
        run.run_id,
        cmd.idempotency_scope,
        "RUN_DUE",
        {
            "run_id": run.run_id,
            "schedule_id": schedule.schedule_id,
            "schedule_version_id": version.schedule_version_id,
            "run_kind": "MANUAL",
            "scheduled_for": st.iso_ms(now),
        },
    )
    _audit(ctx, "schedule.run_now", "schedule_run", run.run_id, schedule_id=schedule.schedule_id)
    return _run_result(ctx, res, run)


@handles(CancelScheduleRun)
def cancel_run(cmd: CancelScheduleRun, ctx: CommandContext) -> CommandResult:
    run = st.load_run(ctx.session, cmd.run_id, workspace_id=_ws(ctx), for_update=True)
    if run is None:
        raise CommandError("RUN_NOT_FOUND", cmd.run_id, status=404)
    version = st.load_version(ctx.session, run.schedule_version_id)
    require_permission(
        ctx,
        "schedule.run",
        action="api:schedule_run_cancel",
        channel_id=None if version is None else str(version.channel_id),
    )
    replay = _replay(ctx, "schedule_run", run.run_id, cmd.idempotency_scope)
    if replay is not None:
        return _run_result(ctx, replay, run)
    try:
        target = sc.cancel_run(sc.RunStatus(run.status))
    except ScheduleContractError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc
    now = ctx.clock.now()
    if target is sc.RunStatus.CANCELLED:
        st.update_run(
            ctx.session,
            run.run_id,
            now,
            status="CANCELLED",
            cancelled_at=now,
            error_code=cmd.reason_code,
            finished_at=now,
        )
        res = _append(
            ctx,
            "schedule_run",
            run.run_id,
            cmd.idempotency_scope,
            "RUN_CANCELLED",
            {"run_id": run.run_id, "reason_code": cmd.reason_code},
        )
    else:
        st.update_run(
            ctx.session,
            run.run_id,
            now,
            status="CANCEL_REQUESTED",
            cancel_requested_at=now,
            error_code=cmd.reason_code,
        )
        res = _append(
            ctx,
            "schedule_run",
            run.run_id,
            cmd.idempotency_scope,
            "RUN_CANCEL_REQUESTED",
            {"run_id": run.run_id, "reason_code": cmd.reason_code},
        )
    _audit(
        ctx,
        "schedule.run_cancel",
        "schedule_run",
        run.run_id,
        from_status=run.status,
        to_status=target.value,
        reason_code=cmd.reason_code,
    )
    return _run_result(ctx, res, run)


@handles(RetryScheduleRun)
def retry_run(cmd: RetryScheduleRun, ctx: CommandContext) -> CommandResult:
    original = st.load_run(ctx.session, cmd.run_id, workspace_id=_ws(ctx), for_update=True)
    if original is None:
        raise CommandError("RUN_NOT_FOUND", cmd.run_id, status=404)
    version = st.load_version(ctx.session, original.schedule_version_id)
    require_permission(
        ctx,
        "schedule.run",
        action="api:schedule_run_retry",
        channel_id=None if version is None else str(version.channel_id),
    )
    existing = st.find_run_by_request_key(
        ctx.session, original.schedule_id, "RETRY", ctx.idempotency_key
    )
    if existing is not None:
        replay = _replay(ctx, "schedule_run", existing.run_id, cmd.idempotency_scope)
        if replay is not None:
            return _run_result(ctx, replay, existing)
    if original.status not in RETRYABLE_TERMINAL:
        code = (
            "RUN_RETRY_NOT_TERMINAL"
            if original.status not in sc.RUN_TERMINAL
            else "RUN_RETRY_NOT_ALLOWED"
        )
        raise CommandError(code, f"{original.status} cannot be retried", status=409)
    assert version is not None
    retry_no = int(
        ctx.session.execute(
            text("SELECT count(*) + 1 FROM schedule_runs WHERE retry_of_run_id = :r"),
            {"r": original.run_id},
        ).scalar_one()
    )
    now = ctx.clock.now()
    run = st.insert_run(
        ctx.session,
        workspace_id=_ws(ctx),
        schedule_id=original.schedule_id,
        version=version,
        run_kind="RETRY",
        scheduled_for=now,
        idempotency_key=retry_idempotency_key(original.run_id, retry_no),
        status="DUE",
        now=now,
        retry_of_run_id=original.run_id,
        request_key=ctx.idempotency_key,
        requested_by=uuid.UUID(ctx.principal.account_uuid),
    )
    if run is None:  # pragma: no cover - retry_no is unique per original
        raise CommandError("RUN_DUPLICATE", "retry already materialized", status=409)
    res = _append(
        ctx,
        "schedule_run",
        run.run_id,
        cmd.idempotency_scope,
        "RUN_DUE",
        {
            "run_id": run.run_id,
            "schedule_id": original.schedule_id,
            "schedule_version_id": version.schedule_version_id,
            "run_kind": "RETRY",
            "scheduled_for": st.iso_ms(now),
            "retry_of_run_id": original.run_id,
            "retry_no": retry_no,
        },
    )
    _audit(
        ctx,
        "schedule.run_retry",
        "schedule_run",
        run.run_id,
        retry_of_run_id=original.run_id,
        retry_no=retry_no,
    )
    return _run_result(ctx, res, run)


# ---------------------------------------------------------------------------------- reads
def _require_read(ctx: CommandContext, action: str, channel_id: str | None = None) -> None:
    require_permission(ctx, "schedule.read", action=action, channel_id=channel_id)


def schedule_view(ctx: CommandContext, schedule_id: str) -> dict[str, Any]:
    schedule = st.load_schedule(ctx.session, _ws(ctx), schedule_id)
    if schedule is None:
        raise CommandError("SCHEDULE_NOT_FOUND", schedule_id, status=404)
    version = (
        None
        if schedule.current_version_id is None
        else st.load_version(ctx.session, schedule.current_version_id)
    )
    runs = st.list_runs(ctx.session, schedule_id, limit=5)
    return {
        "schedule_id": schedule.schedule_id,
        "name": schedule.name,
        "status": schedule.status,
        "current_version": None if version is None else version.view(),
        "next_run_at": st.iso_ms(schedule.next_run_at),
        "last_planned_until": st.iso_ms(schedule.last_planned_until),
        "created_by": str(schedule.created_by),
        "created_at": st.iso_ms(schedule.created_at),
        "updated_at": st.iso_ms(schedule.updated_at),
        "recent_runs": [r.view() for r in runs],
    }


def get_schedule(ctx: CommandContext, schedule_id: str) -> dict[str, Any]:
    _require_read(ctx, "api:schedule_get")
    view = schedule_view(ctx, schedule_id)
    view["versions"] = [v.view() for v in st.list_versions(ctx.session, schedule_id)]
    view["planner_notes"] = st.planner_notes(ctx.session, schedule_id)
    return view


def list_schedules(ctx: CommandContext) -> list[dict[str, Any]]:
    _require_read(ctx, "api:schedule_list")
    return [schedule_view(ctx, s.schedule_id) for s in st.list_schedules(ctx.session, _ws(ctx))]


def preview_occurrences(
    cron_expression: str,
    timezone: str,
    after: dt.datetime,
    *,
    schedule_id: str = "preview",
    count: int = 10,
) -> list[dict[str, Any]]:
    try:
        cron.validate(cron_expression)
        occ = cron.next_occurrences(
            cron_expression, timezone, after, count=count, schedule_id=schedule_id
        )
    except cron.CronError as exc:
        raise _contract(exc) from exc
    return [
        {
            "local": o.local.strftime("%Y-%m-%dT%H:%M"),
            "utc": st.iso_ms(o.utc),
            "occurrence_key": o.occurrence_key,
            "reason": o.reason,
            "executable": o.executable,
        }
        for o in occ
    ]


def preview(
    ctx: CommandContext,
    *,
    schedule_id: str | None = None,
    cron_expression: str | None = None,
    timezone: str | None = None,
    after: dt.datetime | None = None,
    count: int = 10,
) -> dict[str, Any]:
    _require_read(ctx, "api:schedule_preview")
    since = after or ctx.clock.now()
    if schedule_id is not None:
        schedule = st.load_schedule(ctx.session, _ws(ctx), schedule_id)
        if schedule is None:
            raise CommandError("SCHEDULE_NOT_FOUND", schedule_id, status=404)
        version = _current_version(ctx, schedule)
        cron_expression, timezone = version.cron_expression, version.timezone
    if not cron_expression or not timezone:
        raise CommandError(
            "SCHEDULE_PREVIEW_INPUT", "cron_expression and timezone required", status=400
        )
    items = preview_occurrences(
        cron_expression, timezone, since, schedule_id=schedule_id or "preview", count=count
    )
    return {
        "cron_expression": " ".join(cron_expression.split()),
        "timezone": timezone,
        "after": st.iso_ms(since),
        "items": items,
    }


def run_view(ctx: CommandContext, run_id: str) -> dict[str, Any]:
    _require_read(ctx, "api:schedule_runs")
    run = st.load_run(ctx.session, run_id, workspace_id=_ws(ctx))
    if run is None:
        raise CommandError("RUN_NOT_FOUND", run_id, status=404)
    view = run.view()
    view["attempts"] = st.attempts_of(ctx.session, run_id)
    view["links"] = run_links(ctx, run)
    return view


def run_links(ctx: CommandContext, run: st.RunRow) -> dict[str, Any]:
    """Task / Artifact / Document / Verification links of a Run (development plan §10A.5)."""
    s = ctx.session
    artifacts = [
        str(r[0])
        for r in s.execute(
            text(
                "SELECT artifact_id FROM artifact_links WHERE subject_type = 'schedule_run' "
                "AND subject_id = :r ORDER BY linked_at"
            ),
            {"r": run.run_id},
        ).all()
    ]
    documents: list[str] = []
    verifications: list[str] = []
    if run.task_id:
        documents = [
            str(r[0])
            for r in s.execute(
                text("SELECT document_id FROM documents WHERE task_id = :t ORDER BY created_at"),
                {"t": run.task_id},
            ).all()
        ]
        verifications = [
            str(r[0])
            for r in s.execute(
                text(
                    "SELECT verification_id FROM verification_runs WHERE task_id "
                    "= :t ORDER BY created_at"
                ),
                {"t": run.task_id},
            ).all()
        ]
        artifacts += [
            str(r[0])
            for r in s.execute(
                text(
                    "SELECT artifact_id FROM artifact_links WHERE subject_type = 'task' "
                    "AND subject_id = :t ORDER BY linked_at"
                ),
                {"t": run.task_id},
            ).all()
        ]
    return {
        "task_id": run.task_id,
        "artifacts": sorted(set(artifacts)),
        "documents": documents,
        "verifications": verifications,
    }


def list_runs(
    ctx: CommandContext,
    schedule_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
    before: dt.datetime | None = None,
) -> dict[str, Any]:
    _require_read(ctx, "api:schedule_runs")
    if st.load_schedule(ctx.session, _ws(ctx), schedule_id) is None:
        raise CommandError("SCHEDULE_NOT_FOUND", schedule_id, status=404)
    limit = max(1, min(limit, 100))
    runs = st.list_runs(ctx.session, schedule_id, status=status, limit=limit, before=before)
    items = [r.view() for r in runs]
    next_before = st.iso_ms(runs[-1].scheduled_for) if len(runs) == limit else None
    return {"items": items, "next_before": next_before}


def run_history(ctx: CommandContext, schedule_id: str, *, limit: int = 50) -> dict[str, Any]:
    _require_read(ctx, "api:schedule_runs")
    if st.load_schedule(ctx.session, _ws(ctx), schedule_id) is None:
        raise CommandError("SCHEDULE_NOT_FOUND", schedule_id, status=404)
    runs = st.list_runs(ctx.session, schedule_id, limit=max(1, min(limit, 100)))
    items = []
    for r in runs:
        view = r.view()
        view["attempts"] = st.attempts_of(ctx.session, r.run_id)
        view["links"] = run_links(ctx, r)
        items.append(view)
    return {"items": items, "planner_notes": st.planner_notes(ctx.session, schedule_id)}
