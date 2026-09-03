"""Missed-run execution, retry/backoff, timeout and cancel windows, manual Runs
(P5-05/P5-06; V-P5-12, V-P5-13, V-P5-14, V-P5-19, V-P5-20, V-P5-21)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import bus
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.schedules import execution, recovery
from server.schedules.contract import (
    MissedOccurrence,
    MissedRunPolicy,
    RunStatus,
    plan_missed_runs,
)
from tests.integration.schedule_exec_fixture import Fixture, advance
from tests.integration.schedule_seed import T0

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def fx(engine: Engine) -> Fixture:
    return Fixture.create(engine, f"rec{uuid.uuid4().hex[:6]}", FixedClock(T0))


def _missed(fx: Fixture, count: int, step_min: int = 5) -> list[MissedOccurrence]:
    base = fx.clock.now() - dt.timedelta(minutes=step_min * count)
    return [
        MissedOccurrence(f"key-{i}", base + dt.timedelta(minutes=step_min * i))
        for i in range(count)
    ]


def test_missed_run_policies_decide_what_executes_after_a_restart(
    fx: Fixture, engine: Engine
) -> None:
    """V-P5-12/13/14: SKIP executes nothing; RUN_ONCE executes only the most recent occurrence
    with its original scheduled_for; BACKFILL_LIMITED executes oldest first up to the limit and
    records a warning for what it dropped."""
    missed = _missed(fx, 4)
    assert plan_missed_runs(MissedRunPolicy.SKIP, list(missed), fx.clock.now()).to_create == ()

    once = plan_missed_runs(MissedRunPolicy.RUN_ONCE, list(missed), fx.clock.now())
    assert [m.occurrence_key for m in once.to_create] == ["key-3"]

    limited = plan_missed_runs(
        MissedRunPolicy.BACKFILL_LIMITED,
        list(missed),
        fx.clock.now(),
        backfill_window_seconds=3600,
        backfill_limit=2,
    )
    assert [m.occurrence_key for m in limited.to_create] == ["key-0", "key-1"]  # oldest first
    assert limited.warning and "BACKFILL_TRUNCATED" in limited.warning

    # the planner's chosen occurrences execute with their ORIGINAL scheduled_for preserved
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-missed", missed_run="RUN_ONCE")
        chosen = once.to_create[0]
        run = fx.run(
            s,
            "sch-missed",
            run_id="run-missed-1",
            scheduled_for=chosen.scheduled_for,
            occurrence_key=chosen.occurrence_key,
        )
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.status == RunStatus.TASK_CREATED.value
        assert fx.reload(s, "run-missed-1").scheduled_for == chosen.scheduled_for
        # a late start raises the start-delay alert and posts the late notice
        alerts = s.execute(
            text("SELECT kind FROM budget_alerts WHERE run_id = 'run-missed-1'")
        ).all()
        assert [str(a[0]) for a in alerts] == ["start_delay"]
        assert "late" in [n["kind"] for n in _notices(s, "run-missed-1")]


def _notices(session: Session, run_id: str) -> list[dict[str, str]]:
    from server.schedules.notify import notices

    return notices(session, run_id)


def test_transient_failures_retry_three_times_then_fail(fx: Fixture, engine: Engine) -> None:
    """V-P5-19: at most 3 attempts with 1/5/25 s backoff (+0-20 % jitter); a permanent error is
    terminal FAILED after one attempt."""
    calls: dict[str, int] = {"n": 0}

    def flaky(cmd: object, ctx: object) -> object:
        calls["n"] += 1
        raise bus.CommandError("TRANSIENT", "provider hiccup", status=503)

    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-retry", retry_policy={"max_attempts": 3})
        run = fx.run(s, "sch-retry", run_id="run-retry-1")
        ctx = fx.ctx(s)
        original = execution.bus.execute
        execution.bus.execute = flaky  # type: ignore[assignment]
        try:
            first = execution.execute(run, ctx)
            assert first.retry_at is not None
            assert 1.0 <= (first.retry_at - fx.clock.now()).total_seconds() <= 1.2  # 1 s + jitter
            advance(fx.clock, 2)
            retried = recovery.run_due_retries(ctx)
            assert retried == ("run-retry-1",)
            second = execution.pending_retry(s, "run-retry-1")
            assert second is not None and second["next_attempt_no"] == 3
            gap = (second["next_attempt_at"] - fx.clock.now()).total_seconds()
            assert 5.0 <= gap <= 6.0  # 5 s + jitter
            advance(fx.clock, 6)
            recovery.run_due_retries(ctx)
        finally:
            execution.bus.execute = original  # type: ignore[assignment]
        final = fx.reload(s, "run-retry-1")
        assert final.status == RunStatus.FAILED.value
        assert final.error_code in ("RETRY_EXHAUSTED", "TRANSIENT")
        assert calls["n"] == 3 and len(fx.attempts(s, "run-retry-1")) == 3
        assert execution.pending_retry(s, "run-retry-1") is None


def test_permanent_failure_is_terminal_after_one_attempt(fx: Fixture, engine: Engine) -> None:
    """V-P5-19 (permanent half): a non-transient error fails the Run immediately."""

    def broken(cmd: object, ctx: object) -> object:
        raise bus.CommandError("TASK_INPUT_INVALID", "bad template", status=400)

    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-perm")
        run = fx.run(s, "sch-perm", run_id="run-perm-1")
        original = execution.bus.execute
        execution.bus.execute = broken  # type: ignore[assignment]
        try:
            outcome = execution.execute(run, fx.ctx(s))
        finally:
            execution.bus.execute = original  # type: ignore[assignment]
        assert outcome.status == RunStatus.FAILED.value
        assert outcome.error_code == "TASK_INPUT_INVALID"
        assert len(fx.attempts(s, "run-perm-1")) == 1
        assert "RUN_FAILED" in fx.run_events(s, "run-perm-1")


def test_max_duration_cancels_then_times_out_when_cleanup_never_confirms(
    fx: Fixture, engine: Engine
) -> None:
    """V-P5-20: a Run past max_duration is asked to cancel (ack ≤ 10 s, cleanup ≤ 60 s); without
    confirmation it ends TIMED_OUT with the defined Events and its leases/budget cleaned up."""
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-timeout", max_duration_seconds=60)
        run = fx.run(s, "sch-timeout", run_id="run-timeout-1")
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.task_id
        advance(fx.clock, 61)
        ctx = fx.ctx(s)
        assert recovery.handle_timeouts(ctx) == ("run-timeout-1",)
        assert fx.reload(s, "run-timeout-1").status == RunStatus.CANCEL_REQUESTED.value
        # an unresponsive Adapter: the Task keeps running despite the cancel request
        s.execute(
            text("UPDATE tasks_projection SET status = 'RUNNING' WHERE task_id = :t"),
            {"t": outcome.task_id},
        )
        advance(fx.clock, 71)  # ack + cleanup window elapsed without the Task ending
        cancelled, timed_out = recovery.handle_cancel_windows(ctx)
        assert timed_out == ("run-timeout-1",) and cancelled == ()
        final = fx.reload(s, "run-timeout-1")
        assert final.status == RunStatus.TIMED_OUT.value
        events = fx.run_events(s, "run-timeout-1")
        assert "RUN_CANCEL_REQUESTED" in events and "RUN_TIMED_OUT" in events
        kinds = {
            a[0]
            for a in s.execute(
                text("SELECT kind FROM budget_alerts WHERE run_id = 'run-timeout-1'")
            ).all()
        }
        assert {"timeout", "cancel_timeout"} <= kinds


def test_cancel_window_closes_as_cancelled_when_the_task_ends(fx: Fixture, engine: Engine) -> None:
    """V-P5-20 (clean half): once the Task is gone the Run becomes CANCELLED, not TIMED_OUT."""
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-cancel", max_duration_seconds=60)
        run = fx.run(s, "sch-cancel", run_id="run-cancel-1")
        outcome = execution.execute(run, fx.ctx(s))
        ctx = fx.ctx(s)
        execution.request_cancel(ctx, fx.reload(s, "run-cancel-1"), "CANCELLED_BY_ADMIN")
        fx.finish_task(s, str(outcome.task_id), "CANCELLED")
        advance(fx.clock, 5)  # inside the ack window
        cancelled, timed_out = recovery.handle_cancel_windows(ctx)
        assert cancelled == ("run-cancel-1",) and timed_out == ()
        assert fx.reload(s, "run-cancel-1").status == RunStatus.CANCELLED.value


def test_manual_run_executes_independently_of_scheduled_occurrences(
    fx: Fixture, engine: Engine
) -> None:
    """V-P5-21 (execution half): a MANUAL Run has no occurrence key and runs on its own."""
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-manual", concurrency="ALLOW")
        scheduled = fx.run(s, "sch-manual", run_id="run-man-sched")
        manual = fx.run(s, "sch-manual", run_id="run-man-1", run_kind="MANUAL", occurrence_key=None)
        assert manual.occurrence_key is None
        first = execution.execute(scheduled, fx.ctx(s))
        second = execution.execute(manual, fx.ctx(s))
        assert first.task_id and second.task_id and first.task_id != second.task_id
        assert "RUN_STARTED" in fx.run_events(s, "run-man-1")
