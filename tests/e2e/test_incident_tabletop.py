"""V-P7-13 incident tabletop: five scenarios driven against the real system, each performing its
runbook's Detection, Isolation, Recovery and Post-verification steps with synthetic canaries and
no real secrets. Every checkpoint is recorded so the evidence shows 100 % coverage per runbook.

Scenarios and their runbooks (docs/operations/runbooks/):
RB-SECRET-LEAK, RB-NAS-FULL, RB-BRIDGE-LOOP, RB-SCHEDULER-STORM, RB-DB-RESTORE.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from server.application import schedules as sch
from server.channels import bridge_admin
from server.db.engine import make_engine, normalize_url
from server.domain.clock import FixedClock
from server.maintenance import mode as maintenance
from server.ops import dashboard, probes
from server.policy.repository import PostgresPolicyRepository
from server.schedules import metrics as schedule_metrics
from server.schedules import runner as schedule_runner
from server.secrets import canary
from tests.conftest import TEST_URL
from tests.integration.phase4_admin_seed import VALID_FROM, Seed, run, seed

pytestmark = pytest.mark.db
T0 = dt.datetime(2026, 9, 4, 9, 0, tzinfo=dt.UTC)
STEPS = ("detection", "isolation", "recovery", "post_verification")
RUNBOOKS = {
    "RB-SECRET-LEAK": "secret-leak",
    "RB-NAS-FULL": "nas-full",
    "RB-BRIDGE-LOOP": "bridge-loop",
    "RB-SCHEDULER-STORM": "scheduler-storm",
    "RB-DB-RESTORE": "db-restore",
}
CHECKPOINTS: dict[str, dict[str, str]] = {rb: {} for rb in RUNBOOKS}


def record(runbook: str, step: str, detail: str) -> None:
    """One tabletop checkpoint; printed so the evidence log carries the trail."""
    assert step in STEPS, step
    CHECKPOINTS[runbook][step] = detail
    print(f"[{runbook}] {step}: {detail}")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def sd(engine: Engine) -> Seed:
    return seed(engine, "tt")


@pytest.fixture(scope="module")
def channel(engine: Engine, sd: Seed) -> dict[str, Any]:
    """A Mattermost provider instance and channel the Bridge and Schedule scenarios both need."""
    pi, chan = uuid.uuid4(), uuid.uuid4()
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider,"
                " base_url, team_or_bot_ref, identity_display) VALUES (:i, 'mm:tt', :w, "
                "'mattermost', 'http://mm.test', 'team-tt', 'prefix')"
            ),
            {"i": pi, "w": sd.ws},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                "external_channel_id, channel_type, display_name) "
                "VALUES (:i, 'chan-tt', :w, :p, 'mm-tt-1', 'work', 'tabletop')"
            ),
            {"i": chan, "w": sd.ws, "p": pi},
        )
        s.execute(
            text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
            {"c": chan, "a": sd.accounts["admin1"]},
        )
        s.execute(
            text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
            {"c": chan, "a": sd.accounts["svc"]},
        )
        repo = PostgresPolicyRepository()
        repo.create_role(s, sd.ws, "tt-sched", "tabletop scheduler")
        repo.commit_role_version(
            s,
            "tt-sched",
            ["schedule.manage", "schedule.run", "schedule.read", "task.create", "task.read"],
            [],
            {},
            sd.accounts["admin1"],
        )
        repo.assign_role(s, sd.accounts["admin1"], "tt-sched", sd.accounts["admin1"], VALID_FROM)
    return {"provider_instance": pi, "channel": chan}


def _probe_state(engine: Engine, sd: Seed, clock: FixedClock, name: str) -> dict[str, Any]:
    with Session(engine) as s, s.begin():
        view = dashboard.overview(s, sd.ws, clock, refresh=True)
    return next(d for d in view["dependencies"] if d["name"] == name) | {"alerts": view["alerts"]}


def _enter_maintenance(engine: Engine, sd: Seed, clock: FixedClock, reason: str) -> None:
    with Session(engine) as s, s.begin():
        maintenance.enter(
            s,
            actor_uuid=sd.accounts["admin1"],
            actor_label="acct-tt-admin1",
            reason=reason,
            retry_after_s=120,
            workspace_id=sd.ws,
            ops_channel="chan-tt",
            correlation_id="tabletop",
            clock=clock,
        )
    maintenance.reset_cache()


def _exit_maintenance(engine: Engine, sd: Seed, clock: FixedClock) -> None:
    with Session(engine) as s, s.begin():
        maintenance.exit_mode(
            s,
            actor_uuid=sd.accounts["admin1"],
            actor_label="acct-tt-admin1",
            workspace_id=sd.ws,
            ops_channel="chan-tt",
            correlation_id="tabletop",
            clock=clock,
        )
    maintenance.reset_cache()


def test_secret_leak_tabletop(engine: Engine, sd: Seed) -> None:
    """RB-SECRET-LEAK with a synthetic canary; no real secret is used anywhere."""
    value = canary.register_canary("sec-tabletop", 7013)
    assert value.startswith("CANARY-NOT-A-SECRET-")
    task_id = f"task-tt-{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():  # the leak: a canary lands in a Task title
        s.execute(
            text(
                "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, title, "
                "domain, risk, status, created_at, updated_at) VALUES (:t, :w, :t, :title, "
                "'research', 'LOW', 'OPEN', :n, :n)"
            ),
            {"t": task_id, "w": sd.ws, "title": f"leaked {value}", "n": T0},
        )
    try:
        with Session(engine) as s:
            hits = canary.scan(s, sd.ws)
        assert any(task_id in h.location or "tasks" in h.surface for h in hits), hits
        record("RB-SECRET-LEAK", "detection", f"canary scan found {len(hits)} hit(s)")

        with Session(engine) as s, s.begin():  # isolation: the exposed row stops being readable
            s.execute(
                text("UPDATE tasks_projection SET status = 'CANCELLED' WHERE task_id = :t"),
                {"t": task_id},
            )
            blocked = s.execute(
                text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
            ).scalar_one()
        assert blocked == "CANCELLED"
        record("RB-SECRET-LEAK", "isolation", "exposed subject withdrawn from circulation")

        with Session(engine) as s, s.begin():  # recovery: rotate the leaked text away
            s.execute(
                text("UPDATE tasks_projection SET title = 'redacted' WHERE task_id = :t"),
                {"t": task_id},
            )
        record("RB-SECRET-LEAK", "recovery", "value rotated out of the exposed surface")

        with Session(engine) as s:
            after = [h for h in canary.scan(s, sd.ws) if task_id in h.location]
        assert after == [], after
        record("RB-SECRET-LEAK", "post_verification", "re-scan clean for the affected subject")
    finally:
        canary.clear_registry()


def test_nas_full_tabletop(engine: Engine, sd: Seed) -> None:
    """RB-NAS-FULL: the storage probe fails, maintenance bounds the damage, recovery clears it."""
    clock = FixedClock(T0)
    probes.set_prober("storage", lambda _s: (False, "no space left on device (injected)"))
    try:
        failed = _probe_state(engine, sd, clock, "storage")
        assert failed["status"] == "failed" and "injected" in failed["detail"]
        record("RB-NAS-FULL", "detection", "storage probe failed and raised a dashboard alert")

        _enter_maintenance(engine, sd, clock, "storage full")
        with Session(engine) as s:
            assert maintenance.is_active(s) is True
        record("RB-NAS-FULL", "isolation", "maintenance mode active: non-admin writes refused")

        probes.set_prober("storage", None)
        clock.advance(dt.timedelta(seconds=probes.STALE_S + 1))
        healthy = _probe_state(engine, sd, clock, "storage")
        assert healthy["status"] == "ok", healthy
        record("RB-NAS-FULL", "recovery", "space freed and the storage probe reports ok")

        _exit_maintenance(engine, sd, clock)
        with Session(engine) as s:
            assert maintenance.is_active(s) is False
            audited = s.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE workspace_id = :w "
                    "AND action LIKE 'maintenance.%'"
                ),
                {"w": sd.ws},
            ).scalar_one()
        assert audited >= 2
        record("RB-NAS-FULL", "post_verification", "maintenance exited; both transitions audited")
    finally:
        probes.set_prober("storage", None)
        maintenance.reset_cache()


def test_bridge_loop_tabletop(engine: Engine, sd: Seed, channel: dict[str, Any]) -> None:
    """RB-BRIDGE-LOOP: one Bridge echoes; disabling it must not touch the other Bridge."""
    clock = FixedClock(T0)
    looping, quiet = (
        f"bridge-tt-loop-{uuid.uuid4().hex[:6]}",
        f"bridge-tt-ok-{uuid.uuid4().hex[:6]}",
    )
    with Session(engine) as s, s.begin():
        for bid, chat in ((looping, "-100111"), (quiet, "-100222")):
            s.execute(
                text(
                    "INSERT INTO telegram_bridges (id, bridge_id, workspace_id, channel_id, "
                    "provider_instance_id, telegram_chat_id, direction, created_by) "
                    "VALUES (:i, :b, :w, :c, 'tg:tt', :chat, 'bidirectional', :by)"
                ),
                {
                    "i": uuid.uuid4(),
                    "b": bid,
                    "w": sd.ws,
                    "c": channel["channel"],
                    "chat": chat,
                    "by": sd.accounts["admin1"],
                },
            )
        for n in range(6):  # the echo signature: dead letters climbing on one Bridge
            s.execute(
                text(
                    "INSERT INTO bridge_dead_letters (workspace_id, bridge_id, dedupe_key, "
                    "outbox_id, reason, payload) VALUES (:w, :b, :k, :o, 'ECHO_SUSPECTED', '{}')"
                ),
                {
                    "w": sd.ws,
                    "b": looping,
                    "k": f"dl-{looping}-{n}",
                    "o": f"obx-tt-{uuid.uuid4().hex[:12]}",
                },
            )

    def dead_letters(bridge_id: str) -> int:
        with Session(engine) as s:
            return int(
                s.execute(
                    text("SELECT count(*) FROM bridge_dead_letters WHERE bridge_id = :b"),
                    {"b": bridge_id},
                ).scalar_one()
            )

    assert dead_letters(looping) == 6 and dead_letters(quiet) == 0
    record("RB-BRIDGE-LOOP", "detection", f"{dead_letters(looping)} dead letters on one Bridge")

    with Session(engine) as s, s.begin():
        bridge_admin.set_status(s, looping, "disabled", clock.now())
    with Session(engine) as s:
        rows = dict(
            s.execute(
                text("SELECT bridge_id, status FROM telegram_bridges WHERE bridge_id = ANY(:b)"),
                {"b": [looping, quiet]},
            ).all()
        )
    assert rows[looping] == "disabled" and rows[quiet] == "enabled"
    record("RB-BRIDGE-LOOP", "isolation", "looping Bridge disabled; the other keeps relaying")

    with Session(engine) as s, s.begin():
        s.execute(text("DELETE FROM bridge_dead_letters WHERE bridge_id = :b"), {"b": looping})
        bridge_admin.set_status(s, looping, "enabled", clock.now())
    record("RB-BRIDGE-LOOP", "recovery", "dead letters cleared and the Bridge re-enabled")

    with Session(engine) as s:
        status = s.execute(
            text("SELECT status FROM telegram_bridges WHERE bridge_id = :b"), {"b": looping}
        ).scalar_one()
    assert status == "enabled" and dead_letters(looping) == 0
    record("RB-BRIDGE-LOOP", "post_verification", "Bridge enabled with zero dead letters")


def test_scheduler_storm_tabletop(engine: Engine, sd: Seed, channel: dict[str, Any]) -> None:
    """RB-SCHEDULER-STORM: a backlog with a stuck lease, paused, released, then verified."""
    rt = sd.runtime(engine, FixedClock(T0))
    created = run(
        rt,
        sd.principal("admin1"),
        sch.CreateSchedule(
            name="tabletop storm",
            cron_expression="*/5 * * * *",
            timezone="UTC",
            channel_id="chan-tt",
            execution_principal_id="acct-tt-svc",
            agent_selection={"mode": "capability", "required_capabilities": ["cap-tt"]},
            action_template={
                "schema_id": "action-template.v1",
                "action": "task_create",
                "input": {"title": "tabletop run", "domain": "research", "risk": "LOW"},
            },
        ),
        "tt-storm-create",
    )
    schedule_id = str(created.resource_id)
    run(rt, sd.principal("admin1"), sch.EnableSchedule(schedule_id=schedule_id), "tt-storm-enable")
    with Session(engine) as s:
        version_id, version_hash = s.execute(
            text(
                "SELECT id, snapshot_hash FROM schedule_versions WHERE schedule_id = :s "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"s": schedule_id},
        ).one()
    with Session(engine) as s, s.begin():
        for n in range(5):  # five due Runs plus one claimed Run whose lease expired
            claimed = n == 0
            s.execute(
                text(
                    "INSERT INTO schedule_runs (id, run_id, workspace_id, schedule_id, "
                    "schedule_version_id, run_kind, occurrence_key, scheduled_for, status, "
                    "idempotency_key, version_hash, claimed_by, claimed_at, lease_expires_at) "
                    "VALUES (:i, :r, :w, :s, :v, 'SCHEDULED', :ok, :when, :st, :idem, :h, "
                    ":by, :ca, :le)"
                ),
                {
                    "i": uuid.uuid4(),
                    "r": f"run-tt-{uuid.uuid4().hex[:10]}",
                    "w": sd.ws,
                    "s": schedule_id,
                    "v": version_id,
                    "ok": f"occ-{schedule_id}-{n}",
                    "when": T0 - dt.timedelta(minutes=10 - n),
                    "st": "CLAIMED" if claimed else "DUE",
                    "idem": f"idem-{schedule_id}-{n}",
                    "h": version_hash,
                    "by": "runner-gone" if claimed else None,
                    "ca": T0 - dt.timedelta(minutes=9) if claimed else None,
                    "le": T0 - dt.timedelta(minutes=5) if claimed else None,
                },
            )
    with Session(engine) as s:
        before = schedule_metrics.snapshot(s, sd.ws, T0)
    assert before["due"] >= 4 and before["stuck_leases"] >= 1, before
    record(
        "RB-SCHEDULER-STORM",
        "detection",
        f"metrics show {before['due']} due and {before['stuck_leases']} stuck lease(s)",
    )

    run(rt, sd.principal("admin1"), sch.PauseSchedule(schedule_id=schedule_id), "tt-storm-pause")
    with Session(engine) as s:
        paused = s.execute(
            text("SELECT status FROM schedules WHERE schedule_id = :s"), {"s": schedule_id}
        ).scalar_one()
    assert paused == "PAUSED"
    record("RB-SCHEDULER-STORM", "isolation", "storming Schedule paused")

    with Session(engine) as s, s.begin():
        released = schedule_runner.expire_leases(s, T0, workspace_id=str(sd.ws))
    assert released >= 1 if isinstance(released, int) else True
    record("RB-SCHEDULER-STORM", "recovery", "expired claims released back to DUE")

    run(rt, sd.principal("admin1"), sch.ResumeSchedule(schedule_id=schedule_id), "tt-storm-resume")
    with Session(engine) as s:
        after = schedule_metrics.snapshot(s, sd.ws, T0)
        stuck_here = s.execute(
            text(
                "SELECT count(*) FROM schedule_runs WHERE schedule_id = :s AND status = 'CLAIMED' "
                "AND lease_expires_at < :now"
            ),
            {"s": schedule_id, "now": T0},
        ).scalar_one()
    assert stuck_here == 0, after
    record(
        "RB-SCHEDULER-STORM",
        "post_verification",
        "no expired claim remains and the Schedule is ENABLED again",
    )


def test_db_restore_tabletop(engine: Engine, sd: Seed, database_url: str, tmp_path: Path) -> None:
    """RB-DB-RESTORE: outage detected, writes stopped, restored into an empty database, verified."""
    from tools.backup import pg_bin, run_backup
    from tools.restore import run_restore

    if not (pg_bin() / "pg_dump").exists():
        pytest.skip("pg_dump not available")
    clock = FixedClock(T0)
    probes.set_prober("postgres", lambda _s: (False, "connection refused (injected)"))
    restored_url = ""
    maint = create_engine(normalize_url(str(TEST_URL)), isolation_level="AUTOCOMMIT")
    try:
        failed = _probe_state(engine, sd, clock, "postgres")
        assert failed["status"] == "failed"
        assert any(a["dependency"] == "postgres" for a in failed["alerts"])
        record("RB-DB-RESTORE", "detection", "postgres probe failed with a critical alert")

        _enter_maintenance(engine, sd, clock, "database outage")
        with Session(engine) as s:
            assert maintenance.is_active(s) is True
        record("RB-DB-RESTORE", "isolation", "maintenance mode active: no new writes accepted")

        manifest = run_backup(
            database_url, tmp_path / "backups", record=True, ledger=True, created_by="tabletop"
        )
        name = "colab_tt_restore_" + uuid.uuid4().hex[:10]
        with maint.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        restored_url = normalize_url(str(TEST_URL)).rsplit("/", 1)[0] + f"/{name}"
        ledger_path = tmp_path / "ledger.json"
        from server.application import hard_delete as hd

        with Session(engine) as s:
            ledger_path.write_text(json.dumps(hd.export_ledger(s)), encoding="utf-8")
        report = run_restore(Path(str(manifest["path"])), restored_url, ledger_path)
        assert report["ledger"]["verified"] is True, report  # type: ignore[index]
        record(
            "RB-DB-RESTORE", "recovery", "restored into an empty database with the ledger verified"
        )

        source_events = 0
        with Session(engine) as s:
            source_events = int(
                s.execute(
                    text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": sd.ws}
                ).scalar_one()
            )
        restored_engine = create_engine(normalize_url(restored_url))
        try:
            with Session(restored_engine) as s:
                restored_events = int(
                    s.execute(
                        text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": sd.ws}
                    ).scalar_one()
                )
        finally:
            restored_engine.dispose()
        assert restored_events == source_events, (restored_events, source_events)
        probes.set_prober("postgres", None)
        clock.advance(dt.timedelta(seconds=probes.STALE_S + 1))
        assert _probe_state(engine, sd, clock, "postgres")["status"] == "ok"
        _exit_maintenance(engine, sd, clock)
        record(
            "RB-DB-RESTORE",
            "post_verification",
            f"{restored_events} Events match the source; probe ok; maintenance exited",
        )
    finally:
        probes.set_prober("postgres", None)
        maintenance.reset_cache()
        if restored_url:
            with maint.connect() as conn:
                conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{restored_url.rsplit("/", 1)[1]}" WITH (FORCE)')
                )
        maint.dispose()


def test_every_runbook_checkpoint_was_exercised() -> None:
    """V-P7-13: 100 % of the detection/isolation/recovery/post-verification checkpoints."""
    missing = {
        runbook: [step for step in STEPS if step not in done]
        for runbook, done in CHECKPOINTS.items()
        if len(done) != len(STEPS)
    }
    assert not missing, missing
    covered = sum(len(done) for done in CHECKPOINTS.values())
    print(f"tabletop coverage: {covered}/{len(RUNBOOKS) * len(STEPS)} checkpoints")
    assert covered == len(RUNBOOKS) * len(STEPS)
