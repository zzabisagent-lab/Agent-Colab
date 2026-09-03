"""Start-delay target for Schedule Runs under normal load (P5-10; V-P5-27).

Development plan §21.1 normal load: 100 active Schedules, at most 20 due per minute, 2 runners.
The simulation drives the execution package with a virtual clock and measures the delay between
each Run's ``scheduled_for`` and the moment its Task was created; p95 must stay ≤ 60 s and a Run
that exceeds it must raise a ``start_delay`` alert with the late notice.
"""

from __future__ import annotations

import datetime as dt
import statistics
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.domain import defaults
from server.domain.clock import FixedClock
from server.schedules import budget as run_budget
from server.schedules import execution
from server.schedules.contract import RunStatus
from tests.integration.schedule_exec_fixture import Fixture
from tests.integration.schedule_seed import T0

pytestmark = pytest.mark.db

SCHEDULES = 100
DUE_PER_MINUTE = 20
RUNNERS = 2
BATCH_PER_RUNNER_TICK = 10
TICK_S = defaults.SCHEDULER_POLL_S  # 15 s


def set_clock(clock: FixedClock, at: dt.datetime) -> None:
    """Move the virtual clock to an absolute instant (the simulation drives time explicitly)."""
    clock.advance(at - clock.now())


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


def test_start_delay_p95_under_normal_load(engine: Engine) -> None:
    """V-P5-27: p95 of (task created minus scheduled_for) ≤ 60 s at 20 due Runs per minute."""
    fx = Fixture.create(engine, f"lat{uuid.uuid4().hex[:6]}", FixedClock(T0))
    delays: list[float] = []
    with Session(engine) as s, s.begin():
        for i in range(SCHEDULES):
            # no Agent selection: the simulation measures scheduler latency, not routing
            fx.schedule(s, f"sch-lat-{i:03d}", concurrency="ALLOW", agent_selection={})

    minute = 0
    created = 0
    with Session(engine) as s, s.begin():
        pending: list[tuple[str, dt.datetime]] = []
        while created < SCHEDULES:
            # a minute's worth of occurrences becomes due at the top of the minute
            due_at = T0 + dt.timedelta(minutes=minute)
            for _ in range(DUE_PER_MINUTE):
                if created >= SCHEDULES:
                    break
                run_id = f"run-lat-{created:03d}"
                set_clock(fx.clock, due_at)
                fx.run(
                    s,
                    f"sch-lat-{created:03d}",
                    run_id=run_id,
                    status="DUE",
                    scheduled_for=due_at,
                )
                pending.append((run_id, due_at))
                created += 1
            # four ticks per minute, two runners each claiming a bounded batch
            for tick in range(4):
                set_clock(fx.clock, due_at + dt.timedelta(seconds=TICK_S * (tick + 1)))
                for _ in range(RUNNERS):
                    batch, pending = (
                        pending[:BATCH_PER_RUNNER_TICK],
                        pending[BATCH_PER_RUNNER_TICK:],
                    )
                    for run_id, scheduled_for in batch:
                        ctx = fx.ctx(s)
                        s.execute(
                            text(
                                "UPDATE schedule_runs SET status = 'CLAIMED', claimed_by = 'r', "
                                "claimed_at = :now, lease_expires_at = :lease WHERE run_id = :r"
                            ),
                            {
                                "now": ctx.now,
                                "lease": ctx.now + dt.timedelta(seconds=60),
                                "r": run_id,
                            },
                        )
                        outcome = execution.execute(ctx.store.load_run(s, run_id), ctx)
                        assert outcome.status == RunStatus.TASK_CREATED.value, outcome
                        delays.append((ctx.now - scheduled_for).total_seconds())
            minute += 1

    assert len(delays) == SCHEDULES
    ordered = sorted(delays)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    assert p95 <= defaults.SCHEDULE_START_DELAY_P95_S, (p95, statistics.mean(delays))
    assert max(ordered) <= 60


def test_a_late_start_raises_an_alert_and_a_late_notice(engine: Engine) -> None:
    """V-P5-27 (alert half): a Run whose start exceeds the p95 target alerts and posts a notice."""
    fx = Fixture.create(engine, f"late{uuid.uuid4().hex[:6]}", FixedClock(T0))
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-late")
        run = fx.run(s, "sch-late", run_id="run-late-1", scheduled_for=T0 - dt.timedelta(minutes=5))
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.status == RunStatus.TASK_CREATED.value
        alerts = [
            a
            for a in run_budget.alerts(s, str(fx.seed.ws), "start_delay")
            if a["run_id"] == "run-late-1"
        ]
        assert alerts and alerts[0]["detail"]["delay_s"] >= 300
        kinds = [
            str(r[0])
            for r in s.execute(
                text("SELECT kind FROM schedule_notices WHERE run_id = 'run-late-1' ORDER BY kind")
            ).all()
        ]
        assert kinds == ["late", "start"]
