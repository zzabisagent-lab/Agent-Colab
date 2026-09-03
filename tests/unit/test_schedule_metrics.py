"""P5-09 pure computations: percentiles, lag/start-delay/stuck-lease derivation, thresholds."""

from __future__ import annotations

import datetime as dt

import pytest

from server.schedules import metrics as m

T0 = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)


def _run(
    status: str, *, sched_offset_s: float, started_offset_s: float | None = None, **kw: object
) -> m.RunRow:
    scheduled = T0 + dt.timedelta(seconds=sched_offset_s)
    started = (
        None if started_offset_s is None else scheduled + dt.timedelta(seconds=started_offset_s)
    )
    return m.RunRow(
        run_id=f"run-{status}-{sched_offset_s}-{started_offset_s}",
        schedule_id=str(kw.pop("schedule_id", "sched-1")),
        status=status,
        scheduled_for=scheduled,
        started_at=started,
        **kw,  # type: ignore[arg-type]
    )


def test_percentiles_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert m.percentile(values, 50) == 50.0
    assert m.percentile(values, 95) == 95.0
    assert m.percentile(values, 100) == 100.0
    assert m.percentile([7.0], 95) == 7.0
    assert m.percentile([], 95) == 0.0
    with pytest.raises(ValueError):
        m.percentile([1.0], 101)
    p = m.percentiles([3.0, 1.0, 2.0])
    assert (p.count, p.p50, p.p95, p.max) == (3, 2.0, 3.0, 3.0)


def test_compute_derives_everything_from_run_rows() -> None:
    now = T0 + dt.timedelta(minutes=10)
    rows = [
        _run("DUE", sched_offset_s=0),  # lag 600 s
        _run(
            "CLAIMED", sched_offset_s=300, lease_expires_at=now - dt.timedelta(seconds=40)
        ),  # stuck
        _run("CLAIMED", sched_offset_s=400, lease_expires_at=now + dt.timedelta(seconds=40)),
        _run("SUCCEEDED", sched_offset_s=-600, started_offset_s=10),
        _run("SUCCEEDED", sched_offset_s=-500, started_offset_s=70),
        _run("FAILED", sched_offset_s=-400, started_offset_s=5, error_code="ADAPTER_TIMEOUT"),
        _run("TIMED_OUT", sched_offset_s=-300, started_offset_s=3),
        _run("SKIPPED", sched_offset_s=-200, error_code="SKIPPED_POLICY"),
        _run("SKIPPED", sched_offset_s=-100, error_code="SKIPPED_CONCURRENCY"),
        _run(
            "SUCCEEDED",
            sched_offset_s=-50,
            started_offset_s=1,
            error_code="BACKFILL_LIMITED_WARNING",
        ),
        _run(
            "RUNNING", sched_offset_s=-10, started_offset_s=2, kind="MANUAL", schedule_id="sched-2"
        ),
    ]
    snap = m.compute(
        rows,
        now,
        poll_s=15,
        schedules=[m.ScheduleRow("sched-1", "one", "ENABLED", now + dt.timedelta(minutes=5))],
        duplicates_prevented={"sched-1": 2},
        budget_alerts=1,
    )
    assert snap["schema_id"] == m.SCHEMA_ID and snap["due"] == 1 and snap["running"] == 3
    assert snap["runs_in_window"] == 11 and snap["stuck_leases"] == 1
    assert snap["lag_s"]["count"] == 3 and snap["lag_s"]["max"] == 600.0
    assert snap["start_delay_s"]["count"] == 5  # manual Runs are not scheduled-start delays
    assert snap["start_delay_s"]["p95"] == 70.0 and snap["start_delay_s"]["max"] == 70.0
    assert (snap["failures"], snap["timed_out"], snap["succeeded"]) == (1, 1, 3)
    assert snap["failure_rate"] == pytest.approx(2 / 5)
    assert snap["skips_by_code"] == {"SKIPPED_CONCURRENCY": 1, "SKIPPED_POLICY": 1}
    assert snap["policy_denials"] == 1 and snap["backfill_warnings"] == 1
    assert snap["duplicates_prevented"] == 2 and snap["budget_alerts"] == 1
    per = {s["schedule_id"]: s for s in snap["schedules"]}
    assert per["sched-1"]["next_run_at"] == (now + dt.timedelta(minutes=5)).isoformat()
    assert per["sched-1"]["runs_in_window"] == 10 and per["sched-1"]["failures"] == 2
    assert per["sched-2"]["last_status"] == "RUNNING" and per["sched-2"]["next_run_at"] is None


def test_alert_thresholds_fire_strictly_above_60s_and_on_stuck_leases_and_failure_rate() -> None:
    base = m.compute([], T0)
    assert m.alerts(base) == []
    at_target = {**base, "start_delay_s": {"count": 20, "p50": 5.0, "p95": 60.0, "max": 61.0}}
    assert m.alerts(at_target) == []  # p95 of exactly 60 s meets the target
    above = {**at_target, "start_delay_s": {**at_target["start_delay_s"], "p95": 60.5}}
    keys = [a["key"] for a in m.alerts(above)]
    assert keys == [m.ALERT_START_DELAY]
    stuck = {**base, "stuck_leases": 2}
    assert [a["severity"] for a in m.alerts(stuck)] == ["critical"]
    few_failures = {**base, "failures": 2, "succeeded": 0, "failure_rate": 1.0}
    assert m.alerts(few_failures) == []  # below the minimum sample
    many_failures = {**base, "failures": 4, "succeeded": 1, "failure_rate": 0.8}
    assert [a["key"] for a in m.alerts(many_failures)] == [m.ALERT_FAILURE_RATE]
    budget = {**base, "budget_alerts": 3}
    assert [a["key"] for a in m.alerts(budget)] == [m.ALERT_BUDGET]
    custom = m.Thresholds(start_delay_p95_s=10.0)
    assert [a["key"] for a in m.alerts(at_target, custom)] == [m.ALERT_START_DELAY]
