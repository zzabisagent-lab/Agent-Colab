"""Scheduler tick and Run-execution glue (development plan §10A.2; P5-04..P5-07, P5-10).

``tick`` is the periodic duty the gateway maintenance loop calls: recover expired leases,
materialize due occurrences (planner), claim Runs (runner) and execute each of them (execution),
then run the timeout/cancel/retry recovery. Maintenance mode pauses claiming: no due Run is
claimed while the instance is in maintenance (development plan §4.6, V-P4-32), while notices and
outbox drains continue.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from server.application import tasks as task_cmds
from server.db.engine import session_scope
from server.domain import defaults
from server.identity.principals import Principal
from server.maintenance import mode as maintenance_mode
from server.schedules import execution, recovery
from server.schedules.execution import ExecutionContext, RunOutcome

log = logging.getLogger(__name__)

TERMINAL_TASK_STATES = frozenset({"COMPLETED", "CANCELLED", "FAILED", "VERIFIED"})


@dataclass(frozen=True)
class TickReport:
    claimed: int = 0
    executed: tuple[RunOutcome, ...] = ()
    recovery: recovery.RecoveryReport = field(default_factory=recovery.RecoveryReport)
    paused: bool = False

    @property
    def started(self) -> int:
        return sum(1 for o in self.executed if o.task_id)

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.executed if o.status == "SKIPPED")


def system_principal(session: Session, workspace_id: str) -> Principal:
    """The Workspace's system service Account (deterministic: lowest service account_id)."""
    from sqlalchemy import text

    row = session.execute(
        text(
            "SELECT id, account_id FROM accounts WHERE workspace_id = :w "
            "AND account_type = 'service' ORDER BY account_id LIMIT 1"
        ),
        {"w": uuid.UUID(workspace_id)},
    ).first()
    if row is None:
        from server.application.bus import CommandError

        raise CommandError("SYSTEM_ACCOUNT_MISSING", "no service Account", status=409)
    return Principal(str(row[1]), str(row[0]), "service", f"system:{row[1]}")


def build_context(
    session: Session,
    runtime: Any,
    *,
    workspace_id: str,
    runner_id: str,
    actor: Principal | None = None,
    extras: dict[str, Any] | None = None,
) -> ExecutionContext:
    """An ExecutionContext bound to the core package's database adapters."""
    from server.schedules.run_access import DbRunStore

    return ExecutionContext(
        session=session,
        store=DbRunStore(runtime.clock),
        event_store=runtime.store_for(session),
        clock=runtime.clock,
        workspace_id=workspace_id,
        runner_id=runner_id,
        actor=actor or system_principal(session, workspace_id),
        authorizer=runtime.authorizer,
        extras=dict(extras or {}),
    )


def tick(
    runtime: Any,
    *,
    workspace_id: str,
    runner_id: str = "scheduler-1",
    limit: int = 10,
    horizon_s: int | None = None,
    session: Session | None = None,
) -> TickReport:
    """One scheduler tick for one Workspace (its own transaction unless one is supplied)."""
    if session is not None:
        return _tick(session, runtime, workspace_id, runner_id, limit, horizon_s)
    with session_scope(runtime.session_factory) as own:
        return _tick(own, runtime, workspace_id, runner_id, limit, horizon_s)


def _tick(
    session: Session,
    runtime: Any,
    workspace_id: str,
    runner_id: str,
    limit: int,
    horizon_s: int | None,
) -> TickReport:
    from server.schedules import runner as core_runner

    ctx = build_context(session, runtime, workspace_id=workspace_id, runner_id=runner_id)
    if maintenance_mode.scheduler_paused(session):
        # V-P4-32: zero due Run claims in maintenance mode; recovery still closes open windows
        return TickReport(0, (), recovery.recover(ctx), paused=True)
    claimed = core_runner.tick(
        session,
        store=ctx.event_store,
        clock=runtime.clock,
        workspace_id=workspace_id,
        runner_id=runner_id,
        actor_account_id=ctx.actor.account_uuid,
        horizon_s=horizon_s,
        lease_s=defaults.SCHEDULER_CLAIM_LEASE_S,
        limit=limit,
    )
    outcomes: list[RunOutcome] = []
    for run in claimed:
        try:
            outcomes.append(execution.execute(run, ctx))
        except Exception:  # one bad Run must not stop the tick
            log.exception("schedule run %s failed to execute", getattr(run, "run_id", "?"))
    report = recovery.recover(ctx)
    return TickReport(len(claimed), tuple(outcomes), report)


def cancel_run(
    runtime: Any, *, workspace_id: str, run_id: str, reason_code: str = "CANCELLED_BY_ADMIN"
) -> str:
    """Cancel one Run (pending → CANCELLED, running → CANCEL_REQUESTED). Returns the new status."""
    with session_scope(runtime.session_factory) as session:
        ctx = build_context(session, runtime, workspace_id=workspace_id, runner_id="api")
        run = ctx.store.load_run(session, run_id, for_update=True)
        return execution.request_cancel(ctx, run, reason_code).status


def finish_cancellations(
    runtime: Any, *, workspace_id: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Close CANCEL_REQUESTED Runs whose ack/cleanup window resolved (used by the API and tick)."""
    with session_scope(runtime.session_factory) as session:
        ctx = build_context(session, runtime, workspace_id=workspace_id, runner_id="api")
        return recovery.handle_cancel_windows(ctx)


# ------------------------------------------------------------------ Task terminal hook


def _terminal_hook(ctx: Any, task_id: str) -> None:
    """Close the Schedule Run of a Task that reached a terminal state (§10A.2 step 7)."""
    from sqlalchemy import text

    row = ctx.session.execute(
        text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
    ).first()
    if row is None or str(row[0]) not in TERMINAL_TASK_STATES:
        return
    linked = ctx.session.execute(
        text("SELECT 1 FROM schedule_runs WHERE task_id = :t LIMIT 1"), {"t": task_id}
    ).first()
    if linked is None:  # an ordinary Task: nothing to close
        return
    from server.schedules.run_access import DbRunStore

    exec_ctx = ExecutionContext(
        session=ctx.session,
        store=DbRunStore(ctx.clock),
        event_store=ctx.store,
        clock=ctx.clock,
        workspace_id=ctx.workspace_id,
        runner_id="task-hook",
        actor=system_principal(ctx.session, ctx.workspace_id),
        authorizer=ctx.authorizer,
    )
    execution.on_task_terminal(exec_ctx, task_id, str(row[0]))


def register_hooks() -> None:
    """Register the Task terminal hook once (idempotent; called from ``create_app``)."""
    if _terminal_hook not in task_cmds.TERMINAL_HOOKS:
        task_cmds.register_terminal_hook(_terminal_hook)
