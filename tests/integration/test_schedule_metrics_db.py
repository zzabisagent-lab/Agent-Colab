"""V-P5-25 (metrics/history) and the alert half of V-P5-27: the dashboard values equal the Run
history, alerts are emitted once per hour through the notification outbox, and the planner's
duplicate-prevention counter is persisted. Runs against the real schedule tables (migration 0016)
seeded through schedule_metrics_fixture."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.main import create_app
from server.ops import dashboard
from server.schedules import metrics as m
from tests.integration.phase4_admin_seed import T0, Seed, seed
from tests.integration.schedule_metrics_fixture import (
    ensure_channel,
    insert_run,
    insert_schedule,
)

pytestmark = pytest.mark.db
SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "documents"
    / "schedule-metrics.v1.schema.json"
)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def sd(engine: Engine) -> Seed:
    return seed(engine, "smet")


NOW = T0 + dt.timedelta(days=1)


@pytest.fixture(scope="module")
def expectations(engine: Engine, sd: Seed) -> dict[str, int]:
    """Seed once per module so every test is self-sufficient under any -k selection."""
    return _seed_runs(engine, sd, NOW)


def _seed_runs(engine: Engine, sd: Seed, now: dt.datetime) -> dict[str, int]:
    """A known population of Runs; returns the independently computed expectations."""
    ws = sd.ws
    by = sd.accounts["admin1"]
    with Session(engine) as s, s.begin():
        chan = ensure_channel(s, ws, "chan-smet")
        v1 = insert_schedule(
            s,
            ws,
            "sched-m1",
            created_by=by,
            channel_uuid=chan,
            name="hourly",
            next_run_at=now + dt.timedelta(minutes=30),
        )
        v2 = insert_schedule(s, ws, "sched-m2", created_by=by, channel_uuid=chan, name="nightly")

        def t(mins: int) -> dt.datetime:
            return now + dt.timedelta(minutes=mins)

        def run1(**kw: Any) -> str:
            return insert_run(s, ws, schedule_id="sched-m1", version=v1, **kw)

        def run2(**kw: Any) -> str:
            return insert_run(s, ws, schedule_id="sched-m2", version=v2, **kw)

        run1(status="DUE", scheduled_for=t(-5))
        run1(
            status="CLAIMED",
            scheduled_for=t(-3),
            claimed_at=t(-2),
            claimed_by="runner-a",
            lease_expires_at=t(-1),
        )
        run1(
            status="SUCCEEDED",
            scheduled_for=t(-60),
            started_at=t(-60) + dt.timedelta(seconds=12),
            finished_at=t(-59),
        )
        run1(
            status="SUCCEEDED",
            scheduled_for=t(-120),
            started_at=t(-120) + dt.timedelta(seconds=90),
            finished_at=t(-118),
        )
        run2(
            status="FAILED",
            scheduled_for=t(-30),
            started_at=t(-30) + dt.timedelta(seconds=4),
            finished_at=t(-29),
            error_code="ADAPTER_TIMEOUT",
        )
        run2(status="SKIPPED", scheduled_for=t(-20), error_code="SKIPPED_POLICY")
        run2(
            status="TIMED_OUT",
            scheduled_for=t(-10),
            started_at=t(-10) + dt.timedelta(seconds=2),
            finished_at=t(-5),
        )
        run2(  # outside the 24 h window
            status="SUCCEEDED",
            scheduled_for=t(-48 * 60),
            started_at=t(-48 * 60) + dt.timedelta(seconds=1),
            finished_at=t(-47 * 60),
        )
        m.record_duplicate_prevented(s, ws, "sched-m1", now)
        m.record_duplicate_prevented(s, ws, "sched-m1", now)
    return {
        "due": 1,
        "running": 1,  # CLAIMED counts as active
        "runs_in_window": 7,  # the 48 h old success is out of the window
        "stuck_leases": 1,  # lease expired 60 s ago, poll 15 s
        "failures": 1,
        "timed_out": 1,
        "succeeded": 2,
        "policy_denials": 1,
        "duplicates_prevented": 2,
        "start_delay_p95": 90,
        "lag_count": 2,
    }


def test_snapshot_matches_history_and_schema(
    engine: Engine, sd: Seed, expectations: dict[str, int]
) -> None:
    now, expect = NOW, expectations
    with Session(engine) as s:
        snap = m.snapshot(s, sd.ws, now)
    Draft202012Validator(json.loads(SCHEMA.read_text())).validate(snap)
    assert snap["due"] == expect["due"] and snap["running"] == expect["running"]
    assert snap["runs_in_window"] == expect["runs_in_window"]
    assert snap["stuck_leases"] == expect["stuck_leases"]
    assert (snap["failures"], snap["timed_out"], snap["succeeded"]) == (
        expect["failures"],
        expect["timed_out"],
        expect["succeeded"],
    )
    assert snap["policy_denials"] == expect["policy_denials"]
    assert snap["duplicates_prevented"] == expect["duplicates_prevented"]
    assert snap["start_delay_s"]["p95"] == expect["start_delay_p95"]
    assert snap["lag_s"]["count"] == expect["lag_count"] and snap["lag_s"]["max"] == 300.0
    per = {x["schedule_id"]: x for x in snap["schedules"]}
    assert per["sched-m1"]["next_run_at"] == (now + dt.timedelta(minutes=30)).isoformat()
    assert per["sched-m1"]["duplicates_prevented"] == 2 and per["sched-m2"]["failures"] == 2
    assert per["sched-m2"]["last_status"] == "TIMED_OUT"
    # the dashboard shows the same numbers (V-P5-25)
    with Session(engine) as s, s.begin():
        over = dashboard.overview(s, sd.ws, FixedClock(now))
        # the probe cache is instance-level: leave no future-dated rows behind for other modules
        s.execute(text("DELETE FROM dependency_probes"))
    block = over["schedules"]
    assert (block["due"], block["running"], block["stuck_leases"]) == (1, 1, 1)
    assert block["start_delay_p95_s"] == 90 and block["failures"] == 1
    assert set(block["alerts"]) == {m.ALERT_START_DELAY, m.ALERT_STUCK_LEASES}


def test_alerts_emit_once_per_hour_through_the_outbox(
    engine: Engine, sd: Seed, expectations: dict[str, int]
) -> None:
    now = NOW + dt.timedelta(minutes=5)
    clock = FixedClock(now)
    with Session(engine) as s, s.begin():
        _, found, first = m.evaluate_and_emit(s, sd.ws, clock, ops_channel="ops-chan-x")
    assert {a["key"] for a in found} >= {m.ALERT_START_DELAY, m.ALERT_STUCK_LEASES}
    assert sorted(first.emitted) == sorted(a["key"] for a in found) and first.suppressed == []
    with Session(engine) as s, s.begin():
        _, _, second = m.evaluate_and_emit(s, sd.ws, clock, ops_channel="ops-chan-x")
    assert second.emitted == [] and sorted(second.suppressed) == sorted(first.emitted)
    clock.advance(dt.timedelta(hours=1))
    with Session(engine) as s, s.begin():
        _, _, third = m.evaluate_and_emit(s, sd.ws, clock, ops_channel="ops-chan-x")
    assert sorted(third.emitted) == sorted(first.emitted)
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT dedupe_key, payload FROM delivery_outbox WHERE workspace_id = :w "
                "AND kind = 'notification' AND destination = 'mattermost:ops-chan-x' ORDER BY id"
            ),
            {"w": sd.ws},
        ).all()
    assert len(rows) == 2 * len(first.emitted)
    assert all(r[1]["event_type"] == "SCHEDULE_ALERT" for r in rows)
    assert {r[1]["alert_key"] for r in rows} == set(first.emitted)
    # the start-delay alert (V-P5-27 alert half) exists exactly because p95 = 90 s > 60 s
    start = next(a for a in found if a["key"] == m.ALERT_START_DELAY)
    assert start["value"] == 90.0 and start["threshold"] == 60.0


def test_metrics_api_requires_schedule_or_admin_rights(
    database_url: str, sd: Seed, expectations: dict[str, int]
) -> None:
    app = create_app(
        Settings(database_url=database_url, base_url="http://t", master_key_b64=sd.master_key_b64)
    )
    with TestClient(app) as client:
        r = client.get("/api/v1/schedules/metrics", headers=sd.headers("admin1", "r"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema_id"] == m.SCHEMA_ID and isinstance(body["alerts"], list)
        assert body["duplicates_prevented"] == 2
        r = client.get("/api/v1/schedules/sched-m2/metrics", headers=sd.headers("admin1", "r"))
        assert r.status_code == 200 and r.json()["failures"] == 1
        assert [s["schedule_id"] for s in r.json()["schedules"]] == ["sched-m2"]
        r = client.get("/api/v1/schedules/metrics?window_s=1", headers=sd.headers("admin1", "r"))
        assert r.status_code == 422
        assert (
            client.get("/api/v1/schedules/metrics", headers=sd.headers("member", "r")).status_code
            == 404
        )
