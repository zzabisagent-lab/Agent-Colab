"""Periodic Phase 3 maintenance (development plan §7B.1, §7D.2, §7.3 runtime norms).

Runs from the channel gateway loop every ``AGENT_COLAB_MAINTENANCE_INTERVAL_S`` seconds (default
30) per Workspace, each step in its own transaction so one failure never blocks the others:

1. work-item timeouts (ACK 60 s / accept 120 s / deadlines) → §7D.3 re-routing;
2. verifier offers silent for 10 minutes → next candidate or WAITING;
3. Agents without heartbeats (3 misses or 90 s) → offline (+ re-routing of their Tasks).

The actor is the Workspace's system service Account, which must hold ``task.reassign``,
``task.progress``, ``verification.assign`` and ``agent.manage``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.agents import rerouting
from server.application import agents as agent_cmds
from server.application import bus
from server.db.engine import session_scope
from server.verification import assignment
from server.work import timeouts

log = logging.getLogger(__name__)


def workspace_ids(session: Session) -> list[str]:
    return [str(r[0]) for r in session.execute(text("SELECT id FROM workspaces ORDER BY id")).all()]


def _system_ctx(runtime: Any, session: Session, workspace_id: str, key: str) -> bus.CommandContext:
    principal = rerouting.system_principal(session, workspace_id)
    return bus.CommandContext(
        session=session,
        store=runtime.store_for(session),
        authorizer=runtime.authorizer,
        clock=runtime.clock,
        principal=principal,
        workspace_id=workspace_id,
        correlation_id=f"maint-{key}",
        idempotency_key=f"maint-{key}-{uuid.uuid4().hex[:12]}",
    )


def run_maintenance(runtime: Any) -> dict[str, int]:
    """One maintenance pass over every Workspace; returns counters for logging/tests."""
    counters = {
        "rerouted": 0,
        "verifier_timeouts": 0,
        "marked_offline": 0,
        "breakglass_expired": 0,
        "errors": 0,
    }
    with session_scope(runtime.session_factory) as session:
        workspaces = workspace_ids(session)
    for ws in workspaces:
        for step in (_sweep_work_items, _sweep_verifier_offers, _sweep_offline, _sweep_breakglass):
            try:
                with session_scope(runtime.session_factory) as session:
                    step(runtime, session, ws, counters)
            except bus.CommandError as exc:
                if exc.code != "SYSTEM_ACCOUNT_MISSING":  # a Workspace without Setup: skip
                    counters["errors"] += 1
                    log.warning("maintenance step %s failed in %s: %s", step.__name__, ws, exc.code)
            except Exception:
                counters["errors"] += 1
                log.exception("maintenance step %s failed in %s", step.__name__, ws)
    return counters


def _sweep_work_items(runtime: Any, session: Session, ws: str, counters: dict[str, int]) -> None:
    actor = rerouting.system_principal(session, ws)
    store = runtime.store_for(session)
    report = timeouts.sweep(
        session, store, clock=runtime.clock, actor_account_id=actor.account_uuid
    )
    outcomes = rerouting.process_sweep(
        session,
        store,
        report,
        clock=runtime.clock,
        workspace_id=ws,
        actor=actor,
        authorizer=runtime.authorizer,
    )
    counters["rerouted"] += len(outcomes)


def _sweep_verifier_offers(
    runtime: Any, session: Session, ws: str, counters: dict[str, int]
) -> None:
    actor = rerouting.system_principal(session, ws)
    out = assignment.sweep_timeouts(
        session,
        runtime.store_for(session),
        clock=runtime.clock,
        workspace_id=ws,
        actor=actor,
        authorizer=runtime.authorizer,
    )
    counters["verifier_timeouts"] += len(out)


def _sweep_offline(runtime: Any, session: Session, ws: str, counters: dict[str, int]) -> None:
    ctx = _system_ctx(runtime, session, ws, "offline")
    result = bus.execute(agent_cmds.SweepOffline(), ctx)
    counters["marked_offline"] += len(result.data.get("marked_offline", []))


def _sweep_breakglass(runtime: Any, session: Session, ws: str, counters: dict[str, int]) -> None:
    from server.security.breakglass import expire_sessions, open_posthoc

    ended = expire_sessions(session, runtime.store_for(session), clock=runtime.clock)
    session.commit()  # the post-hoc Tasks open in their own transactions (independent audits)
    for sid in ended:
        with session_scope(runtime.session_factory) as own:
            open_posthoc(
                own,
                runtime.store_for(own),
                sid,
                correlation_id=f"bg-expire:{sid}",
                clock=runtime.clock,
                runtime=runtime,
            )
    counters["breakglass_expired"] += len(ended)
