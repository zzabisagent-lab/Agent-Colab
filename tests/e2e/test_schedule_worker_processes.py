"""V-P5-08 and V-P5-24 with **real scheduler processes** (findings F-P5-001, F-P5-003).

Both criteria are about surviving a worker's death, so neither is exercised in-process here: a
``server.schedules.worker`` child is started with ``subprocess``, killed at a committed boundary
through the ``AGENT_COLAB_SCHEDULE_KILL_AFTER`` failpoint (``os._exit``, indistinguishable from
``SIGKILL`` for the database), and a peer worker recovers on the wall clock.

* ``test_crash_after_task_creation_leaves_exactly_one_task`` (V-P5-08) kills the worker in the
  instant after the transaction that created the Task committed, then lets a second worker run
  repeatedly: exactly one Task, one ``TASK_CREATED`` Event, one ``RUN_STARTED`` Event, one attempt
  row and one start notice survive.
* ``test_restart_recovery_after_claim_kill`` (V-P5-24) runs two workers, kills the claimant right
  after its claim committed and measures the wall-clock time until the peer completes the Run:
  within the claim lease plus two poll intervals, with no duplicate Task.

Scheduler settings use the smallest values §10A.1 allows (poll 5 s, lease 15 s), so each test
finishes in well under two minutes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.policy.repository import PostgresPolicyRepository
from tests.integration.schedule_seed import Seed

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
POLL_S = 5  # §10A.1 floor
LEASE_S = 15  # §10A.1: at least 3x the poll interval
RECOVERY_BUDGET_S = LEASE_S + 2 * POLL_S  # the criterion's bound
EXIT_KILLED = 137
ACTION_TEMPLATE: dict[str, Any] = {
    "schema_id": "action-template.v1",
    "action": "task_create",
    "input": {"title": "Worker crash probe", "domain": "research", "risk": "LOW"},
}


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


def _grant_execution_role(session: Session, seed: Seed, tag: str) -> None:
    """The worker runs the real Policy Engine, so the execution principal needs a real Role."""
    repo = PostgresPolicyRepository()
    role = f"role-{tag}-exec"
    repo.create_role(session, seed.ws, role, "schedule execution")
    repo.commit_role_version(
        session,
        role,
        ["task.create", "task.delegate", "task.read", "task.progress"],
        [],
        {},
        seed.accounts[seed.owner],
    )
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    repo.assign_role(session, seed.accounts[seed.owner], role, seed.accounts[seed.owner], now)


def _seed_due_schedule(engine: Engine, tag: str) -> tuple[Seed, str]:
    """A workspace with one ENABLED every-minute Schedule whose occurrence is already due."""
    seed = Seed(tag)
    seed.create(engine)
    schedule_id = f"sch-{tag}"
    now = dt.datetime.now(dt.UTC)
    content = {"schedule_id": schedule_id, "version": 1, "template": ACTION_TEMPLATE}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    sid, vid = uuid.uuid4(), uuid.uuid4()
    with Session(engine) as s, s.begin():
        _grant_execution_role(s, seed, tag)
        s.execute(
            text(
                "INSERT INTO schedules (id, schedule_id, workspace_id, name, status, created_by, "
                "last_planned_until) VALUES (:i, :s, :w, :n, 'ENABLED', :by, :planned)"
            ),
            {
                "i": sid,
                "s": schedule_id,
                "w": seed.ws,
                "n": schedule_id,
                "by": seed.accounts[seed.owner],
                "planned": now - dt.timedelta(seconds=180),
            },
        )
        s.execute(
            text(
                "INSERT INTO schedule_versions (id, schedule_version_id, schedule_id, version, "
                "name, channel_id, cron_expression, timezone, execution_principal_id, "
                "agent_selection, action_template, concurrency_policy, missed_run_policy, "
                "backfill_limit, backfill_window_seconds, max_duration_seconds, retry_policy, "
                "budget_policy, documentation_policy, starts_at, ends_at, snapshot_hash, "
                "created_by) VALUES (:i, :sv, :s, 1, :n, :c, '* * * * *', 'UTC', :p, "
                "CAST(:sel AS jsonb), CAST(:tpl AS jsonb), 'FORBID', 'RUN_ONCE', 0, 0, 3600, "
                "CAST('{\"max_attempts\": 3}' AS jsonb), CAST('{}' AS jsonb), "
                "CAST('{}' AS jsonb), NULL, :ends, :h, :by)"
            ),
            {
                "i": vid,
                "sv": f"sv-{schedule_id}-1",
                "s": schedule_id,
                "n": schedule_id,
                "c": seed.channel,
                "p": seed.accounts[seed.owner],
                # a fixed Agent keeps the crash tests about worker death, not routing (V-P5-16)
                "sel": json.dumps({"mode": "fixed", "agent_id": seed.agent_id}),
                "tpl": json.dumps(ACTION_TEMPLATE),
                # ends now: exactly one (missed) occurrence is executable, so any second Task
                # would be a duplicate rather than the next legitimate occurrence
                "ends": now,
                "h": digest,
                "by": seed.accounts[seed.owner],
            },
        )
        s.execute(
            text("UPDATE schedules SET current_version_id = :v WHERE schedule_id = :s"),
            {"v": vid, "s": schedule_id},
        )
        # scheduled actions are unclassified for the risk catalog and therefore need an Approval;
        # an unbounded Schedule-scoped grant keeps these tests about worker death (V-P5-18 covers
        # the approval rules themselves)
        s.execute(
            text(
                "INSERT INTO approval_grants (id, approval_id, workspace_id, subject_type, "
                "subject_id, action, risk, status, requested_by, valid_from, expires_at, "
                "max_uses, quorum_required, aggregate_seq) VALUES (:i, :a, :w, 'schedule', :s, "
                "'api:schedule_run', 'HIGH', 'APPROVED', :by, :from, :to, NULL, 1, 0)"
            ),
            {
                "i": uuid.uuid4(),
                "a": f"apr-{uuid.uuid4().hex[:12]}",
                "w": seed.ws,
                "s": schedule_id,
                "by": seed.accounts[seed.owner],
                "from": now - dt.timedelta(minutes=5),
                "to": now + dt.timedelta(hours=2),
            },
        )
    return seed, schedule_id


def _worker(
    database_url: str,
    workspace: uuid.UUID,
    runner_id: str,
    *,
    kill_after: str | None = None,
    max_ticks: int | None = None,
    start_delay_s: float = 0.0,
) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "AGENT_COLAB_SCHEDULER_POLL_S": str(POLL_S),
        "AGENT_COLAB_SCHEDULER_LEASE_S": str(LEASE_S),
    }
    env.pop("AGENT_COLAB_SCHEDULE_KILL_AFTER", None)
    if kill_after:
        env["AGENT_COLAB_SCHEDULE_KILL_AFTER"] = kill_after
    argv = [
        sys.executable,
        "-m",
        "server.schedules.worker",
        "--workspace",
        str(workspace),
        "--runner-id",
        runner_id,
        "--database-url",
        database_url,
        "--start-delay-s",
        str(start_delay_s),
    ]
    if max_ticks is not None:
        argv += ["--max-ticks", str(max_ticks)]
    return subprocess.Popen(
        argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def _stop(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _counts(
    engine: Engine, schedule_id: str, workspace_id: uuid.UUID | None = None
) -> dict[str, int]:
    with Session(engine) as s:
        run = s.execute(
            text("SELECT run_id, status, task_id FROM schedule_runs WHERE schedule_id = :s"),
            {"s": schedule_id},
        ).all()
        task_ids = [r[2] for r in run if r[2]]
        counts = {
            "runs": len(run),
            "tasks_with_id": len(task_ids),
            "tasks": s.execute(
                # scoped to this test's Workspace: the title is shared by every invocation
                text(
                    "SELECT count(*) FROM tasks_projection WHERE title = :t "
                    "AND (CAST(:w AS uuid) IS NULL OR workspace_id = CAST(:w AS uuid))"
                ),
                {"t": ACTION_TEMPLATE["input"]["title"], "w": workspace_id},
            ).scalar_one(),
            "task_created_events": s.execute(
                text(
                    "SELECT count(*) FROM events WHERE type = 'TASK_CREATED' AND aggregate_id "
                    "= ANY(:ids)"
                ),
                {"ids": task_ids or [""]},
            ).scalar_one(),
            "run_started_events": s.execute(
                text(
                    "SELECT count(*) FROM events WHERE type = 'RUN_STARTED' AND aggregate_id "
                    "= ANY(:ids)"
                ),
                {"ids": [r[0] for r in run] or [""]},
            ).scalar_one(),
            "attempts": s.execute(
                text("SELECT count(*) FROM schedule_run_attempts WHERE run_id = ANY(:ids)"),
                {"ids": [r[0] for r in run] or [""]},
            ).scalar_one(),
            "start_notices": s.execute(
                text(
                    "SELECT count(*) FROM schedule_notices WHERE kind = 'start' "
                    "AND run_id = ANY(:ids)"
                ),
                {"ids": [r[0] for r in run] or [""]},
            ).scalar_one(),
        }
    return {k: int(v) for k, v in counts.items()}


def _wait_for(check: Any, timeout_s: float, interval_s: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval_s)
    return last


def test_crash_after_task_creation_leaves_exactly_one_task(
    engine: Engine, database_url: str
) -> None:
    """V-P5-08: SIGKILL right after the Task-creating transaction commits, then recover."""
    seed, schedule_id = _seed_due_schedule(engine, f"wk{uuid.uuid4().hex[:6]}")
    victim = recoverer = None
    try:
        victim = _worker(
            database_url, seed.ws, "runner-crash", kill_after="task_created", max_ticks=4
        )
        stdout, stderr = victim.communicate(timeout=120)
        assert victim.returncode == EXIT_KILLED, (victim.returncode, stdout[-2000:], stderr[-2000:])
        assert '"killed_after": "task_created"' in stdout, stdout[-2000:]

        after_crash = _counts(engine, schedule_id, seed.ws)
        assert after_crash["tasks"] == 1, after_crash  # the Task survived the kill
        assert after_crash["runs"] == 1 and after_crash["tasks_with_id"] == 1, after_crash

        # a peer worker now runs several ticks over the same Schedule
        recoverer = _worker(database_url, seed.ws, "runner-recover", max_ticks=3)
        recoverer.communicate(timeout=180)
        after = _counts(engine, schedule_id, seed.ws)
        assert after["tasks"] == 1, after
        assert after["task_created_events"] == 1, after
        assert after["run_started_events"] == 1, after
        assert after["attempts"] == 1, after
        assert after["start_notices"] == 1, after
        assert after["runs"] == 1, after
    finally:
        _stop(victim)
        _stop(recoverer)


def test_restart_recovery_after_claim_kill(engine: Engine, database_url: str) -> None:
    """V-P5-24: two workers; the claimant is killed right after its claim committed."""
    seed, schedule_id = _seed_due_schedule(engine, f"wk{uuid.uuid4().hex[:6]}")
    victim = peer = None
    try:
        # the peer is already running (its first tick is delayed past the victim's claim)
        peer = _worker(database_url, seed.ws, "runner-b", start_delay_s=POLL_S + 3, max_ticks=12)
        victim = _worker(database_url, seed.ws, "runner-a", kill_after="claimed", max_ticks=4)
        stdout, stderr = victim.communicate(timeout=120)
        killed_at = time.monotonic()
        assert victim.returncode == EXIT_KILLED, (victim.returncode, stdout[-2000:], stderr[-2000:])
        assert '"killed_after": "claimed"' in stdout, stdout[-2000:]

        with Session(engine) as s:
            claimed = s.execute(
                text(
                    "SELECT status, claimed_by, task_id FROM schedule_runs WHERE schedule_id = :s"
                ),
                {"s": schedule_id},
            ).all()
        assert [r[0] for r in claimed] == ["CLAIMED"], claimed
        assert claimed[0][1] == "runner-a" and claimed[0][2] is None, claimed

        def _recovered() -> Any:
            with Session(engine) as s:
                return s.execute(
                    text(
                        "SELECT run_id, status, claimed_by, task_id FROM schedule_runs "
                        "WHERE schedule_id = :s AND task_id IS NOT NULL"
                    ),
                    {"s": schedule_id},
                ).all()

        rows = _wait_for(_recovered, timeout_s=RECOVERY_BUDGET_S + 20)
        elapsed = time.monotonic() - killed_at
        assert rows, f"no runner recovered within {RECOVERY_BUDGET_S + 20}s"
        print(f"recovery after claim kill: {elapsed:.1f}s (budget {RECOVERY_BUDGET_S}s)")
        assert elapsed <= RECOVERY_BUDGET_S, f"recovery took {elapsed:.1f}s > {RECOVERY_BUDGET_S}s"
        assert len(rows) == 1 and rows[0][2] == "runner-b", rows

        after = _counts(engine, schedule_id, seed.ws)
        assert after["runs"] == 1 and after["tasks"] == 1, after
        assert after["task_created_events"] == 1, after
        assert after["run_started_events"] == 1, after
    finally:
        _stop(victim)
        _stop(peer)
