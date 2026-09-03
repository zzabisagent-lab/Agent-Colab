"""Occurrence planner over the real database (P5-02).

V-P5-06 two planners materialize one Run per occurrence key, V-P5-04/05 DST gap and fold,
V-P5-12/13/14 missed-run materialization (SKIP / RUN_ONCE / BACKFILL_LIMITED) after downtime.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import schedules as sch
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.schedules import planner
from server.schedules import store as st
from tests.integration.schedule_seed import ACTION_TEMPLATE, AGENT_SELECTION, Seed, run_rows

pytestmark = pytest.mark.db
SEED = Seed("splan")
T0 = dt.datetime(2026, 3, 2, 8, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def seed(engine: Engine) -> Seed:
    return SEED


def _schedule(engine: Engine, seed: Seed, key: str, **over: Any) -> str:
    body: dict[str, Any] = {
        "name": key,
        "cron_expression": "0 * * * *",
        "timezone": "UTC",
        "channel_id": seed.channel_id,
        "execution_principal_id": seed.owner,
        "agent_selection": dict(AGENT_SELECTION),
        "action_template": dict(ACTION_TEMPLATE),
    }
    body.update(over)
    clock = FixedClock(T0)
    schedule_id = str(
        seed.run(engine, sch.CreateSchedule(**body), seed.owner, f"{key}-create", clock).resource_id
    )
    seed.run(engine, sch.EnableSchedule(schedule_id), seed.owner, f"{key}-enable", clock)
    return schedule_id


def _plan(engine: Engine, schedule_id: str, now: dt.datetime, horizon_s: int = 3600) -> Any:
    clock = FixedClock(now)
    with Session(engine) as s, s.begin():
        schedule = st.load_schedule(s, SEED.ws, schedule_id)
        assert schedule is not None and schedule.current_version_id is not None
        version = st.load_version(s, schedule.current_version_id)
        assert version is not None
        return planner.plan_schedule(
            s,
            PostgresEventStore(s, clock=clock),
            clock,
            schedule=schedule,
            version=version,
            horizon_s=horizon_s,
        )


def test_planner_materializes_each_occurrence_once(engine: Engine, seed: Seed) -> None:
    """Repeated ticks are idempotent per (schedule_id, occurrence_key)."""
    schedule_id = _schedule(engine, seed, "plan-once")
    first = _plan(engine, schedule_id, T0, horizon_s=3 * 3600)
    assert len(first.created) == 3  # 09:00, 10:00, 11:00 UTC
    again = _plan(engine, schedule_id, T0, horizon_s=3 * 3600)
    assert again.created == ()
    rows = run_rows(engine, schedule_id)
    assert len(rows) == 3 and len({r["occurrence_key"] for r in rows}) == 3
    assert {r["status"] for r in rows} == {"PENDING"}
    with Session(engine) as s:
        schedule = st.load_schedule(s, seed.ws, schedule_id)
    assert schedule is not None and schedule.next_run_at == dt.datetime(
        2026, 3, 2, 9, tzinfo=dt.UTC
    )


def test_two_planners_create_one_run_per_occurrence(engine: Engine, seed: Seed) -> None:
    """V-P5-06: concurrent planners racing on the same occurrence produce a single row."""
    schedule_id = _schedule(engine, seed, "plan-race")
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def tick() -> None:
        try:
            clock = FixedClock(T0)
            with Session(engine) as s, s.begin():
                schedule = st.load_schedule(s, SEED.ws, schedule_id)
                assert schedule is not None and schedule.current_version_id is not None
                version = st.load_version(s, schedule.current_version_id)
                assert version is not None
                barrier.wait(timeout=10)
                planner.plan_schedule(
                    s,
                    PostgresEventStore(s, clock=clock),
                    clock,
                    schedule=schedule,
                    version=version,
                    horizon_s=2 * 3600,
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    rows = run_rows(engine, schedule_id)
    keys = [r["occurrence_key"] for r in rows]
    assert len(keys) == len(set(keys)) == 2
    with Session(engine) as s:
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM schedule_runs WHERE schedule_id = :s "
                    "GROUP BY occurrence_key ORDER BY count(*) DESC LIMIT 1"
                ),
                {"s": schedule_id},
            ).scalar_one()
            == 1
        )


def test_dst_gap_is_skipped_and_fold_runs_once(engine: Engine, seed: Seed) -> None:
    """V-P5-04 / V-P5-05 in the planner: no Run for the gap, one Run for the folded minute."""
    gap_id = _schedule(
        engine, seed, "plan-gap", cron_expression="30 2 * * *", timezone="America/New_York"
    )
    # plan only 2026-03-08T00:00Z .. 12:00Z: the window holds exactly the non-existent 02:30 local
    with Session(engine) as s, s.begin():
        st.update_schedule(
            s, gap_id, T0, last_planned_until=dt.datetime(2026, 3, 8, 0, tzinfo=dt.UTC)
        )
    _plan(engine, gap_id, dt.datetime(2026, 3, 8, 0, tzinfo=dt.UTC), horizon_s=12 * 3600)
    assert run_rows(engine, gap_id) == []
    with Session(engine) as s:
        notes = st.planner_notes(s, gap_id)
    assert notes and notes[0]["reason"] == "DST_GAP"
    assert notes[0]["local_time"] == "2026-03-08T02:30"

    fold_id = _schedule(
        engine, seed, "plan-fold", cron_expression="30 1 * * *", timezone="America/New_York"
    )
    with Session(engine) as s, s.begin():
        st.update_schedule(
            s, fold_id, T0, last_planned_until=dt.datetime(2026, 11, 1, 3, tzinfo=dt.UTC)
        )
    _plan(engine, fold_id, dt.datetime(2026, 11, 1, 3, tzinfo=dt.UTC), horizon_s=6 * 3600)
    rows = run_rows(engine, fold_id)
    assert len(rows) == 1
    assert rows[0]["scheduled_for"] == dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("policy", "expected", "note"),
    [
        ("SKIP", 0, None),
        ("RUN_ONCE", 1, "MISSED_RUN_ONCE"),
        ("BACKFILL_LIMITED", 2, "MISSED_BACKFILL_LIMITED"),
    ],
)
def test_missed_run_policies_after_downtime(
    engine: Engine, seed: Seed, policy: str, expected: int, note: str | None
) -> None:
    """V-P5-12/13/14: the policy decides which missed occurrences are materialized."""
    schedule_id = _schedule(
        engine,
        seed,
        f"plan-missed-{policy.lower()}",
        missed_run_policy=policy,
        backfill_limit=2,
        backfill_window_seconds=4 * 3600,
    )
    # the server was down for five hours: 09:00..13:00 UTC are missed at 13:30
    with Session(engine) as s, s.begin():
        st.update_schedule(
            s, schedule_id, T0, last_planned_until=dt.datetime(2026, 3, 2, 8, 30, tzinfo=dt.UTC)
        )
    result = _plan(
        engine, schedule_id, dt.datetime(2026, 3, 2, 13, 30, tzinfo=dt.UTC), horizon_s=60
    )
    missed = [r for r in run_rows(engine, schedule_id) if r["planner_note"]]
    assert len(result.missed_created) == expected and len(missed) == expected
    if expected:
        assert {r["planner_note"] for r in missed} == {note}
        instants = sorted(r["scheduled_for"] for r in missed)
        assert instants[0] < dt.datetime(2026, 3, 2, 13, 30, tzinfo=dt.UTC)  # original instants
        if policy == "RUN_ONCE":
            assert instants == [dt.datetime(2026, 3, 2, 13, tzinfo=dt.UTC)]  # only the latest
        else:  # oldest first inside the window, up to the limit
            assert instants == [
                dt.datetime(2026, 3, 2, 10, tzinfo=dt.UTC),
                dt.datetime(2026, 3, 2, 11, tzinfo=dt.UTC),
            ]
            assert result.warning and "BACKFILL_TRUNCATED" in result.warning
    else:
        assert result.skipped == 5
    with Session(engine) as s:
        reasons = {n["reason"] for n in st.planner_notes(s, schedule_id)}
    assert not expected or "BACKFILL_TRUNCATED" in reasons or policy == "RUN_ONCE"


def test_planner_respects_validity_window(engine: Engine, seed: Seed) -> None:
    """Occurrences outside starts_at/ends_at are never materialized."""
    schedule_id = _schedule(
        engine,
        seed,
        "plan-window",
        starts_at="2026-03-02T10:00:00.000Z",
        ends_at="2026-03-02T11:00:00.000Z",
    )
    _plan(engine, schedule_id, T0, horizon_s=6 * 3600)
    instants = sorted(r["scheduled_for"] for r in run_rows(engine, schedule_id))
    assert instants == [
        dt.datetime(2026, 3, 2, 10, tzinfo=dt.UTC),
        dt.datetime(2026, 3, 2, 11, tzinfo=dt.UTC),
    ]


def test_workspace_planning_covers_every_enabled_schedule(engine: Engine, seed: Seed) -> None:
    """``materialize`` plans all ENABLED Schedules and skips DRAFT/PAUSED ones."""
    active = _schedule(engine, seed, "plan-ws-active", cron_expression="0 0 * * *")
    clock = FixedClock(T0)
    paused = _schedule(engine, seed, "plan-ws-paused", cron_expression="0 0 * * *")
    seed.run(engine, sch.PauseSchedule(paused), seed.owner, "plan-ws-pause", clock)
    with Session(engine) as s, s.begin():
        created = planner.materialize(
            s, PostgresEventStore(s, clock=clock), clock, str(seed.ws), 26 * 3600
        )
    assert created >= 1
    assert run_rows(engine, active) and run_rows(engine, paused) == []
    assert uuid.UUID(str(seed.ws))  # workspace scoped
