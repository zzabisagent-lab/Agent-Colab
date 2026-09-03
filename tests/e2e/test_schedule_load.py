"""V-P5-27 (finding F-P5-004): wall-clock start-delay measurement under the development plan
§21.1 normal profile.

Two real scheduler worker processes (``python -m server.schedules.worker``) poll a database that
holds the §21.1 population — 50 Human accounts, 20 Agents, 100 channels — and 100 ENABLED
Schedules whose crons make 20 occurrences due every minute. Nothing here is simulated: the clock
is wall-clock, the planner and runners are the shipped ones, and the start delay is measured as
``started_at - scheduled_for`` from the rows the workers wrote. DB CPU is sampled from the
PostgreSQL server processes so the measurement can be read against the §21.1 "DB CPU < 70 %"
condition. The alert half (p95 above 60 s must raise) is asserted here too, so this file carries
the whole criterion.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.policy.repository import PostgresPolicyRepository
from server.schedules import metrics
from tests.integration.schedule_seed import Seed

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
SCHEDULES = 100  # §21.1 normal profile: 100 active Schedules
DUE_PER_MINUTE = 20  # 100 schedules on a 5-minute cycle → 20 due per minute
HUMANS, AGENTS, CHANNELS = 50, 20, 100
WINDOW_S = int(os.environ.get("AGENT_COLAB_LOAD_WINDOW_S", "300"))  # 5 minutes of wall clock
P95_LIMIT_S = 60.0
DB_CPU_LIMIT = 70.0
TICKS = os.sysconf("SC_CLK_TCK")


# ---------------------------------------------------------------- DB CPU sampling


def _postgres_pids() -> list[int]:
    """Every PostgreSQL server process of this host (postmaster plus its backends)."""
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if comm == "postgres":
            pids.append(int(entry.name))
    return pids


def _cpu_ticks(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        try:
            fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").rsplit(") ", 1)
        except OSError:
            continue
        if len(fields) != 2:
            continue
        parts = fields[1].split()
        total += int(parts[11]) + int(parts[12])  # utime + stime
    return total


class DbCpu:
    """Samples PostgreSQL CPU use as a percentage of one core-second per wall second."""

    def __init__(self) -> None:
        self.samples: list[float] = []
        self._pids = _postgres_pids()
        self._ticks = _cpu_ticks(self._pids)
        self._at = time.monotonic()

    def sample(self) -> None:
        self._pids = _postgres_pids()  # backends come and go
        now, ticks = time.monotonic(), _cpu_ticks(self._pids)
        elapsed = now - self._at
        if elapsed > 0 and ticks >= self._ticks:
            busy = (ticks - self._ticks) / TICKS
            self.samples.append(100.0 * busy / elapsed / (os.cpu_count() or 1))
        self._ticks, self._at = ticks, now

    @property
    def mean(self) -> float:
        return sum(self.samples) / len(self.samples) if self.samples else 0.0

    @property
    def peak(self) -> float:
        return max(self.samples, default=0.0)


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile, matching server/schedules/metrics.py."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-pct * len(ordered) // 100))))
    return ordered[rank - 1]


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def population(engine: Engine) -> Seed:
    """The §21.1 normal-profile population plus 100 ENABLED Schedules due 20 times a minute."""
    seed = Seed(f"load{uuid.uuid4().hex[:6]}")
    seed.create(engine)
    with Session(engine) as s, s.begin():
        for i in range(HUMANS):
            _account(s, seed, f"acct-{seed.tag}-h{i:03d}", "human")
        for i in range(AGENTS):
            account = _account(s, seed, f"acct-{seed.tag}-a{i:03d}", "agent")
            agent_id = f"agent-{seed.tag}-{i:03d}"
            s.execute(
                text(
                    "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, "
                    "status, display_name, online, capacity, last_heartbeat_at) "
                    "VALUES (:i, :g, :w, :a, 'mcp', 'active', :g, true, 50, :now)"
                ),
                {
                    "i": uuid.uuid4(),
                    "g": agent_id,
                    "w": seed.ws,
                    "a": account,
                    "now": dt.datetime.now(dt.UTC),
                },
            )
            s.execute(
                text("INSERT INTO agent_capabilities (agent_id, capability_id) VALUES (:g, :c)"),
                {"g": agent_id, "c": "cap-research"},
            )
            s.execute(
                text(
                    "INSERT INTO channel_members (channel_id, account_id, permissions) "
                    "VALUES (:c, :a, CAST(:p AS jsonb))"
                ),
                {"c": seed.channel, "a": account, "p": '["read", "write"]'},
            )
        for i in range(CHANNELS):
            s.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, channel_type, "
                    "display_name) VALUES (:i, :c, :w, 'work', :c)"
                ),
                {"i": uuid.uuid4(), "c": f"chan-{seed.tag}-{i:03d}", "w": seed.ws},
            )
        s.execute(  # the seeded Agent is online too, as a heartbeat would leave it
            text(
                "UPDATE agents SET online = true, capacity = 50, last_heartbeat_at = :now "
                "WHERE workspace_id = :w"
            ),
            {"now": dt.datetime.now(dt.UTC), "w": seed.ws},
        )
        _grant_execution_role(s, seed)
    _create_schedules(engine, seed)
    return seed


def _grant_execution_role(session: Session, seed: Seed) -> None:
    """Authorize through the real Policy Engine, as in production: the execution principal may
    create and delegate Tasks, and every candidate Agent may accept them (routing asks the
    Authorizer for ``task.accept`` before an Agent is eligible)."""
    repo = PostgresPolicyRepository()
    since = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    runner_role, agent_role = f"role-{seed.tag}-runner", f"role-{seed.tag}-agent"
    repo.create_role(session, seed.ws, runner_role, "scheduled work")
    repo.commit_role_version(
        session,
        runner_role,
        ["task.create", "task.delegate", "task.read"],
        [],
        {},
        seed.accounts[seed.owner],
    )
    repo.assign_role(
        session, seed.accounts[seed.owner], runner_role, seed.accounts[seed.owner], since
    )
    repo.create_role(session, seed.ws, agent_role, "scheduled work assignee")
    repo.commit_role_version(
        session,
        agent_role,
        ["task.accept", "task.progress", "task.read", "task.submit"],
        [],
        {},
        seed.accounts[seed.owner],
    )
    rows = session.execute(
        text("SELECT account_id FROM agents WHERE workspace_id = :w"), {"w": seed.ws}
    ).all()
    for (account_uuid,) in rows:
        repo.assign_role(session, account_uuid, agent_role, seed.accounts[seed.owner], since)


def _account(session: Session, seed: Seed, account_id: str, account_type: str) -> uuid.UUID:
    acc = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
            "VALUES (:i, :a, :w, :t, :a)"
        ),
        {"i": acc, "a": account_id, "w": seed.ws, "t": account_type},
    )
    return acc


def _create_schedules(engine: Engine, seed: Seed) -> None:
    """100 Schedules on a five-minute cycle, offset so 20 fall due in every minute."""
    import json

    now = dt.datetime.now(dt.UTC)
    with Session(engine) as s, s.begin():
        for i in range(SCHEDULES):
            offset = i % 5  # five offsets, 20 schedules each: 20 due per minute
            schedule_id = f"sch-load-{i:03d}"
            version_id = f"schv-{uuid.uuid4().hex[:16]}"
            template = {
                "schema_id": "action-template.v1",
                "action": "task_create",
                "input": {"title": f"load {i:03d}", "domain": "research", "risk": "LOW"},
            }
            selection = {"mode": "capability", "required_capabilities": ["cap-research"]}
            s.execute(
                text(
                    "INSERT INTO schedules (id, schedule_id, workspace_id, name, status, "
                    "last_planned_until, created_by, created_at, updated_at) VALUES (:i, :s, :w, "
                    ":n, 'ENABLED', :p, :by, :now, :now)"
                ),
                {
                    "i": uuid.uuid4(),
                    "s": schedule_id,
                    "w": seed.ws,
                    "n": f"load {i:03d}",
                    "p": now,
                    "by": seed.accounts[seed.owner],
                    "now": now,
                },
            )
            row = s.execute(
                text(
                    "INSERT INTO schedule_versions (id, schedule_version_id, schedule_id, version, "
                    "name, channel_id, cron_expression, timezone, execution_principal_id, "
                    "agent_selection, action_template, concurrency_policy, missed_run_policy, "
                    "backfill_limit, backfill_window_seconds, max_duration_seconds, "
                    "min_interval_minutes, retry_policy, budget_policy, documentation_policy, "
                    "snapshot_hash, created_by, created_at) VALUES (:i, :v, :s, 1, :n, :c, :cron, "
                    "'UTC', :p, CAST(:sel AS jsonb), CAST(:tpl AS jsonb), 'ALLOW', 'SKIP', 0, 0, "
                    "3600, 1, CAST(:rp AS jsonb), CAST(:bp AS jsonb), CAST(:dp AS jsonb), :h, "
                    ":by, :now) RETURNING id"
                ),
                {
                    "i": uuid.uuid4(),
                    "v": version_id,
                    "s": schedule_id,
                    "n": f"load {i:03d}",
                    "c": seed.channel,
                    "cron": f"{offset}-59/5 * * * *",
                    "p": seed.accounts[seed.owner],
                    "sel": json.dumps(selection),
                    "tpl": json.dumps(template),
                    "rp": json.dumps({"max_attempts": 1, "backoff_seconds": [1]}),
                    "bp": json.dumps({"per_run_cost_units": 100000, "daily_cost_units": 100000000}),
                    "dp": json.dumps({"draft": False}),
                    "h": f"{i:064x}",
                    "by": seed.accounts[seed.owner],
                    "now": now,
                },
            ).scalar_one()
            s.execute(
                text("UPDATE schedules SET current_version_id = :v WHERE schedule_id = :s"),
                {"v": row, "s": schedule_id},
            )


def _start_worker(
    database_url: str, workspace: str, runner_id: str, delay: float
) -> subprocess.Popen[bytes]:
    env = {**os.environ, "AGENT_COLAB_DATABASE_URL": database_url}
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "server.schedules.worker",
            "--workspace",
            workspace,
            "--runner-id",
            runner_id,
            "--start-delay-s",
            str(delay),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


# ---------------------------------------------------------------- the measurement


def test_start_delay_p95_under_normal_load(
    engine: Engine, population: Seed, database_url: str
) -> None:
    seed = population
    started_at = dt.datetime.now(dt.UTC)
    workers = [
        _start_worker(database_url, str(seed.ws), "load-runner-1", 0.0),
        _start_worker(database_url, str(seed.ws), "load-runner-2", 7.0),  # staggered poll beat
    ]
    cpu = DbCpu()
    try:
        deadline = time.monotonic() + WINDOW_S
        while time.monotonic() < deadline:
            time.sleep(5.0)
            cpu.sample()
            for worker in workers:
                assert worker.poll() is None, f"a scheduler worker exited early: {worker.args}"
    finally:
        for worker in workers:
            worker.terminate()
        for worker in workers:
            try:
                worker.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                worker.kill()
        for worker in workers:
            err = (worker.stderr.read() if worker.stderr else b"").decode(errors="replace")
            if err.strip():
                print(f"\nworker {worker.args[-3]} stderr:\n{err[-2000:]}")

    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT run_id, occurrence_key, status, scheduled_for, started_at "
                "FROM schedule_runs WHERE workspace_id = :w AND started_at IS NOT NULL "
                "AND scheduled_for >= :from"
            ),
            {"w": seed.ws, "from": started_at},
        ).all()
        duplicates = s.execute(
            text(
                "SELECT count(*) FROM (SELECT schedule_id, occurrence_key FROM schedule_runs "
                "WHERE workspace_id = :w AND occurrence_key IS NOT NULL "
                "GROUP BY schedule_id, occurrence_key HAVING count(*) > 1) d"
            ),
            {"w": seed.ws},
        ).scalar_one()
        snapshot = metrics.snapshot(s, seed.ws, dt.datetime.now(dt.UTC), window_s=WINDOW_S * 2)
        by_status = s.execute(
            text(
                "SELECT status, count(*), min(error_code) FROM schedule_runs "
                "WHERE workspace_id = :w GROUP BY status ORDER BY status"
            ),
            {"w": seed.ws},
        ).all()

    delays = [(r[4] - r[3]).total_seconds() for r in rows]
    p50, p95, worst = _percentile(delays, 50), _percentile(delays, 95), max(delays, default=0.0)
    print(  # the evidence log carries the measured numbers
        f"\nV-P5-27 wall-clock load: schedules={SCHEDULES} due/min={DUE_PER_MINUTE} runners=2 "
        f"window={WINDOW_S}s runs_started={len(delays)}\n"
        f"  start delay p50={p50:.1f}s p95={p95:.1f}s max={worst:.1f}s\n"
        f"  db cpu mean={cpu.mean:.1f}% peak={cpu.peak:.1f}% samples={len(cpu.samples)}\n"
        f"  metrics snapshot start_delay_p95={snapshot['start_delay_s']['p95']}s "
        f"duplicates_prevented={snapshot['duplicates_prevented']}\n"
        f"  runs by status: {[(r[0], r[1], r[2]) for r in by_status]}"
    )
    assert len(delays) >= DUE_PER_MINUTE, f"only {len(delays)} Runs started in {WINDOW_S}s"
    assert duplicates == 0, "an occurrence key produced more than one Run"
    assert p95 <= P95_LIMIT_S, f"start delay p95 {p95:.1f}s exceeds {P95_LIMIT_S}s"
    assert cpu.mean < DB_CPU_LIMIT, f"mean DB CPU {cpu.mean:.1f}% is not below {DB_CPU_LIMIT}%"


def test_start_delay_alert_fires_above_the_threshold(engine: Engine, population: Seed) -> None:
    """The alert half of V-P5-27: exactly 60 s does not alert, above it does."""
    base = {
        "stuck_leases": 0,
        "failures": 0,
        "timed_out": 0,
        "succeeded": 0,
        "budget_alerts": 0,
        "failure_rate": 0.0,
    }
    at_limit = {**base, "start_delay_s": {"p95": 60.0}}
    above = {**base, "start_delay_s": {"p95": 60.5}}
    assert not [a for a in metrics.alerts(at_limit) if a["key"] == "START_DELAY_P95_ABOVE_60S"]
    raised = [a for a in metrics.alerts(above) if a["key"] == "START_DELAY_P95_ABOVE_60S"]
    assert len(raised) == 1 and raised[0]["severity"] == "warning"
