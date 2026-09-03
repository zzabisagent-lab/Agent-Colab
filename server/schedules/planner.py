"""Occurrence planner (P5-02; development plan §10A.2 steps 1-2, 9, §10A.3).

For every ENABLED Schedule the planner materializes the occurrences of the *current* version
inside the planning horizon, exactly once per ``(schedule_id, occurrence_key)``. Two planners
running concurrently therefore produce one Run per occurrence (``ON CONFLICT DO NOTHING``).

Occurrences that lie in the past because the server was down are materialized through the
missed-run policy (`SKIP | RUN_ONCE | BACKFILL_LIMITED`) with their original ``scheduled_for``;
non-existent local times (DST gaps) never become Runs and are recorded as planner notes.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.events.store import EventStore
from server.schedules import cron
from server.schedules import store as st
from server.schedules.contract import MissedOccurrence, MissedRunPolicy, plan_missed_runs
from server.schedules.occurrence import scheduled_idempotency_key

DEFAULT_HORIZON_S = 900  # 15 minutes of look-ahead per tick
MAX_OCCURRENCES_PER_TICK = 500


@dataclass(frozen=True)
class PlanResult:
    schedule_id: str
    created: tuple[str, ...] = ()
    missed_created: tuple[str, ...] = ()
    skipped: int = 0
    gaps: int = 0
    warning: str | None = None
    next_run_at: dt.datetime | None = None


def _expected_count(window_s: float, min_interval_minutes: int) -> int:
    per = max(1, min_interval_minutes) * 60
    return max(1, min(MAX_OCCURRENCES_PER_TICK, math.ceil(window_s / per) + 2))


def _occurrences(
    version: st.VersionRow, after: dt.datetime, until: dt.datetime
) -> list[cron.Occurrence]:
    """Executable occurrences and DST gaps in ``(after, until]``."""
    window = max(0.0, (until - after).total_seconds())
    count = _expected_count(window, version.min_interval_minutes)
    out: list[cron.Occurrence] = []
    for occ in cron.next_occurrences(
        version.cron_expression,
        version.timezone,
        after,
        count=count,
        schedule_id=version.schedule_id,
        include_gaps=True,
    ):
        if occ.utc is None:  # DST gap: keep while its wall-clock day is inside the window
            out.append(occ)
            continue
        if occ.utc > until:
            break
        out.append(occ)
    return out


def _within_validity(version: st.VersionRow, when: dt.datetime) -> bool:
    if version.starts_at is not None and when < version.starts_at:
        return False
    return not (version.ends_at is not None and when > version.ends_at)


def plan_schedule(
    session: Session,
    store: EventStore,
    clock: Clock,
    *,
    schedule: st.ScheduleRow,
    version: st.VersionRow,
    horizon_s: int = DEFAULT_HORIZON_S,
) -> PlanResult:
    """Materialize one Schedule's due and upcoming occurrences (idempotent per occurrence key)."""
    now = clock.now()
    window_end = now + dt.timedelta(seconds=horizon_s)
    start = schedule.last_planned_until or now
    created: list[str] = []
    gaps = 0
    missed: list[MissedOccurrence] = []
    local_of: dict[str, dt.datetime] = {}

    for occ in _occurrences(version, start, window_end):
        if occ.utc is None:
            gaps += 1
            st.add_planner_note(
                session,
                schedule_id=schedule.schedule_id,
                occurrence_key=occ.occurrence_key,
                reason="DST_GAP",
                now=now,
                local_time=occ.local.strftime("%Y-%m-%dT%H:%M"),
                detail="non-existent local time; occurrence skipped",
            )
            continue
        if not _within_validity(version, occ.utc):
            continue
        local_of[occ.occurrence_key] = occ.local
        if occ.utc <= now:  # the server was down (or the tick is late): missed-run policy decides
            missed.append(MissedOccurrence(occ.occurrence_key, occ.utc))
            continue
        run = st.insert_run(
            session,
            workspace_id=schedule.workspace_id,
            schedule_id=schedule.schedule_id,
            version=version,
            run_kind="SCHEDULED",
            scheduled_for=occ.utc,
            idempotency_key=scheduled_idempotency_key(schedule.schedule_id, occ.occurrence_key),
            status="PENDING",
            now=now,
            occurrence_key=occ.occurrence_key,
            local_scheduled_for=occ.local,
        )
        if run is not None:
            created.append(run.run_id)

    plan = plan_missed_runs(
        MissedRunPolicy(version.missed_run_policy),
        missed,
        now,
        backfill_window_seconds=version.backfill_window_seconds,
        backfill_limit=version.backfill_limit,
    )
    missed_created: list[str] = []
    for item in plan.to_create:
        note = f"MISSED_{version.missed_run_policy}"
        run = st.insert_run(
            session,
            workspace_id=schedule.workspace_id,
            schedule_id=schedule.schedule_id,
            version=version,
            run_kind="SCHEDULED",
            scheduled_for=item.scheduled_for,  # the original instant is preserved (§10A.2 step 9)
            idempotency_key=scheduled_idempotency_key(schedule.schedule_id, item.occurrence_key),
            status="PENDING",
            now=now,
            occurrence_key=item.occurrence_key,
            local_scheduled_for=local_of.get(item.occurrence_key),
            planner_note=note,
        )
        if run is not None:
            missed_created.append(run.run_id)
    for item in plan.skipped:
        st.add_planner_note(
            session,
            schedule_id=schedule.schedule_id,
            occurrence_key=item.occurrence_key,
            reason=f"MISSED_SKIPPED_{version.missed_run_policy}",
            now=now,
            scheduled_for=item.scheduled_for,
            detail=plan.warning,
        )
    if plan.warning:
        st.add_planner_note(
            session,
            schedule_id=schedule.schedule_id,
            occurrence_key=f"warn-{uuid.uuid4().hex[:12]}",
            reason="BACKFILL_TRUNCATED",
            now=now,
            detail=plan.warning,
        )

    upcoming = cron.next_occurrences(
        version.cron_expression,
        version.timezone,
        now,
        count=1,
        schedule_id=schedule.schedule_id,
        include_gaps=False,
    )
    next_run_at = upcoming[0].utc if upcoming else None
    st.update_schedule(
        session,
        schedule.schedule_id,
        now,
        last_planned_until=window_end,
        next_run_at=next_run_at,
    )
    return PlanResult(
        schedule.schedule_id,
        tuple(created),
        tuple(missed_created),
        len(plan.skipped),
        gaps,
        plan.warning,
        next_run_at,
    )


def plan_workspace(
    session: Session,
    store: EventStore,
    clock: Clock,
    workspace_id: str,
    horizon_s: int = DEFAULT_HORIZON_S,
) -> list[PlanResult]:
    """Plan every ENABLED Schedule of the Workspace (rows are locked with SKIP LOCKED)."""
    results: list[PlanResult] = []
    for schedule in st.enabled_schedules(session, uuid.UUID(workspace_id)):
        if schedule.current_version_id is None:
            continue
        version = st.load_version(session, schedule.current_version_id)
        if version is None:  # pragma: no cover - FK guarantees the row
            continue
        results.append(
            plan_schedule(
                session, store, clock, schedule=schedule, version=version, horizon_s=horizon_s
            )
        )
    return results


def materialize(
    session: Session,
    store: EventStore,
    clock: Clock,
    workspace_id: str,
    horizon_s: int = DEFAULT_HORIZON_S,
) -> int:
    """``SchedulerPorts.materialize``: number of Runs created in this tick."""
    return sum(
        len(r.created) + len(r.missed_created)
        for r in plan_workspace(session, store, clock, workspace_id, horizon_s)
    )
