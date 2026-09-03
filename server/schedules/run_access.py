"""Database-backed :class:`~server.schedules.execution.RunStore` (P5-04..P5-07).

The execution package speaks only the ``RunStore`` protocol; this adapter maps it onto the
schedule core package's tables through ``server.schedules.store``, so execution contains no SQL
for Runs and the core package owns the schema.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.schedules import store as st
from server.schedules.execution import RunLike, ScheduleLike, VersionLike, coerce_run


class DbRunStore:
    """``RunStore`` over the Phase 5 schedule tables."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    # ---------------------------------------------------------------- reads

    def load_schedule(self, session: Session, schedule_id: str) -> ScheduleLike:
        row = session.execute(
            text(
                "SELECT schedule_id, workspace_id, name, status, current_version_id "
                "FROM schedules WHERE schedule_id = :s"
            ),
            {"s": schedule_id},
        ).first()
        if row is None:
            raise LookupError(f"SCHEDULE_NOT_FOUND: {schedule_id}")
        version = None if row[4] is None else st.load_version(session, row[4])
        return ScheduleLike(
            schedule_id=str(row[0]),
            workspace_id=str(row[1]),
            name=str(row[2]),
            status=str(row[3]),
            current_version_id=None if version is None else version.schedule_version_id,
        )

    def load_version(self, session: Session, schedule_version_id: str) -> VersionLike:
        version = st.load_version_by_public_id(session, schedule_version_id)
        if version is None:
            raise LookupError(f"SCHEDULE_VERSION_NOT_FOUND: {schedule_version_id}")
        return VersionLike(
            schedule_version_id=version.schedule_version_id,
            schedule_id=version.schedule_id,
            version=version.version,
            channel_id=str(version.channel_id),
            cron_expression=version.cron_expression,
            timezone=version.timezone,
            execution_principal_id=str(version.execution_principal_id),
            agent_selection=dict(version.agent_selection),
            action_template=dict(version.action_template),
            concurrency_policy=version.concurrency_policy,
            missed_run_policy=version.missed_run_policy,
            backfill_limit=version.backfill_limit,
            backfill_window_seconds=version.backfill_window_seconds,
            max_duration_seconds=version.max_duration_seconds,
            retry_policy=dict(version.retry_policy),
            budget_policy=dict(version.budget_policy),
            documentation_policy=dict(version.documentation_policy),
            starts_at=version.starts_at,
            ends_at=version.ends_at,
        )

    def load_run(self, session: Session, run_id: str, *, for_update: bool = False) -> RunLike:
        run = st.load_run(session, run_id, for_update=for_update)
        if run is None:
            raise LookupError(f"RUN_NOT_FOUND: {run_id}")
        return coerce_run(run)

    def active_runs(self, session: Session, schedule_id: str, exclude_run_id: str) -> list[RunLike]:
        return [
            coerce_run(r)
            for r in st.active_runs(session, schedule_id)
            if r.run_id != exclude_run_id
        ]

    def runs_for_task(self, session: Session, task_id: str) -> list[RunLike]:
        rows = session.execute(
            text("SELECT run_id FROM schedule_runs WHERE task_id = :t ORDER BY created_at"),
            {"t": task_id},
        ).all()
        return self._load_all(session, [str(r[0]) for r in rows])

    def runs_by_status(
        self, session: Session, workspace_id: str, statuses: Iterable[str]
    ) -> list[RunLike]:
        wanted = list(statuses)
        if not wanted:
            return []
        rows = session.execute(
            text(
                "SELECT run_id FROM schedule_runs WHERE workspace_id = :w AND status = ANY(:st) "
                "ORDER BY scheduled_for, run_id"
            ),
            {"w": uuid.UUID(workspace_id), "st": wanted},
        ).all()
        return self._load_all(session, [str(r[0]) for r in rows])

    def run_ids_for_day(self, session: Session, schedule_id: str, day: dt.date) -> list[str]:
        start = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
        rows = session.execute(
            text(
                "SELECT run_id FROM schedule_runs WHERE schedule_id = :s "
                "AND scheduled_for >= :a AND scheduled_for < :b ORDER BY scheduled_for"
            ),
            {"s": schedule_id, "a": start, "b": start + dt.timedelta(days=1)},
        ).all()
        return [str(r[0]) for r in rows]

    def _load_all(self, session: Session, run_ids: list[str]) -> list[RunLike]:
        out: list[RunLike] = []
        for run_id in run_ids:
            run = st.load_run(session, run_id)
            if run is not None:
                out.append(coerce_run(run))
        return out

    # ---------------------------------------------------------------- writes

    def update_run(self, session: Session, run_id: str, **cols: Any) -> RunLike:
        st.update_run(session, run_id, self.clock.now(), **cols)
        return self.load_run(session, run_id)

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
    ) -> None:
        existing = session.execute(
            text("SELECT 1 FROM schedule_run_attempts WHERE run_id = :r AND attempt_no = :n"),
            {"r": run_id, "n": attempt_no},
        ).first()
        if existing is not None:
            if finished_at is None and result is None:
                return
            st.finish_attempt(
                session,
                run_id,
                attempt_no,
                finished_at=finished_at or self.clock.now(),
                result=result or "",
                error_code=error_code,
            )
            return
        st.add_attempt(
            session,
            run_id,
            runner_id="",
            started_at=started_at or self.clock.now(),
            finished_at=finished_at,
            result=result,
            error_code=error_code,
        )

    def create_run(self, session: Session, run: RunLike) -> RunLike:
        version = st.load_version_by_public_id(session, run.schedule_version_id)
        if version is None:
            raise LookupError(f"SCHEDULE_VERSION_NOT_FOUND: {run.schedule_version_id}")
        row = session.execute(
            text("SELECT workspace_id FROM schedules WHERE schedule_id = :s"),
            {"s": run.schedule_id},
        ).first()
        if row is None:
            raise LookupError(f"SCHEDULE_NOT_FOUND: {run.schedule_id}")
        created = st.insert_run(
            session,
            workspace_id=uuid.UUID(str(row[0])),
            schedule_id=run.schedule_id,
            version=version,
            run_kind=run.run_kind,
            scheduled_for=run.scheduled_for,
            idempotency_key=run.idempotency_key,
            status=run.status,
            now=self.clock.now(),
            occurrence_key=run.occurrence_key,
            local_scheduled_for=run.local_scheduled_for,
            retry_of_run_id=run.retry_of_run_id,
            run_id=run.run_id,
        )
        if created is None:  # an equal Run already exists (idempotent materialization)
            return self.load_run(session, run.run_id)
        return coerce_run(created)
