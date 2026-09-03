"""Per-Run and daily cost_units budgets for Schedule Runs (P5-10; V-P5-28, V-P5-37)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.schedules import budget as run_budget
from server.schedules import execution
from server.schedules.contract import RunStatus, SkipCode
from server.usage.records import usage_for
from tests.integration.schedule_exec_fixture import Fixture
from tests.integration.schedule_seed import T0

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def fx(engine: Engine) -> Fixture:
    return Fixture.create(engine, f"bud{uuid.uuid4().hex[:6]}", FixedClock(T0))


def _alerts(session: Session, fx: Fixture, run_id: str) -> list[str]:
    return [a["kind"] for a in run_budget.alerts(session, str(fx.seed.ws)) if a["run_id"] == run_id]


def test_estimates_within_the_limit_run_and_the_next_one_is_rejected(
    fx: Fixture, engine: Engine
) -> None:
    """V-P5-28: with a per-Run limit of 100 and an estimate of 99/100 the Run proceeds; an
    estimate of 101 is rejected before any side effect with BUDGET_EXCEEDED and an alert."""
    with Session(engine) as s, s.begin():
        for name, estimate, expect_started in (
            ("99", 99, True),
            ("100", 100, True),
            ("101", 101, False),
        ):
            schedule_id = f"sch-bud-{name}"
            fx.schedule(
                s,
                schedule_id,
                concurrency="ALLOW",
                budget_policy={"per_run_cost_units": 100, "estimate_cost_units": estimate},
            )
            run = fx.run(s, schedule_id, run_id=f"run-bud-{name}")
            outcome = execution.execute(run, fx.ctx(s))
            if expect_started:
                assert outcome.status == RunStatus.TASK_CREATED.value, (name, outcome)
                assert outcome.task_id
            else:
                assert outcome.status == RunStatus.SKIPPED.value
                assert outcome.error_code == SkipCode.BUDGET_EXCEEDED.value
                assert outcome.task_id is None  # rejected before the side effect
                assert _alerts(s, fx, f"run-bud-{name}") == ["budget_exceeded"]


def test_usage_is_aggregated_per_run_and_settled_against_the_limit(
    fx: Fixture, engine: Engine
) -> None:
    """V-P5-37: the Run's usage_records aggregation matches, settlement records the overrun and
    the next Run of the same Schedule is skipped with BUDGET_EXCEEDED plus an alert."""
    with Session(engine) as s, s.begin():
        fx.schedule(
            s,
            "sch-bud-settle",
            concurrency="ALLOW",
            budget_policy={
                "per_run_cost_units": 100,
                "daily_cost_units": 100,
                "estimate_cost_units": 10,
            },
        )
        run = fx.run(s, "sch-bud-settle", run_id="run-bud-settle-1")
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.task_id
        # the Agent reports more usage than the limit allowed
        fx.report_usage(s, "run-bud-settle-1", 40, str(outcome.task_id))
        fx.report_usage(s, "run-bud-settle-1", 75, str(outcome.task_id))
        assert usage_for(s, "schedule_run", "run-bud-settle-1") == 115
        fx.finish_task(s, str(outcome.task_id), "COMPLETED")
        execution.on_task_terminal(fx.ctx(s), str(outcome.task_id), "COMPLETED")
        settled = s.execute(
            text(
                "SELECT scope_type, status, settled_cost_units FROM schedule_run_budgets "
                "WHERE run_id = 'run-bud-settle-1' ORDER BY scope_type"
            )
        ).all()
        assert {(str(r[0]), str(r[1]), int(r[2])) for r in settled} == {
            ("schedule_daily", "exceeded", 115),
            ("schedule_run", "exceeded", 115),
        }
        assert "budget_exceeded" in _alerts(s, fx, "run-bud-settle-1")

        # the overrun blocks the next Run of the same Schedule for the day
        nxt = fx.run(s, "sch-bud-settle", run_id="run-bud-settle-2")
        outcome2 = execution.execute(nxt, fx.ctx(s))
        assert outcome2.status == RunStatus.SKIPPED.value
        assert outcome2.error_code == SkipCode.BUDGET_EXCEEDED.value
        assert outcome2.task_id is None


def test_a_skipped_run_releases_its_reservation(fx: Fixture, engine: Engine) -> None:
    """A Run that never starts gives its reservation back, so the daily budget is not consumed."""
    with Session(engine) as s, s.begin():
        fx.schedule(
            s,
            "sch-bud-release",
            concurrency="FORBID",
            budget_policy={"per_run_cost_units": 100, "estimate_cost_units": 10},
        )
        fx.run(s, "sch-bud-release", run_id="run-bud-active", status="RUNNING", task_id="task-a")
        run = fx.run(s, "sch-bud-release", run_id="run-bud-released")
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.error_code == SkipCode.SKIPPED_CONCURRENCY.value
        status = s.execute(
            text(
                "SELECT status FROM schedule_run_budgets WHERE run_id = 'run-bud-released' "
                "AND scope_type = 'schedule_run'"
            )
        ).scalar_one()
        assert str(status) == "released"
