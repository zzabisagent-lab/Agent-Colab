"""Schedule and ScheduleRun state contract (spec §8.6, development plan §6.6, §10A.2).

Pure functions and tables only: transition validity, cancel rules, run-kind invariants,
concurrency, missed-run materialization, retry/backoff. Stable error codes everywhere.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum

from server.domain import defaults


class ScheduleContractError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ScheduleStatus(StrEnum):
    DRAFT = "DRAFT"
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    DUE = "DUE"
    CLAIMED = "CLAIMED"
    TASK_CREATED = "TASK_CREATED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    TIMED_OUT = "TIMED_OUT"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class RunKind(StrEnum):
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"
    RETRY = "RETRY"


class ConcurrencyPolicy(StrEnum):
    FORBID = "FORBID"
    ALLOW = "ALLOW"
    REPLACE = "REPLACE"


class MissedRunPolicy(StrEnum):
    SKIP = "SKIP"
    RUN_ONCE = "RUN_ONCE"
    BACKFILL_LIMITED = "BACKFILL_LIMITED"


class SkipCode(StrEnum):
    SKIPPED_CONCURRENCY = "SKIPPED_CONCURRENCY"
    SKIPPED_REPLACE_CANCEL_TIMEOUT = "SKIPPED_REPLACE_CANCEL_TIMEOUT"
    SKIPPED_POLICY = "SKIPPED_POLICY"
    SKIPPED_AGENT_UNAVAILABLE = "SKIPPED_AGENT_UNAVAILABLE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


DEFAULT_CONCURRENCY = ConcurrencyPolicy.FORBID
DEFAULT_MISSED_RUN = MissedRunPolicy.RUN_ONCE

SCHEDULE_TRANSITIONS: dict[ScheduleStatus, frozenset[ScheduleStatus]] = {
    ScheduleStatus.DRAFT: frozenset({ScheduleStatus.ENABLED, ScheduleStatus.DISABLED}),
    ScheduleStatus.ENABLED: frozenset({ScheduleStatus.PAUSED, ScheduleStatus.DISABLED}),
    ScheduleStatus.PAUSED: frozenset({ScheduleStatus.ENABLED, ScheduleStatus.DISABLED}),
    ScheduleStatus.DISABLED: frozenset(),
}

RUN_TERMINAL: frozenset[RunStatus] = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.SKIPPED,
        RunStatus.TIMED_OUT,
        RunStatus.CANCELLED,
    }
)
RUN_PENDING: frozenset[RunStatus] = frozenset({RunStatus.PENDING, RunStatus.DUE})
RUN_RUNNING: frozenset[RunStatus] = frozenset(
    {RunStatus.CLAIMED, RunStatus.TASK_CREATED, RunStatus.RUNNING, RunStatus.VERIFYING}
)

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.DUE, RunStatus.CANCELLED, RunStatus.SKIPPED}),
    RunStatus.DUE: frozenset({RunStatus.CLAIMED, RunStatus.CANCELLED, RunStatus.SKIPPED}),
    RunStatus.CLAIMED: frozenset(
        {
            RunStatus.TASK_CREATED,
            RunStatus.SKIPPED,
            RunStatus.FAILED,
            RunStatus.CANCEL_REQUESTED,
            RunStatus.DUE,  # lease expiry recovery: the claim is released
        }
    ),
    RunStatus.TASK_CREATED: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCEL_REQUESTED, RunStatus.TIMED_OUT}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.VERIFYING,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.TIMED_OUT,
            RunStatus.CANCEL_REQUESTED,
        }
    ),
    RunStatus.VERIFYING: frozenset(
        {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMED_OUT, RunStatus.CANCEL_REQUESTED}
    ),
    RunStatus.CANCEL_REQUESTED: frozenset({RunStatus.CANCELLED, RunStatus.TIMED_OUT}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.SKIPPED: frozenset(),
    RunStatus.TIMED_OUT: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def schedule_transition(current: ScheduleStatus, target: ScheduleStatus) -> ScheduleStatus:
    if target not in SCHEDULE_TRANSITIONS[current]:
        raise ScheduleContractError("SCHEDULE_TRANSITION_INVALID", f"{current} -> {target}")
    return target


def run_transition(current: RunStatus, target: RunStatus) -> RunStatus:
    if current in RUN_TERMINAL:
        raise ScheduleContractError("RUN_TERMINAL_CONFLICT", f"{current} is terminal")
    if target not in RUN_TRANSITIONS[current]:
        raise ScheduleContractError("RUN_TRANSITION_INVALID", f"{current} -> {target}")
    return target


def cancel_run(current: RunStatus) -> RunStatus:
    """Pending Runs cancel immediately; running Runs enter CANCEL_REQUESTED; terminal = conflict."""
    if current in RUN_TERMINAL:
        raise ScheduleContractError("RUN_TERMINAL_CONFLICT", f"cannot cancel {current}")
    if current in RUN_PENDING:
        return RunStatus.CANCELLED
    if current in RUN_RUNNING:
        return RunStatus.CANCEL_REQUESTED
    if current is RunStatus.CANCEL_REQUESTED:
        raise ScheduleContractError("RUN_CANCEL_ALREADY_REQUESTED", "cancel already requested")
    raise ScheduleContractError(
        "RUN_TRANSITION_INVALID", f"cancel from {current}"
    )  # pragma: no cover


def check_run_kind(
    run_kind: RunKind, occurrence_key: str | None, retry_of_run_id: str | None
) -> None:
    if run_kind is RunKind.SCHEDULED:
        if not occurrence_key:
            raise ScheduleContractError("RUN_KIND_INVARIANT", "SCHEDULED requires occurrence_key")
        if retry_of_run_id is not None:
            raise ScheduleContractError(
                "RUN_KIND_INVARIANT", "SCHEDULED cannot have retry_of_run_id"
            )
        return
    if occurrence_key is not None:
        raise ScheduleContractError(
            "RUN_KIND_INVARIANT", f"{run_kind} must have NULL occurrence_key"
        )
    if run_kind is RunKind.RETRY and not retry_of_run_id:
        raise ScheduleContractError("RUN_KIND_INVARIANT", "RETRY requires retry_of_run_id")
    if run_kind is RunKind.MANUAL and retry_of_run_id is not None:
        raise ScheduleContractError("RUN_KIND_INVARIANT", "MANUAL cannot have retry_of_run_id")


class ConcurrencyDecision(StrEnum):
    START = "START"
    SKIP = "SKIP"
    REPLACE_CANCEL_EXISTING = "REPLACE_CANCEL_EXISTING"


@dataclass(frozen=True)
class ConcurrencyOutcome:
    decision: ConcurrencyDecision
    error_code: str | None = None


def decide_concurrency(
    policy: ConcurrencyPolicy,
    previous_run_active: bool,
    replace_cancel_confirmed: bool | None = None,
) -> ConcurrencyOutcome:
    """Decide whether a new Run may start while a previous Run of the same Schedule is active.

    For REPLACE, ``replace_cancel_confirmed`` is ``None`` before the cancel was attempted (the
    caller must cancel the existing Run), ``True`` once cleanup was confirmed within
    ``SCHEDULE_REPLACE_CANCEL_TIMEOUT_S``, and ``False`` when the timeout elapsed.
    """
    if not previous_run_active:
        return ConcurrencyOutcome(ConcurrencyDecision.START)
    if policy is ConcurrencyPolicy.ALLOW:
        return ConcurrencyOutcome(ConcurrencyDecision.START)
    if policy is ConcurrencyPolicy.FORBID:
        return ConcurrencyOutcome(ConcurrencyDecision.SKIP, SkipCode.SKIPPED_CONCURRENCY)
    if replace_cancel_confirmed is None:
        return ConcurrencyOutcome(ConcurrencyDecision.REPLACE_CANCEL_EXISTING)
    if replace_cancel_confirmed:
        return ConcurrencyOutcome(ConcurrencyDecision.START)
    return ConcurrencyOutcome(ConcurrencyDecision.SKIP, SkipCode.SKIPPED_REPLACE_CANCEL_TIMEOUT)


@dataclass(frozen=True)
class MissedOccurrence:
    occurrence_key: str
    scheduled_for: dt.datetime  # original UTC instant, preserved on the created Run


@dataclass(frozen=True)
class MissedRunPlan:
    to_create: tuple[MissedOccurrence, ...]
    skipped: tuple[MissedOccurrence, ...]
    warning: str | None = None


def plan_missed_runs(
    policy: MissedRunPolicy,
    missed: list[MissedOccurrence],
    now_utc: dt.datetime,
    backfill_window_seconds: int = 0,
    backfill_limit: int = 0,
) -> MissedRunPlan:
    """Materialize missed occurrences after a restart (spec §8.6, §10A.2 step 9)."""
    ordered = sorted(missed, key=lambda m: m.scheduled_for)
    if not ordered or policy is MissedRunPolicy.SKIP:
        return MissedRunPlan((), tuple(ordered))
    if policy is MissedRunPolicy.RUN_ONCE:
        return MissedRunPlan((ordered[-1],), tuple(ordered[:-1]))
    if backfill_window_seconds < 0 or backfill_limit < 0:
        raise ScheduleContractError("BACKFILL_INVALID", "window and limit must be >= 0")
    window_start = now_utc - dt.timedelta(seconds=backfill_window_seconds)
    in_window = [m for m in ordered if m.scheduled_for >= window_start]
    outside = [m for m in ordered if m.scheduled_for < window_start]
    chosen = in_window[:backfill_limit]
    dropped = in_window[backfill_limit:]
    warning = None
    if outside or dropped:
        warning = (
            f"BACKFILL_TRUNCATED: {len(outside)} outside window, {len(dropped)} beyond limit "
            f"{backfill_limit}; created {len(chosen)} of {len(ordered)} missed occurrences"
        )
    return MissedRunPlan(tuple(chosen), tuple(outside + dropped), warning)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = defaults.SCHEDULE_RETRY_MAX_ATTEMPTS
    backoff_seconds: tuple[int, ...] = defaults.SCHEDULE_RETRY_BACKOFF_S
    jitter_ratio: float = defaults.SCHEDULE_RETRY_JITTER_MAX_RATIO
    transient_error_codes: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"TRANSIENT", "TIMEOUT_TRANSIENT", "PROVIDER_UNAVAILABLE"}
        )
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_attempts > defaults.SCHEDULE_RETRY_MAX_ATTEMPTS:
            raise ScheduleContractError(
                "RETRY_POLICY_INVALID",
                f"max_attempts must be 1..{defaults.SCHEDULE_RETRY_MAX_ATTEMPTS}",
            )
        if not 0 <= self.jitter_ratio <= defaults.SCHEDULE_RETRY_JITTER_MAX_RATIO:
            raise ScheduleContractError("RETRY_POLICY_INVALID", "jitter_ratio outside 0..0.2")


class RetryDecision(StrEnum):
    RETRY = "RETRY"
    FAIL_PERMANENT = "FAIL_PERMANENT"
    FAIL_EXHAUSTED = "FAIL_EXHAUSTED"


@dataclass(frozen=True)
class RetryOutcome:
    decision: RetryDecision
    next_attempt_no: int | None = None
    delay_min_s: float | None = None
    delay_max_s: float | None = None


def decide_retry(policy: RetryPolicy, attempt_no: int, error_code: str) -> RetryOutcome:
    """Attempt ``attempt_no`` (1-based) failed with ``error_code``; decide the next step."""
    if error_code not in policy.transient_error_codes:
        return RetryOutcome(RetryDecision.FAIL_PERMANENT)
    if attempt_no >= policy.max_attempts:
        return RetryOutcome(RetryDecision.FAIL_EXHAUSTED)
    base = policy.backoff_seconds[min(attempt_no - 1, len(policy.backoff_seconds) - 1)]
    return RetryOutcome(
        RetryDecision.RETRY,
        next_attempt_no=attempt_no + 1,
        delay_min_s=float(base),
        delay_max_s=base * (1 + policy.jitter_ratio),
    )


def replace_cancel_confirmed(requested_at: dt.datetime, confirmed_at: dt.datetime | None) -> bool:
    """True when the existing Run's cancel/cleanup was confirmed within the REPLACE timeout."""
    if confirmed_at is None:
        return False
    return (
        confirmed_at - requested_at
    ).total_seconds() <= defaults.SCHEDULE_REPLACE_CANCEL_TIMEOUT_S
