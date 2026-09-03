"""Standalone scheduler worker process (development plan §10A.1, §10A.2 steps 3-8; P5-03).

``python -m server.schedules.worker --workspace <workspace uuid> --runner-id <id>`` runs the
scheduler duty of one Workspace in its own OS process, so several workers can share the load and
survive each other's death. Unlike the in-process maintenance tick, the worker commits the **claim
phase separately from execution**: the DB lease becomes visible to peer workers immediately, and a
Run whose worker dies mid-execution is recovered by lease expiry rather than staying invisible
inside one long transaction.

One JSON line is printed per tick (``{"tick": n, "claimed": [...], "executed": [...]}``) so an
operator (or a test) can follow progress; SIGTERM/SIGINT stop the loop after the current tick.

Settings (development plan §10A.1 bounds are enforced by
:func:`server.schedules.runner.validate_scheduler_settings`):

===================================== =========================================================
``AGENT_COLAB_SCHEDULER_POLL_S``      poll interval, 5-60 s (default 15)
``AGENT_COLAB_SCHEDULER_LEASE_S``     claim lease, at least 3x the poll interval (default 60)
``AGENT_COLAB_SCHEDULER_LIMIT``       Runs claimed per tick (default 10)
``AGENT_COLAB_SCHEDULE_KILL_AFTER``   **test seam**, see below
===================================== =========================================================

``AGENT_COLAB_SCHEDULE_KILL_AFTER`` is a failpoint used only by the crash/recovery tests
(V-P5-08, V-P5-24): set to ``claimed`` or ``task_created`` it makes the worker terminate with
``os._exit`` at exactly that committed boundary, which is indistinguishable from ``SIGKILL`` for
the database. It is inert when the variable is unset, and :func:`_maybe_kill` is the only place
that reads it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

from server.application.schedule_runs import build_context
from server.db.engine import make_engine, make_session_factory, session_scope
from server.domain import defaults
from server.maintenance import mode as maintenance_mode
from server.schedules import execution, recovery
from server.schedules import runner as core_runner

log = logging.getLogger(__name__)

KILL_STAGES = ("claimed", "task_created")
EXIT_KILLED = 137  # what SIGKILL reports through a shell, reused for the failpoint


def _maybe_kill(stage: str) -> None:
    """Test seam: terminate the process at ``stage`` when the failpoint names it.

    A no-op unless ``AGENT_COLAB_SCHEDULE_KILL_AFTER`` is set. ``os._exit`` skips every cleanup
    hook, so the effect on committed and uncommitted database state matches ``SIGKILL``.
    """
    if os.environ.get("AGENT_COLAB_SCHEDULE_KILL_AFTER") != stage:
        return
    sys.stdout.write(json.dumps({"killed_after": stage}) + "\n")
    sys.stdout.flush()
    os._exit(EXIT_KILLED)


@dataclass
class WorkerSettings:
    workspace_id: str
    runner_id: str
    poll_s: int = defaults.SCHEDULER_POLL_S
    lease_s: int = defaults.SCHEDULER_CLAIM_LEASE_S
    limit: int = 10
    horizon_s: int | None = None
    once: bool = False
    max_ticks: int | None = None
    start_delay_s: float = 0.0  # stagger peer workers so they do not all poll on the same beat

    @classmethod
    def from_env(cls, workspace_id: str, runner_id: str, **over: Any) -> WorkerSettings:
        poll = int(os.environ.get("AGENT_COLAB_SCHEDULER_POLL_S", defaults.SCHEDULER_POLL_S))
        lease = int(
            os.environ.get("AGENT_COLAB_SCHEDULER_LEASE_S", defaults.SCHEDULER_CLAIM_LEASE_S)
        )
        limit = int(os.environ.get("AGENT_COLAB_SCHEDULER_LIMIT", "10"))
        core_runner.validate_scheduler_settings(poll, lease)
        return cls(workspace_id, runner_id, poll_s=poll, lease_s=lease, limit=limit, **over)


@dataclass
class TickLine:
    """What one worker tick did, printed as one JSON line."""

    tick: int
    claimed: list[str] = field(default_factory=list)
    executed: list[dict[str, Any]] = field(default_factory=list)
    paused: bool = False

    def emit(self) -> None:
        sys.stdout.write(json.dumps(self.__dict__, sort_keys=True) + "\n")
        sys.stdout.flush()


def claim_phase(runtime: Any, settings: WorkerSettings) -> tuple[list[str], bool]:
    """Expire leases, materialize occurrences and claim Runs in **one committed transaction**.

    Returns the claimed Run ids and whether maintenance mode paused claiming.
    """
    with session_scope(runtime.session_factory) as session:
        ctx = build_context(
            session, runtime, workspace_id=settings.workspace_id, runner_id=settings.runner_id
        )
        if maintenance_mode.scheduler_paused(session):
            recovery.recover(ctx)  # V-P4-32: no claims in maintenance mode, windows still close
            return [], True
        claimed = core_runner.tick(
            session,
            store=ctx.event_store,
            clock=runtime.clock,
            workspace_id=settings.workspace_id,
            runner_id=settings.runner_id,
            actor_account_id=ctx.actor.account_uuid,
            horizon_s=settings.horizon_s,
            lease_s=settings.lease_s,
            limit=settings.limit,
        )
        return [r.run_id for r in claimed], False


def execute_run(runtime: Any, settings: WorkerSettings, run_id: str) -> dict[str, Any]:
    """Execute one claimed Run in its own transaction; the Task commits with the Run's status."""
    with session_scope(runtime.session_factory) as session:
        ctx = build_context(
            session, runtime, workspace_id=settings.workspace_id, runner_id=settings.runner_id
        )
        run = ctx.store.load_run(session, run_id, for_update=True)
        outcome = execution.execute(run, ctx)
        return {
            "run_id": outcome.run_id,
            "status": outcome.status,
            "task_id": outcome.task_id,
            "error_code": outcome.error_code,
        }


def recovery_phase(runtime: Any, settings: WorkerSettings) -> None:
    """Timeouts, cancel windows and due retries (own transaction, never blocks claiming)."""
    with session_scope(runtime.session_factory) as session:
        ctx = build_context(
            session, runtime, workspace_id=settings.workspace_id, runner_id=settings.runner_id
        )
        recovery.recover(ctx)


def run_tick(runtime: Any, settings: WorkerSettings, tick_no: int) -> TickLine:
    """One full worker tick: claim (committed), execute each Run, then recover."""
    line = TickLine(tick_no)
    claimed, line.paused = claim_phase(runtime, settings)
    line.claimed = list(claimed)
    if claimed:
        _maybe_kill("claimed")  # the claim and its lease are committed and visible to peers
    for run_id in claimed:
        try:
            result = execute_run(runtime, settings, run_id)
        except Exception:  # one bad Run must never stop the worker
            log.exception("schedule run %s failed to execute", run_id)
            continue
        line.executed.append(result)
        if result.get("task_id"):
            _maybe_kill("task_created")
    recovery_phase(runtime, settings)
    return line


def build_runtime(database_url: str) -> Any:
    from server.api.dispatch import default_runtime
    from server.config import Settings

    settings = Settings(database_url=database_url)
    return default_runtime(make_session_factory(make_engine(database_url)), settings)


def serve(runtime: Any, settings: WorkerSettings) -> int:
    """Run the worker loop until SIGTERM/SIGINT, ``--once``, or ``max_ticks``."""
    stopping = {"now": False}

    def _stop(_signum: int, _frame: FrameType | None) -> None:
        stopping["now"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)
    if settings.start_delay_s:
        time.sleep(settings.start_delay_s)
    tick_no = 0
    while not stopping["now"]:
        tick_no += 1
        run_tick(runtime, settings, tick_no).emit()
        if settings.once or (settings.max_ticks is not None and tick_no >= settings.max_ticks):
            break
        for _ in range(settings.poll_s * 10):  # wake promptly on a signal
            if stopping["now"]:
                break
            time.sleep(0.1)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent-Colab scheduler worker")
    parser.add_argument("--workspace", required=True, help="workspace uuid")
    parser.add_argument("--runner-id", required=True, help="unique id of this worker")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--max-ticks", type=int, default=None)
    parser.add_argument("--start-delay-s", type=float, default=0.0)
    parser.add_argument("--database-url", default=os.environ.get("AGENT_COLAB_DATABASE_URL"))
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or AGENT_COLAB_DATABASE_URL is required")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    settings = WorkerSettings.from_env(
        args.workspace,
        args.runner_id,
        once=args.once,
        max_ticks=args.max_ticks,
        start_delay_s=args.start_delay_s,
    )
    return serve(build_runtime(args.database_url), settings)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
