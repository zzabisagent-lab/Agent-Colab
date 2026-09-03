"""Durable Run claiming and leases (P5-03; development plan §10A.2 steps 3, 8, §10A.1 defaults).

A runner marks due Runs ``DUE``, claims them with ``FOR UPDATE SKIP LOCKED`` plus a database
lease (default 60 s, heartbeat 15 s) and hands each claimed Run to the execution package. Only
one runner can hold a Run; an expired lease returns the Run to ``DUE`` (the single backward
transition of the contract), so exactly one runner recovers it after a crash.

This module also adapts the database rows to the ``RunStore``/``SchedulerPorts`` protocols the
execution package depends on, so that package never touches SQL.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain import defaults
from server.domain.clock import Clock
from server.events.store import AppendRequest, EventStore, EventStoreError
from server.schedules import store as st

log = logging.getLogger(__name__)

CLAIM_LEASE_S = defaults.SCHEDULER_CLAIM_LEASE_S
HEARTBEAT_S = defaults.SCHEDULER_RUNNING_LEASE_HEARTBEAT_S
POLL_S = defaults.SCHEDULER_POLL_S
EXECUTOR_MISSING = "EXECUTOR_MISSING"


class RunExecutor(Protocol):
    """What the runner needs from the execution package (``server.schedules.execution``)."""

    def execute(self, run: Any, ctx: Any) -> Any: ...


def validate_scheduler_settings(poll_s: int, lease_s: int) -> None:
    """§10A.1: poll 5-60 s and the claim lease at least three times the poll interval."""
    low, high = defaults.SCHEDULER_POLL_RANGE_S
    if not low <= poll_s <= high:
        raise ValueError(f"SCHEDULER_POLL_INVALID: {poll_s}s outside {low}-{high}s")
    if lease_s < poll_s * defaults.SCHEDULER_LEASE_MIN_POLL_MULTIPLE:
        raise ValueError(
            f"SCHEDULER_LEASE_INVALID: lease {lease_s}s < "
            f"{defaults.SCHEDULER_LEASE_MIN_POLL_MULTIPLE}x poll {poll_s}s"
        )


def _append(
    store: EventStore | None,
    *,
    workspace_id: str,
    run: st.RunRow,
    event_type: str,
    payload: dict[str, Any],
    actor_account_id: str | None,
    scope: str,
    key: str,
) -> str | None:
    if store is None or actor_account_id is None:
        return None
    try:
        res = store.append(
            AppendRequest(
                workspace_id=workspace_id,
                aggregate_type="schedule_run",
                aggregate_id=run.run_id,
                type=event_type,
                actor_account_id=actor_account_id,
                correlation_id=f"scheduler:{run.run_id}",
                idempotency_scope=scope,
                idempotency_key=key,
                payload=payload,
            )
        )
    except EventStoreError as exc:
        if exc.code != "IDEMPOTENCY_CONFLICT":
            raise
        return None
    return res.event_id


def mark_due(
    session: Session,
    *,
    workspace_id: str,
    now: dt.datetime,
    store: EventStore | None = None,
    actor_account_id: str | None = None,
    limit: int = 200,
) -> list[str]:
    """PENDING Runs whose instant has arrived become DUE (one ``RUN_DUE`` Event each)."""
    rows = session.execute(
        text(
            "SELECT run_id FROM schedule_runs WHERE workspace_id = :w AND status = 'PENDING' "
            "AND scheduled_for <= :now ORDER BY scheduled_for, run_id LIMIT :lim "
            "FOR UPDATE SKIP LOCKED"
        ),
        {"w": uuid.UUID(workspace_id), "now": now, "lim": limit},
    ).all()
    due: list[str] = []
    for (run_id,) in rows:
        run = st.load_run(session, str(run_id))
        if run is None:  # pragma: no cover - selected above
            continue
        st.update_run(session, run.run_id, now, status="DUE")
        _append(
            store,
            workspace_id=workspace_id,
            run=run,
            event_type="RUN_DUE",
            payload={
                "run_id": run.run_id,
                "schedule_id": run.schedule_id,
                "schedule_version_id": run.version_public_id or str(run.schedule_version_id),
                "run_kind": run.run_kind,
                "scheduled_for": st.iso_ms(run.scheduled_for),
            },
            actor_account_id=actor_account_id,
            scope="schedule_run:due",
            key=f"due:{run.run_id}",
        )
        due.append(run.run_id)
    return due


def claim_due(
    session: Session,
    runner_id: str,
    now: dt.datetime,
    *,
    workspace_id: str,
    lease_s: int = CLAIM_LEASE_S,
    limit: int = 1,
    store: EventStore | None = None,
    actor_account_id: str | None = None,
) -> list[st.RunRow]:
    """Claim up to ``limit`` due Runs for this runner (``FOR UPDATE SKIP LOCKED`` + DB lease)."""
    mark_due(
        session,
        workspace_id=workspace_id,
        now=now,
        store=store,
        actor_account_id=actor_account_id,
    )
    rows = session.execute(
        text(
            "SELECT run_id FROM schedule_runs WHERE workspace_id = :w AND status = 'DUE' "
            "AND scheduled_for <= :now ORDER BY scheduled_for, run_id LIMIT :lim "
            "FOR UPDATE SKIP LOCKED"
        ),
        {"w": uuid.UUID(workspace_id), "now": now, "lim": limit},
    ).all()
    claimed: list[st.RunRow] = []
    lease_until = now + dt.timedelta(seconds=lease_s)
    for (run_id,) in rows:
        run = st.load_run(session, str(run_id))
        if run is None or run.status != "DUE":  # pragma: no cover - locked above
            continue
        st.update_run(
            session,
            run.run_id,
            now,
            status="CLAIMED",
            claimed_by=runner_id,
            claimed_at=now,
            lease_expires_at=lease_until,
            heartbeat_at=now,
        )
        _append(
            store,
            workspace_id=workspace_id,
            run=run,
            event_type="RUN_CLAIMED",
            payload={
                "run_id": run.run_id,
                "claimed_by": runner_id,
                "lease_expires_at": st.iso_ms(lease_until),
            },
            actor_account_id=actor_account_id,
            scope="schedule_run:claim",
            key=f"claim:{run.run_id}:{runner_id}:{int(now.timestamp())}",
        )
        fresh = st.load_run(session, run.run_id)
        if fresh is not None:
            claimed.append(fresh)
    return claimed


def heartbeat(
    session: Session, run_id: str, runner_id: str, now: dt.datetime, lease_s: int = CLAIM_LEASE_S
) -> bool:
    """Extend the lease of a Run this runner still owns; False when it was taken over."""
    result = session.execute(
        text(
            "UPDATE schedule_runs SET heartbeat_at = :now, lease_expires_at = :until, "
            "updated_at = :now WHERE run_id = :r AND claimed_by = :runner AND status IN "
            "('CLAIMED','TASK_CREATED','RUNNING','VERIFYING','CANCEL_REQUESTED')"
        ),
        {
            "now": now,
            "until": now + dt.timedelta(seconds=lease_s),
            "r": run_id,
            "runner": runner_id,
        },
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def expire_leases(
    session: Session,
    now: dt.datetime,
    *,
    workspace_id: str | None = None,
    store: EventStore | None = None,
    actor_account_id: str | None = None,
) -> int:
    """Claimed Runs whose lease elapsed return to DUE so another runner recovers them."""
    cond = " AND workspace_id = :w" if workspace_id else ""
    rows = session.execute(
        text(
            "SELECT run_id FROM schedule_runs WHERE status = 'CLAIMED' "  # noqa: S608 - constant fragment, bound parameters
            f"AND lease_expires_at IS NOT NULL AND lease_expires_at < :now{cond} "
            "ORDER BY run_id FOR UPDATE SKIP LOCKED"
        ),
        {"now": now, "w": uuid.UUID(workspace_id) if workspace_id else None},
    ).all()
    recovered = 0
    for (run_id,) in rows:
        run = st.load_run(session, str(run_id))
        if run is None:  # pragma: no cover
            continue
        st.update_run(
            session,
            run.run_id,
            now,
            status="DUE",
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
        )
        _append(
            store,
            workspace_id=str(run.workspace_id),
            run=run,
            event_type="RUN_DUE",
            payload={
                "run_id": run.run_id,
                "schedule_id": run.schedule_id,
                "schedule_version_id": run.version_public_id or str(run.schedule_version_id),
                "run_kind": run.run_kind,
                "scheduled_for": st.iso_ms(run.scheduled_for),
                "reason": "LEASE_EXPIRED",
                "previous_claimed_by": run.claimed_by,
            },
            actor_account_id=actor_account_id,
            scope="schedule_run:due",
            key=f"lease-expired:{run.run_id}:{int(now.timestamp())}",
        )
        recovered += 1
    return recovered


# ----------------------------------------------------------------- execution package bridge
def execute_claimed(session: Session, run: st.RunRow, ctx: Any) -> Any:
    """Hand a claimed Run to the execution package (``server.schedules.execution``).

    The execution package owns §10A.2 steps 4-7 (policy re-check, concurrency, Task creation,
    retries, notices). When it is not installed the Run fails with ``EXECUTOR_MISSING`` rather
    than silently staying claimed.
    """
    try:
        from server.schedules import execution
    except ImportError:  # pragma: no cover - the package ships together
        now = ctx.clock.now() if hasattr(ctx, "clock") else dt.datetime.now(dt.UTC)
        st.update_run(
            session, run.run_id, now, status="FAILED", error_code=EXECUTOR_MISSING, finished_at=now
        )
        log.error("no execution package installed; run %s failed", run.run_id)
        return None
    return execution.execute(run, ctx)


def tick(
    session: Session,
    *,
    store: EventStore,
    clock: Clock,
    workspace_id: str,
    runner_id: str,
    actor_account_id: str,
    horizon_s: int | None = None,
    lease_s: int = CLAIM_LEASE_S,
    limit: int = 10,
) -> list[st.RunRow]:
    """One scheduler tick: recover expired leases, materialize occurrences, claim due Runs.

    Execution of the claimed Runs is the execution package's job (P5-04..P5-07); this function
    returns the claimed Runs so the caller can hand each to :func:`execute_claimed`.
    """
    from server.schedules.planner import DEFAULT_HORIZON_S, materialize

    now = clock.now()
    expire_leases(
        session, now, workspace_id=workspace_id, store=store, actor_account_id=actor_account_id
    )
    materialize(session, store, clock, workspace_id, horizon_s or DEFAULT_HORIZON_S)
    return claim_due(
        session,
        runner_id,
        clock.now(),
        workspace_id=workspace_id,
        lease_s=lease_s,
        limit=limit,
        store=store,
        actor_account_id=actor_account_id,
    )
