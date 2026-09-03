"""Schedule API and lifecycle over the real database (P5-01).

V-P5-01 cron validation, V-P5-03 timezones, V-P5-04/05 DST previews, V-P5-22 lifecycle and
version changes, V-P5-26 shell rejection, V-P5-31/32 Run cancel, V-P5-33 version pinning,
V-P5-34 manual retry, V-P5-35 manual/scheduled isolation, V-P5-36 Approval/Artifact subjects.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import artifacts as art
from server.application import bus
from server.application import schedules as sch
from server.application.approvals import RequestApproval
from server.artifacts import links as artifact_links
from server.artifacts.storage import ArtifactStorage
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.schedules import planner
from server.schedules import store as st
from tests.integration.schedule_seed import (
    ACTION_TEMPLATE,
    AGENT_SELECTION,
    T0,
    Seed,
    event_types,
    run_rows,
)

pytestmark = pytest.mark.db
CLOCK = FixedClock(T0)


SEED = Seed("sapi")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def seed(engine: Engine) -> Seed:
    return SEED


def _create(engine: Engine, seed: Seed, key: str, **over: Any) -> str:
    body: dict[str, Any] = {
        "name": "Daily digest",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "Asia/Seoul",
        "channel_id": seed.channel_id,
        "execution_principal_id": seed.owner,
        "agent_selection": dict(AGENT_SELECTION),
        "action_template": dict(ACTION_TEMPLATE),
    }
    body.update(over)
    return str(seed.run(engine, sch.CreateSchedule(**body), seed.owner, key, CLOCK).resource_id)


def test_create_validates_cron_timezone_and_action_template(engine: Engine, seed: Seed) -> None:
    """V-P5-01 / V-P5-26: only the normative grammar and shell-free templates are stored."""
    schedule_id = _create(engine, seed, "s-create-1")
    view = seed.read(engine, sch.get_schedule, schedule_id)
    assert view["status"] == "DRAFT" and view["current_version"]["version"] == 1
    assert view["current_version"]["cron_expression"] == "0 9 * * 1-5"
    assert len(view["current_version"]["snapshot_hash"]) == 64
    assert event_types(engine, schedule_id) == ["SCHEDULE_CREATED"]

    for field, value, code in (
        ("cron_expression", "0 9 * * MON", "CRON_NAME_REJECTED"),
        ("cron_expression", "*/1 * * * *", "CRON_INTERVAL_TOO_SHORT"),
        ("cron_expression", "0 9 * * 7", "CRON_DOW7_REJECTED"),
        ("cron_expression", "@daily", "CRON_ALIAS_REJECTED"),
        ("cron_expression", "0 0 * * * *", "CRON_SECONDS_REJECTED"),
        ("timezone", "Mars/Olympus", "TIMEZONE_INVALID"),
    ):
        assert (
            seed.run_expect(
                engine,
                sch.CreateSchedule(
                    **{  # type: ignore[arg-type]  # one field is overridden per case
                        "name": "bad",
                        "cron_expression": "0 9 * * 1-5",
                        "timezone": "UTC",
                        "channel_id": seed.channel_id,
                        "execution_principal_id": seed.owner,
                        "agent_selection": dict(AGENT_SELECTION),
                        "action_template": dict(ACTION_TEMPLATE),
                        field: value,
                    }
                ),
                seed.owner,
                f"s-bad-{field}-{value}",
                CLOCK,
            )
            == code
        ), (field, value)

    shell = {
        "schema_id": "action-template.v1",
        "action": "task_create",
        "input": {"title": "x", "command": "rm -rf /"},
    }
    assert (
        seed.run_expect(
            engine,
            sch.CreateSchedule(
                name="shell",
                cron_expression="0 9 * * 1-5",
                timezone="UTC",
                channel_id=seed.channel_id,
                execution_principal_id=seed.owner,
                agent_selection=dict(AGENT_SELECTION),
                action_template=shell,
            ),
            seed.owner,
            "s-shell-1",
            CLOCK,
        )
        == "ACTION_TEMPLATE_FORBIDDEN"
    )
    secret_value = {
        "schema_id": "action-template.v1",
        "action": "task_create",
        "input": {"title": "x", "api_key": "sk-live-not-a-ref"},
    }
    assert (
        seed.run_expect(
            engine,
            sch.CreateSchedule(
                name="secret",
                cron_expression="0 9 * * 1-5",
                timezone="UTC",
                channel_id=seed.channel_id,
                execution_principal_id=seed.owner,
                agent_selection=dict(AGENT_SELECTION),
                action_template=secret_value,
            ),
            seed.owner,
            "s-secret-1",
            CLOCK,
        )
        == "ACTION_TEMPLATE_SECRET_VALUE"
    )
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT count(*) FROM schedules WHERE workspace_id = :w"), {"w": seed.ws}
            ).scalar_one()
            == 1
        )  # zero side effects from the rejected creates


def test_preview_reports_dst_gap_and_fold(engine: Engine, seed: Seed) -> None:
    """V-P5-04 / V-P5-05: the gap is skipped with a reason, the fold runs once."""
    gap = seed.read(
        engine,
        sch.preview,
        cron_expression="30 2 * * *",
        timezone="America/New_York",
        after=dt.datetime(2026, 3, 7, 12, tzinfo=dt.UTC),
        count=2,
    )
    reasons = {i["local"]: (i["reason"], i["utc"]) for i in gap["items"]}
    assert reasons["2026-03-08T02:30"] == ("DST_GAP", None)
    assert reasons["2026-03-09T02:30"][0] is None

    fold = seed.read(
        engine,
        sch.preview,
        cron_expression="30 1 * * *",
        timezone="America/New_York",
        after=dt.datetime(2026, 10, 31, 12, tzinfo=dt.UTC),
        count=2,
    )
    first = fold["items"][0]
    assert first["local"] == "2026-11-01T01:30" and first["reason"] == "DST_FOLD"
    assert first["utc"] == "2026-11-01T05:30:00.000Z"  # the earlier of the two instants
    keys = [i["occurrence_key"] for i in fold["items"]]
    assert len(keys) == len(set(keys))


def test_lifecycle_and_version_pinning(engine: Engine, seed: Seed) -> None:
    """V-P5-22 / V-P5-33: legal transitions only; existing Runs keep their pinned version."""
    schedule_id = _create(engine, seed, "s-life-1", name="Lifecycle")
    assert seed.run_expect(
        engine, sch.ResumeSchedule(schedule_id), seed.owner, "l-resume-0", CLOCK
    ) == ("SCHEDULE_TRANSITION_INVALID")
    seed.run(engine, sch.EnableSchedule(schedule_id), seed.owner, "l-enable-1", CLOCK)
    assert seed.read(engine, sch.get_schedule, schedule_id)["status"] == "ENABLED"

    # materialize one Run, then change the Schedule: the Run keeps version 1
    clock = FixedClock(dt.datetime(2026, 3, 2, 23, 55, tzinfo=dt.UTC))
    with Session(engine) as s, s.begin():
        schedule = st.load_schedule(s, seed.ws, schedule_id)
        assert schedule is not None
        version = st.load_version(s, schedule.current_version_id)  # type: ignore[arg-type]
        assert version is not None
        planner.plan_schedule(
            s,
            seed.ctx(s, seed.owner, "plan", clock).store,
            clock,
            schedule=schedule,
            version=version,
            horizon_s=3600,
        )
    before = run_rows(engine, schedule_id)
    assert before and before[0]["version_hash"] == version.snapshot_hash

    seed.run(
        engine,
        sch.CommitScheduleVersion(schedule_id, {"max_duration_seconds": 1800, "name": "Renamed"}),
        seed.owner,
        "l-update-1",
        CLOCK,
    )
    view = seed.read(engine, sch.get_schedule, schedule_id)
    assert view["current_version"]["version"] == 2 and view["name"] == "Renamed"
    assert [v["version"] for v in view["versions"]] == [1, 2]
    after = run_rows(engine, schedule_id)
    assert [r["version_hash"] for r in after] == [r["version_hash"] for r in before]
    assert view["current_version"]["snapshot_hash"] != before[0]["version_hash"]

    assert (
        seed.run_expect(
            engine, sch.CommitScheduleVersion(schedule_id, {}), seed.owner, "l-noop", CLOCK
        )
        == "SCHEDULE_NO_CHANGES"
    )
    assert (
        seed.run_expect(
            engine,
            sch.CommitScheduleVersion(schedule_id, {"status": "ENABLED"}),
            seed.owner,
            "l-unknown",
            CLOCK,
        )
        == "SCHEDULE_FIELD_UNKNOWN"
    )

    seed.run(engine, sch.PauseSchedule(schedule_id), seed.owner, "l-pause-1", CLOCK)
    seed.run(engine, sch.ResumeSchedule(schedule_id), seed.owner, "l-resume-1", CLOCK)
    seed.run(engine, sch.DisableSchedule(schedule_id), seed.owner, "l-disable-1", CLOCK)
    assert seed.read(engine, sch.get_schedule, schedule_id)["status"] == "DISABLED"
    assert event_types(engine, schedule_id) == [
        "SCHEDULE_CREATED",
        "SCHEDULE_ENABLED",
        "SCHEDULE_UPDATED",
        "SCHEDULE_PAUSED",
        "SCHEDULE_RESUMED",
        "SCHEDULE_DISABLED",
    ]
    for cmd, key in (
        (sch.EnableSchedule(schedule_id), "l-enable-2"),
        (sch.PauseSchedule(schedule_id), "l-pause-2"),
    ):
        assert seed.run_expect(engine, cmd, seed.owner, key, CLOCK) == "SCHEDULE_TRANSITION_INVALID"
    # disabling cancelled the pending Run and froze the version
    assert {r["status"] for r in run_rows(engine, schedule_id)} == {"CANCELLED"}
    assert (
        seed.run_expect(
            engine,
            sch.CommitScheduleVersion(schedule_id, {"name": "x"}),
            seed.owner,
            "l-frozen",
            CLOCK,
        )
        == "SCHEDULE_STATUS_INVALID"
    )


def test_manual_runs_retry_and_cancel(engine: Engine, seed: Seed) -> None:
    """V-P5-31/32/34/35: manual Run isolation, idempotent retry, cancel rules."""
    schedule_id = _create(engine, seed, "s-run-1", name="Manual", cron_expression="0 9 * * *")
    seed.run(engine, sch.EnableSchedule(schedule_id), seed.owner, "r-enable-1", CLOCK)

    first = seed.run(engine, sch.RunScheduleNow(schedule_id), seed.owner, "r-now-1", CLOCK)
    again = seed.run(engine, sch.RunScheduleNow(schedule_id), seed.owner, "r-now-1", CLOCK)
    assert again.resource_id == first.resource_id and again.replayed  # same key: one Run
    other = seed.run(engine, sch.RunScheduleNow(schedule_id), seed.owner, "r-now-2", CLOCK)
    assert other.resource_id != first.resource_id
    manual = [r for r in run_rows(engine, schedule_id) if r["run_kind"] == "MANUAL"]
    assert len(manual) == 2 and all(r["occurrence_key"] is None for r in manual)

    # a scheduled occurrence in the same minute stays a separate Run (V-P5-35)
    clock = FixedClock(dt.datetime(2026, 3, 2, 23, 55, tzinfo=dt.UTC))
    with Session(engine) as s, s.begin():
        schedule = st.load_schedule(s, seed.ws, schedule_id)
        assert schedule is not None and schedule.current_version_id is not None
        version = st.load_version(s, schedule.current_version_id)
        assert version is not None
        planner.plan_schedule(
            s,
            seed.ctx(s, seed.owner, "plan", clock).store,
            clock,
            schedule=schedule,
            version=version,
            horizon_s=3600,
        )
    kinds = [r["run_kind"] for r in run_rows(engine, schedule_id)]
    assert kinds.count("SCHEDULED") == 1 and kinds.count("MANUAL") == 2

    # cancel: pending immediately, terminal is a conflict (V-P5-31/32)
    run_id = str(first.resource_id)
    cancelled = seed.run(engine, sch.CancelScheduleRun(run_id), seed.owner, "r-cancel-1", CLOCK)
    assert cancelled.data["status"] == "CANCELLED"
    assert seed.run_expect(
        engine, sch.CancelScheduleRun(run_id), seed.owner, "r-cancel-2", CLOCK
    ) == ("RUN_TERMINAL_CONFLICT")
    assert "RUN_CANCELLED" in event_types(engine, run_id)

    # retry of a terminal Run: exactly one new RETRY Run, idempotent per request key (V-P5-34)
    retry = seed.run(engine, sch.RetryScheduleRun(run_id), seed.owner, "r-retry-1", CLOCK)
    same = seed.run(engine, sch.RetryScheduleRun(run_id), seed.owner, "r-retry-1", CLOCK)
    assert same.resource_id == retry.resource_id and same.replayed
    retries = [r for r in run_rows(engine, schedule_id) if r["run_kind"] == "RETRY"]
    assert len(retries) == 1 and retries[0]["retry_of_run_id"] == run_id
    assert seed.read(engine, sch.run_view, run_id)["status"] == "CANCELLED"  # original untouched

    # a running Run only enters CANCEL_REQUESTED
    running_id = str(other.resource_id)
    with Session(engine) as s, s.begin():
        st.update_run(s, running_id, CLOCK.now(), status="RUNNING")
    requested = seed.run(engine, sch.CancelScheduleRun(running_id), seed.owner, "r-cancel-3", CLOCK)
    assert requested.data["status"] == "CANCEL_REQUESTED"
    assert "RUN_CANCEL_REQUESTED" in event_types(engine, running_id)


def test_schedule_and_run_subjects_are_active(
    engine: Engine, seed: Seed, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """V-P5-36: Approval `schedule`/`run` subjects and the ScheduleRun ArtifactLink handler."""
    schedule_id = _create(engine, seed, "s-subj-1", name="Subjects", cron_expression="0 10 * * *")
    seed.run(engine, sch.EnableSchedule(schedule_id), seed.owner, "sub-enable", CLOCK)
    run_id = str(
        seed.run(engine, sch.RunScheduleNow(schedule_id), seed.owner, "sub-now", CLOCK).resource_id
    )

    handler = artifact_links.REGISTRY.get("schedule_run")
    assert handler.active and handler.activating_phase == 5
    with Session(engine) as s:
        assert handler.exists(s, str(seed.ws), run_id) is True
        assert handler.exists(s, str(seed.ws), "run-missing") is False
        with pytest.raises(artifact_links.ArtifactLinkError) as mismatch:
            handler.exists(s, str(uuid.uuid4()), run_id)
        assert mismatch.value.code == "WORKSPACE_MISMATCH"

    # an Artifact links to the Run and appears in the Run's links
    artifact_id = str(
        seed.run(
            engine,
            art.RegisterArtifact(filename="digest.md", mime="text/markdown", content=b"# digest"),
            seed.owner,
            "sub-art-1",
            CLOCK,
            extras={"artifact_storage": ArtifactStorage(tmp_path_factory.mktemp("artifacts"))},
        ).resource_id
    )
    seed.run(
        engine,
        art.LinkArtifact(artifact_id, "schedule_run", run_id),
        seed.owner,
        "sub-link-1",
        CLOCK,
    )
    assert artifact_id in seed.read(engine, sch.run_view, run_id)["links"]["artifacts"]

    # Approval subjects `schedule` and `run` are usable in Phase 5
    for subject_type, subject_id, key in (
        ("schedule", schedule_id, "sub-apr-1"),
        ("run", run_id, "sub-apr-2"),
    ):
        result = seed.run(
            engine,
            RequestApproval(subject_type, subject_id, "tool:schedule_run_now"),
            seed.owner,
            key,
            CLOCK,
        )
        assert result.resource_id.startswith("apr-")
    with Session(engine) as s:
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM approval_grants WHERE subject_type IN ('schedule','run') "
                    "AND workspace_id = :w"
                ),
                {"w": seed.ws},
            ).scalar_one()
            == 2
        )
    with pytest.raises(bus.CommandError) as exc:
        seed.run(
            engine,
            RequestApproval("run", "run-missing", "tool:schedule_run_now"),
            seed.owner,
            "sub-apr-3",
            CLOCK,
        )
    assert exc.value.code == "SUBJECT_NOT_FOUND"
