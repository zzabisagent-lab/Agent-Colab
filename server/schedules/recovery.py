"""Timeout, cancel-window and retry recovery for Schedule Runs (§10A.2 steps 10-12; P5-06).

Three periodic duties on top of the execution package:

* ``handle_timeouts`` — a Run that exceeds its version's ``max_duration_seconds`` is asked to
  cancel; the Adapter must acknowledge within 10 s and clean up within 60 s, otherwise the Run
  ends ``TIMED_OUT`` (spec §8.6, development plan §10A.2 step 11).
* ``handle_cancel_windows`` — a Run in ``CANCEL_REQUESTED`` becomes ``CANCELLED`` once its Task is
  gone (cleanup confirmed) and ``TIMED_OUT`` when the cleanup window elapses.
* ``run_due_retries`` — transient failures scheduled by ``execution._transient`` are re-executed
  when their backoff elapses, preserving the Run's original ``scheduled_for``.

Missed-run *materialization* belongs to the planner (P5-03); this module only executes what the
planner created, so `SKIP`/`RUN_ONCE`/`BACKFILL_LIMITED` behaviour is observable end to end.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import text

from server.application.bus import CommandError
from server.domain import defaults
from server.schedules import budget as run_budget
from server.schedules import execution
from server.schedules.contract import RunStatus, ScheduleContractError

log = logging.getLogger(__name__)

CANCEL_ACK_S = 10
CANCEL_CLEANUP_S = 60


@dataclass(frozen=True)
class RecoveryReport:
    timed_out_requested: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()
    timed_out: tuple[str, ...] = ()
    retried: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return (
            len(self.timed_out_requested)
            + len(self.cancelled)
            + len(self.timed_out)
            + len(self.retried)
        )


def _task_open(ctx: execution.ExecutionContext, task_id: str | None) -> bool:
    """True while the Run's Task is still in a non-terminal state (cleanup not confirmed)."""
    if not task_id:
        return False
    row = ctx.session.execute(
        text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
    ).first()
    if row is None:
        return False
    return str(row[0]) not in ("COMPLETED", "CANCELLED", "FAILED", "VERIFIED")


def handle_timeouts(ctx: execution.ExecutionContext) -> tuple[str, ...]:
    """Ask Runs past ``max_duration_seconds`` to cancel (step 11)."""
    now = ctx.now
    requested: list[str] = []
    running = ctx.store.runs_by_status(
        ctx.session,
        ctx.workspace_id,
        [RunStatus.TASK_CREATED.value, RunStatus.RUNNING.value, RunStatus.VERIFYING.value],
    )
    for run in running:
        version = ctx.store.load_version(ctx.session, run.schedule_version_id)
        started = run.started_at or run.scheduled_for
        if (now - started).total_seconds() <= version.max_duration_seconds:
            continue
        execution.request_cancel(ctx, run, "MAX_DURATION_EXCEEDED")
        run_budget.raise_alert(
            ctx.session,
            workspace_id=ctx.workspace_id,
            kind="timeout",
            schedule_id=run.schedule_id,
            run_id=run.run_id,
            detail={"max_duration_seconds": version.max_duration_seconds},
            now=now,
        )
        requested.append(run.run_id)
    return tuple(requested)


def handle_cancel_windows(
    ctx: execution.ExecutionContext,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Close CANCEL_REQUESTED Runs: CANCELLED on confirmed cleanup, TIMED_OUT after 60 s."""
    now = ctx.now
    cancelled: list[str] = []
    timed_out: list[str] = []
    for run in ctx.store.runs_by_status(
        ctx.session, ctx.workspace_id, [RunStatus.CANCEL_REQUESTED.value]
    ):
        requested_at = run.cancel_requested_at or now
        elapsed = (now - requested_at).total_seconds()
        try:
            if not _task_open(ctx, run.task_id):  # ack + cleanup confirmed
                execution.finish_cancel(
                    ctx, run, timed_out=False, reason=run.error_code or "CANCELLED"
                )
                cancelled.append(run.run_id)
                continue
            if elapsed <= CANCEL_ACK_S + CANCEL_CLEANUP_S:
                continue
            execution.finish_cancel(ctx, run, timed_out=True, reason="CANCEL_CLEANUP_TIMEOUT")
        except (ScheduleContractError, CommandError) as exc:
            log.warning("cancel window of %s skipped: %s", run.run_id, exc)
            continue
        run_budget.raise_alert(
            ctx.session,
            workspace_id=ctx.workspace_id,
            kind="cancel_timeout",
            schedule_id=run.schedule_id,
            run_id=run.run_id,
            detail={"elapsed_s": int(elapsed), "cleanup_s": CANCEL_CLEANUP_S},
            now=now,
        )
        timed_out.append(run.run_id)
    return tuple(cancelled), tuple(timed_out)


def run_due_retries(ctx: execution.ExecutionContext, limit: int = 50) -> tuple[str, ...]:
    """Re-execute Runs whose transient backoff elapsed (§10A.2 step 10)."""
    retried: list[str] = []
    for run_id in execution.due_retries(ctx.session, ctx.now, limit):
        run = ctx.store.load_run(ctx.session, run_id, for_update=True)
        if run.status not in (RunStatus.CLAIMED.value, RunStatus.DUE.value):
            execution.clear_retry(ctx.session, run_id)
            continue
        execution.clear_retry(ctx.session, run_id)
        if run.status == RunStatus.DUE.value:  # the lease expired meanwhile: re-claim next tick
            continue
        try:
            execution.execute(run, ctx)
        except (ScheduleContractError, CommandError) as exc:
            log.warning("retry of %s skipped: %s", run_id, exc)
            continue
        retried.append(run_id)
    return tuple(retried)


def recover(ctx: execution.ExecutionContext) -> RecoveryReport:
    """One recovery pass: timeouts → cancel windows → due retries."""
    requested = handle_timeouts(ctx)
    cancelled, timed_out = handle_cancel_windows(ctx)
    retried = run_due_retries(ctx)
    return RecoveryReport(requested, cancelled, timed_out, retried)


def replace_cancel_window_s() -> int:
    return defaults.SCHEDULE_REPLACE_CANCEL_TIMEOUT_S


def stuck_leases(ctx: execution.ExecutionContext, older_than: dt.timedelta) -> list[str]:
    """Claimed Runs whose lease expired more than ``older_than`` ago (metrics/alerts input)."""
    rows = ctx.session.execute(
        text(
            "SELECT run_id FROM schedule_runs WHERE workspace_id = CAST(:w AS uuid) "
            "AND status = 'CLAIMED' AND lease_expires_at IS NOT NULL AND lease_expires_at < :cut "
            "ORDER BY lease_expires_at"
        ),
        {"w": ctx.workspace_id, "cut": ctx.now - older_than},
    ).all()
    return [str(r[0]) for r in rows]
