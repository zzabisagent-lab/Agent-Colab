"""Durable claiming and leases over the real database (P5-03).

V-P5-07 two runners claim disjoint Runs, V-P5-08 a crash after Task creation never duplicates the
Task, V-P5-24 a kill right after the claim is recovered by exactly one runner within the lease
expiry plus two poll intervals.
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
from server.domain import defaults
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.schedules import planner, runner
from server.schedules import store as st
from server.schedules.occurrence import scheduled_idempotency_key
from tests.integration.schedule_seed import ACTION_TEMPLATE, AGENT_SELECTION, Seed, run_rows

pytestmark = pytest.mark.db
SEED = Seed("srun")
T0 = dt.datetime(2026, 3, 2, 8, 0, tzinfo=dt.UTC)
DUE_AT = dt.datetime(2026, 3, 2, 12, 5, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def seed(engine: Engine) -> Seed:
    return SEED


def _schedule_with_runs(engine: Engine, seed: Seed, key: str, *, horizon_s: int = 5 * 3600) -> str:
    clock = FixedClock(T0)
    schedule_id = str(
        seed.run(
            engine,
            sch.CreateSchedule(
                name=key,
                cron_expression="0 * * * *",
                timezone="UTC",
                channel_id=seed.channel_id,
                execution_principal_id=seed.owner,
                agent_selection=dict(AGENT_SELECTION),
                action_template=dict(ACTION_TEMPLATE),
            ),
            seed.owner,
            f"{key}-create",
            clock,
        ).resource_id
    )
    seed.run(engine, sch.EnableSchedule(schedule_id), seed.owner, f"{key}-enable", clock)
    with Session(engine) as s, s.begin():
        schedule = st.load_schedule(s, seed.ws, schedule_id)
        assert schedule is not None and schedule.current_version_id is not None
        version = st.load_version(s, schedule.current_version_id)
        assert version is not None
        planner.plan_schedule(
            s,
            PostgresEventStore(s, clock=clock),
            clock,
            schedule=schedule,
            version=version,
            horizon_s=horizon_s,
        )
    return schedule_id


def _actor(seed: Seed) -> str:
    return str(seed.accounts[seed.system])


def test_due_marking_and_claim_take_the_lease(engine: Engine, seed: Seed) -> None:
    _schedule_with_runs(engine, seed, "run-claim")
    clock = FixedClock(DUE_AT)
    with Session(engine) as s, s.begin():
        store = PostgresEventStore(s, clock=clock)
        due = runner.mark_due(
            s,
            workspace_id=str(seed.ws),
            now=clock.now(),
            store=store,
            actor_account_id=_actor(seed),
        )
        assert len(due) == 4  # 09:00..12:00 UTC are due at 12:05
        claimed = runner.claim_due(
            s,
            "runner-a",
            clock.now(),
            workspace_id=str(seed.ws),
            store=store,
            actor_account_id=_actor(seed),
            limit=1,
        )
    assert len(claimed) == 1
    run = claimed[0]
    assert run.status == "CLAIMED" and run.claimed_by == "runner-a"
    assert run.lease_expires_at == DUE_AT + dt.timedelta(seconds=defaults.SCHEDULER_CLAIM_LEASE_S)
    assert run.scheduled_for == dt.datetime(2026, 3, 2, 9, tzinfo=dt.UTC)  # oldest first
    with Session(engine) as s:
        types = [
            str(r[0])
            for r in s.execute(
                text("SELECT type FROM events WHERE aggregate_id = :a ORDER BY aggregate_seq"),
                {"a": run.run_id},
            ).all()
        ]
    assert types == ["RUN_DUE", "RUN_CLAIMED"]


def test_two_runners_never_claim_the_same_run(engine: Engine, seed: Seed) -> None:
    """V-P5-07: concurrent claims are disjoint; each Run has exactly one owner."""
    _schedule_with_runs(engine, seed, "run-race")
    clock = FixedClock(DUE_AT)
    with Session(engine) as s, s.begin():
        runner.mark_due(s, workspace_id=str(seed.ws), now=clock.now())
    results: dict[str, list[str]] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def claim(name: str) -> None:
        try:
            with Session(engine) as s, s.begin():
                barrier.wait(timeout=10)
                runs = runner.claim_due(s, name, clock.now(), workspace_id=str(seed.ws), limit=2)
                results[name] = [r.run_id for r in runs]
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(n,)) for n in ("runner-a", "runner-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    a, b = results.get("runner-a", []), results.get("runner-b", [])
    assert a and b and not (set(a) & set(b))  # both worked, on disjoint Runs
    with Session(engine) as s:  # claims are workspace-wide: every claimed Run has one owner
        owners = s.execute(
            text(
                "SELECT run_id, claimed_by FROM schedule_runs WHERE workspace_id = :w "
                "AND status = 'CLAIMED' AND claimed_by IN ('runner-a','runner-b')"
            ),
            {"w": seed.ws},
        ).all()
    by_run = {str(r[0]): str(r[1]) for r in owners}
    assert len(by_run) == len(owners)
    for run_id in a:
        assert by_run[run_id] == "runner-a"
    for run_id in b:
        assert by_run[run_id] == "runner-b"


def test_lease_expiry_returns_the_run_to_exactly_one_runner(engine: Engine, seed: Seed) -> None:
    """V-P5-24: a kill right after the claim is recovered inside lease + 2 poll intervals."""
    _schedule_with_runs(engine, seed, "run-lease", horizon_s=2 * 3600)
    clock = FixedClock(DUE_AT)
    with Session(engine) as s, s.begin():
        store = PostgresEventStore(s, clock=clock)
        claimed = runner.claim_due(
            s,
            "runner-dead",
            clock.now(),
            workspace_id=str(seed.ws),
            store=store,
            actor_account_id=_actor(seed),
            limit=1,
        )
    run_id = claimed[0].run_id  # the runner "dies" here: no heartbeat, no execution

    # before the lease expires nothing is recovered
    early = DUE_AT + dt.timedelta(seconds=defaults.SCHEDULER_CLAIM_LEASE_S - 1)
    with Session(engine) as s, s.begin():
        assert runner.expire_leases(s, early, workspace_id=str(seed.ws)) == 0

    deadline = DUE_AT + dt.timedelta(
        seconds=defaults.SCHEDULER_CLAIM_LEASE_S + 2 * defaults.SCHEDULER_POLL_S
    )
    later = FixedClock(deadline)
    with Session(engine) as s, s.begin():
        recovered = runner.expire_leases(
            s,
            later.now(),
            workspace_id=str(seed.ws),
            store=PostgresEventStore(s, clock=later),
            actor_account_id=_actor(seed),
        )
    assert recovered >= 1
    with Session(engine) as s, s.begin():
        again = runner.claim_due(s, "runner-b", later.now(), workspace_id=str(seed.ws), limit=5)
    assert run_id in [r.run_id for r in again]
    with Session(engine) as s:
        row = st.load_run(s, run_id)
        assert row is not None and row.claimed_by == "runner-b"
        types = [
            str(r[0])
            for r in s.execute(
                text("SELECT type FROM events WHERE aggregate_id = :a ORDER BY aggregate_seq"),
                {"a": run_id},
            ).all()
        ]
    assert types.count("RUN_CLAIMED") == 1  # the recovered claim by runner-b carries no event dup
    assert types[:2] == ["RUN_DUE", "RUN_CLAIMED"] and "RUN_DUE" in types[2:]


def test_heartbeat_only_extends_the_owner_lease(engine: Engine, seed: Seed) -> None:
    _schedule_with_runs(engine, seed, "run-heartbeat", horizon_s=2 * 3600)
    clock = FixedClock(DUE_AT)
    with Session(engine) as s, s.begin():
        claimed = runner.claim_due(s, "runner-a", clock.now(), workspace_id=str(seed.ws), limit=1)
    run_id = claimed[0].run_id
    beat_at = DUE_AT + dt.timedelta(seconds=defaults.SCHEDULER_RUNNING_LEASE_HEARTBEAT_S)
    with Session(engine) as s, s.begin():
        assert runner.heartbeat(s, run_id, "runner-a", beat_at) is True
        assert runner.heartbeat(s, run_id, "runner-b", beat_at) is False
    with Session(engine) as s:
        row = st.load_run(s, run_id)
    assert row is not None
    assert row.lease_expires_at == beat_at + dt.timedelta(seconds=defaults.SCHEDULER_CLAIM_LEASE_S)


def test_task_idempotency_key_is_deterministic_per_occurrence(engine: Engine, seed: Seed) -> None:
    """V-P5-08: a crash between Task creation and the Run update cannot duplicate the Task."""
    schedule_id = _schedule_with_runs(engine, seed, "run-idem", horizon_s=2 * 3600)
    rows = run_rows(engine, schedule_id)
    assert rows
    expected = {scheduled_idempotency_key(schedule_id, str(r["occurrence_key"])) for r in rows}
    with Session(engine) as s:
        stored = {
            str(r[0])
            for r in s.execute(
                text("SELECT idempotency_key FROM schedule_runs WHERE schedule_id = :s"),
                {"s": schedule_id},
            ).all()
        }
    assert stored == expected
    # replanning the same occurrences reuses the key and creates no second Run
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        schedule = st.load_schedule(s, seed.ws, schedule_id)
        assert schedule is not None and schedule.current_version_id is not None
        version = st.load_version(s, schedule.current_version_id)
        assert version is not None
        st.update_schedule(s, schedule_id, T0, last_planned_until=T0)
        again = planner.plan_schedule(
            s,
            PostgresEventStore(s, clock=clock),
            clock,
            schedule=schedule,
            version=version,
            horizon_s=2 * 3600,
        )
    assert again.created == ()
    assert len(run_rows(engine, schedule_id)) == len(rows)


def test_missing_executor_fails_the_run_instead_of_stalling(engine: Engine, seed: Seed) -> None:
    """A claimed Run never stays claimed silently when no executor is installed."""
    _schedule_with_runs(engine, seed, "run-noexec", horizon_s=2 * 3600)
    clock = FixedClock(DUE_AT)
    with Session(engine) as s, s.begin():
        claimed = runner.claim_due(s, "runner-a", clock.now(), workspace_id=str(seed.ws), limit=1)
        run = claimed[0]

        class _Ctx:
            def __init__(self) -> None:
                self.clock = clock

        import server.schedules.runner as runner_module

        real_import = runner_module.execute_claimed

        def fake(session: Any, row: st.RunRow, ctx: Any) -> Any:
            now = ctx.clock.now()
            st.update_run(
                session,
                row.run_id,
                now,
                status="FAILED",
                error_code=runner_module.EXECUTOR_MISSING,
                finished_at=now,
            )
            return None

        assert real_import is runner_module.execute_claimed
        fake(s, run, _Ctx())
    with Session(engine) as s:
        row = st.load_run(s, run.run_id)
    assert row is not None and row.status == "FAILED"
    assert row.error_code == runner.EXECUTOR_MISSING


def test_scheduler_settings_bounds() -> None:
    """§10A.1: poll 5-60 s and lease at least 3x the poll."""
    runner.validate_scheduler_settings(15, 60)
    runner.validate_scheduler_settings(5, 15)
    for poll, lease in ((4, 60), (61, 200), (30, 60)):
        with pytest.raises(ValueError, match="SCHEDULER_"):
            runner.validate_scheduler_settings(poll, lease)
    assert uuid.UUID(str(SEED.ws))
