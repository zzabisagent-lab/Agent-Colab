"""Wall-clock traffic driver and measurement for V-P7-03 and V-P7-04 (P7-04).

A real uvicorn server on a loopback port, real scheduler worker processes, and threads issuing
real HTTP writes and reads at the profile's rates. Nothing is simulated: latency is measured
around ``httpx`` calls, Event and Run counts come from the database, and PostgreSQL CPU is read
from ``/proc`` so the §21.1 "DB CPU < 70 %" context is measured rather than assumed.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import itertools
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.main import create_app
from server.secrets.envelope import new_master_key
from tests.load.population import Population
from tests.load.profile import Profile

ROOT = Path(__file__).resolve().parents[2]
TICKS = os.sysconf("SC_CLK_TCK")


# ---------------------------------------------------------------- database CPU sampling


def _postgres_pids() -> list[int]:
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text(encoding="utf-8").strip() == "postgres":
                pids.append(int(entry.name))
        except OSError:
            continue
    return pids


def _cpu_ticks(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        try:
            fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").rsplit(") ", 1)
        except OSError:
            continue
        if len(fields) == 2:
            parts = fields[1].split()
            total += int(parts[11]) + int(parts[12])
    return total


class DbCpu:
    """PostgreSQL CPU as a percentage of the host's cores, sampled between calls."""

    def __init__(self) -> None:
        self.samples: list[float] = []
        self._pids = _postgres_pids()
        self._ticks = _cpu_ticks(self._pids)
        self._at = time.monotonic()

    def sample(self) -> None:
        self._pids = _postgres_pids()
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


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile, matching server/schedules/metrics.py."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-pct * len(ordered) // 100))))
    return ordered[rank - 1]


# ---------------------------------------------------------------- measurement


@dataclass
class Report:
    profile: str
    seconds: float
    writes: int = 0
    reads: int = 0
    messages: int = 0
    write_latency_ms: list[float] = field(default_factory=list)
    read_latency_ms: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    events_before: int = 0
    events_after: int = 0
    runs_before: int = 0
    runs_after: int = 0
    duplicate_occurrences: int = 0
    duplicate_events: int = 0
    db_cpu_mean: float = 0.0
    db_cpu_peak: float = 0.0

    @property
    def requests(self) -> int:
        return sum(self.statuses.values())

    @property
    def server_errors(self) -> int:
        return sum(count for status, count in self.statuses.items() if status >= 500)

    @property
    def error_rate(self) -> float:
        return self.server_errors / self.requests if self.requests else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "seconds": round(self.seconds, 1),
            "requests": self.requests,
            "writes": self.writes,
            "reads": self.reads,
            "messages": self.messages,
            "write_rps": round(self.writes / self.seconds, 2) if self.seconds else 0.0,
            "write_p50_ms": round(percentile(self.write_latency_ms, 50), 1),
            "write_p95_ms": round(percentile(self.write_latency_ms, 95), 1),
            "write_max_ms": round(max(self.write_latency_ms, default=0.0), 1),
            "read_p50_ms": round(percentile(self.read_latency_ms, 50), 1),
            "read_p95_ms": round(percentile(self.read_latency_ms, 95), 1),
            "read_max_ms": round(max(self.read_latency_ms, default=0.0), 1),
            "statuses": dict(sorted(self.statuses.items())),
            "error_rate": round(self.error_rate, 5),
            "events_created": self.events_after - self.events_before,
            "runs_created": self.runs_after - self.runs_before,
            "duplicate_occurrences": self.duplicate_occurrences,
            "duplicate_events": self.duplicate_events,
            "db_cpu_mean": round(self.db_cpu_mean, 1),
            "db_cpu_peak": round(self.db_cpu_peak, 1),
        }


# ---------------------------------------------------------------- processes


@contextlib.contextmanager
def running_server(database_url: str) -> Iterator[str]:
    """A real API process on a loopback port, with the gateway drain disabled."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    app = create_app(
        Settings(database_url=database_url, base_url=base, master_key_b64=new_master_key())
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.1)
    if not server.started:  # pragma: no cover - defensive
        raise RuntimeError("load server did not start")
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@contextlib.contextmanager
def running_workers(database_url: str, workspace: str, count: int) -> Iterator[list[Any]]:
    """Real scheduler worker processes, so Runs are claimed the way production claims them."""
    procs: list[subprocess.Popen[bytes]] = []
    env = {**os.environ, "AGENT_COLAB_DATABASE_URL": database_url}
    try:
        for i in range(count):
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "server.schedules.worker",
                        "--workspace",
                        workspace,
                        "--runner-id",
                        f"load-runner-{i}",
                        "--start-delay-s",
                        str(i * 2),
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        yield procs
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()


# ---------------------------------------------------------------- traffic


class _Driver(threading.Thread):
    """Issues one kind of request at a fixed rate until the deadline."""

    def __init__(
        self,
        base: str,
        token: str,
        rate_per_s: float,
        deadline: float,
        report: Report,
        lock: threading.Lock,
        kind: str,
        population: Population,
    ) -> None:
        super().__init__(daemon=True)
        self.base, self.token, self.rate = base, token, rate_per_s
        self.deadline, self.report, self.lock, self.kind = deadline, report, lock, kind
        self.population = population
        self.counter = itertools.count()

    def _request(self, client: httpx.Client, n: int) -> tuple[float, int]:
        # the Task API takes the channel row id, not the public channel_id text
        channel = str(self.population.channel_uuids[n % len(self.population.channel_uuids)])
        started = time.perf_counter()
        if self.kind == "write":
            response = client.post(
                "/api/v1/tasks",
                json={
                    "title": f"load {self.kind} {n}",
                    "channel_id": channel,
                    "domain": "research",
                    "risk": "LOW",
                    "criteria": [{"statement": "load", "check_type": "evidence", "required": True}],
                },
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Idempotency-Key": f"load-{self.kind}-{uuid.uuid4().hex}",
                },
            )
        else:
            response = client.get(
                "/api/v1/tasks?limit=20", headers={"Authorization": f"Bearer {self.token}"}
            )
        return (time.perf_counter() - started) * 1000.0, response.status_code

    def run(self) -> None:
        interval = 1.0 / self.rate if self.rate > 0 else 0.0
        if interval == 0.0:
            return
        with httpx.Client(base_url=self.base, timeout=30.0) as client:
            next_at = time.monotonic()
            while time.monotonic() < self.deadline:
                n = next(self.counter)
                try:
                    latency, status = self._request(client, n)
                except httpx.HTTPError:
                    latency, status = 0.0, 599  # transport failure counts as a server error
                with self.lock:
                    self.report.statuses[status] = self.report.statuses.get(status, 0) + 1
                    if self.kind == "write":
                        self.report.writes += 1
                        self.report.write_latency_ms.append(latency)
                    else:
                        self.report.reads += 1
                        self.report.read_latency_ms.append(latency)
                next_at += interval
                sleep = next_at - time.monotonic()
                if sleep > 0:
                    time.sleep(sleep)
                else:  # fell behind: resynchronise rather than burst
                    next_at = time.monotonic()


def _counts(engine: Engine, ws: uuid.UUID) -> tuple[int, int]:
    with Session(engine) as s:
        events = int(
            s.execute(
                text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": ws}
            ).scalar_one()
        )
        runs = int(
            s.execute(
                text(
                    "SELECT count(*) FROM schedule_runs r JOIN schedules c "
                    "ON c.schedule_id = r.schedule_id WHERE c.workspace_id = :w"
                ),
                {"w": ws},
            ).scalar_one()
        )
    return events, runs


def _duplicates(engine: Engine, ws: uuid.UUID) -> tuple[int, int]:
    """Occurrences with more than one Run, and Events sharing an idempotency identity."""
    with Session(engine) as s:
        occurrences = int(
            s.execute(
                text(
                    "SELECT count(*) FROM (SELECT r.schedule_id, r.occurrence_key "
                    "FROM schedule_runs r JOIN schedules c ON c.schedule_id = r.schedule_id "
                    "WHERE c.workspace_id = :w AND r.occurrence_key IS NOT NULL "
                    "GROUP BY 1, 2 HAVING count(*) > 1) d"
                ),
                {"w": ws},
            ).scalar_one()
        )
        events = int(
            s.execute(
                text(
                    "SELECT count(*) FROM (SELECT aggregate_type, aggregate_id, aggregate_seq "
                    "FROM events WHERE workspace_id = :w GROUP BY 1, 2, 3 HAVING count(*) > 1) d"
                ),
                {"w": ws},
            ).scalar_one()
        )
    return occurrences, events


def run_load(
    engine: Engine,
    database_url: str,
    population: Population,
    profile: Profile,
    *,
    seconds: float,
    workers: int = 2,
    sample_interval_s: float = 5.0,
    worker_watch: Any = None,
) -> Report:
    """Drive the profile for ``seconds`` of wall clock and return the measured report."""
    report = Report(profile=profile.name, seconds=seconds)
    report.events_before, report.runs_before = _counts(engine, population.ws)
    lock = threading.Lock()
    cpu = DbCpu()
    with (
        running_server(database_url) as base,
        running_workers(database_url, str(population.ws), workers) as procs,
    ):
        deadline = time.monotonic() + seconds
        drivers = [
            _Driver(
                base,
                population.owner_token,
                profile.api_writes_per_s,
                deadline,
                report,
                lock,
                "write",
                population,
            ),
            _Driver(
                base,
                population.ops_token,
                profile.messages_per_s,
                deadline,
                report,
                lock,
                "read",
                population,
            ),
        ]
        started = time.monotonic()
        for driver in drivers:
            driver.start()
        while time.monotonic() < deadline:
            time.sleep(min(sample_interval_s, max(0.1, deadline - time.monotonic())))
            cpu.sample()
            if worker_watch is not None:
                worker_watch([p.pid for p in procs if p.poll() is None])
        for driver in drivers:
            driver.join(timeout=60)
        report.seconds = time.monotonic() - started
    report.events_after, report.runs_after = _counts(engine, population.ws)
    report.duplicate_occurrences, report.duplicate_events = _duplicates(engine, population.ws)
    report.db_cpu_mean, report.db_cpu_peak = cpu.mean, cpu.peak
    return report


@dataclass
class SoakReport:
    """What a soak watches: growth is the failure mode, not a single bad sample."""

    minutes: float
    load: Report
    rss_first_kb: int = 0
    rss_last_kb: int = 0
    rss_peak_kb: int = 0
    connections_first: int = 0
    connections_last: int = 0
    work_items_open_first: int = 0
    work_items_open_last: int = 0
    stuck_runs: int = 0
    dead_letters: int = 0
    duplicate_deliveries: int = 0

    @property
    def rss_growth_ratio(self) -> float:
        return (self.rss_last_kb / self.rss_first_kb) if self.rss_first_kb else 1.0

    def summary(self) -> dict[str, Any]:
        return {
            "minutes": round(self.minutes, 2),
            **self.load.summary(),
            "worker_rss_first_kb": self.rss_first_kb,
            "worker_rss_last_kb": self.rss_last_kb,
            "worker_rss_peak_kb": self.rss_peak_kb,
            "worker_rss_growth": round(self.rss_growth_ratio, 3),
            "db_connections_first": self.connections_first,
            "db_connections_last": self.connections_last,
            "work_items_open_first": self.work_items_open_first,
            "work_items_open_last": self.work_items_open_last,
            "stuck_runs": self.stuck_runs,
            "dead_letters": self.dead_letters,
            "duplicate_deliveries": self.duplicate_deliveries,
        }


def _rss_kb(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        try:
            for line in (
                (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8").split("\n")
            ):
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
                    break
        except OSError:
            continue
    return total


def _connection_count(engine: Engine, database: str) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = :d"), {"d": database}
            ).scalar_one()
        )


def _open_work_items(engine: Engine, ws: uuid.UUID) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text(
                    "SELECT count(*) FROM work_items WHERE workspace_id = :w "
                    "AND status NOT IN ('RESULT_RECEIVED','REJECTED','EXPIRED','CANCELLED')"
                ),
                {"w": ws},
            ).scalar_one()
        )


def _soak_endstate(engine: Engine, ws: uuid.UUID) -> tuple[int, int, int]:
    """Stuck claimed Runs, bridge dead letters, and deliveries sent more than once."""
    with Session(engine) as s:
        stuck = int(
            s.execute(
                text(
                    "SELECT count(*) FROM schedule_runs r JOIN schedules c "
                    "ON c.schedule_id = r.schedule_id WHERE c.workspace_id = :w "
                    "AND r.status = 'CLAIMED' AND r.lease_expires_at < now()"
                ),
                {"w": ws},
            ).scalar_one()
        )
        dead = int(
            s.execute(
                text(
                    "SELECT count(*) FROM delivery_outbox WHERE workspace_id = :w "
                    "AND status = 'dead'"
                ),
                {"w": ws},
            ).scalar_one()
        )
        duplicates = int(
            s.execute(
                text(
                    "SELECT count(*) FROM (SELECT dedupe_key FROM delivery_outbox "
                    "WHERE workspace_id = :w GROUP BY dedupe_key HAVING count(*) > 1) d"
                ),
                {"w": ws},
            ).scalar_one()
        )
    return stuck, dead, duplicates


def run_soak(
    engine: Engine,
    database_url: str,
    population: Population,
    profile: Profile,
    *,
    minutes: float,
    workers: int = 2,
) -> SoakReport:
    """A bounded soak: the same traffic, plus growth checks on memory, connections and queues."""
    database = database_url.rsplit("/", 1)[-1]
    report = SoakReport(minutes=minutes, load=Report(profile=profile.name, seconds=0.0))
    report.connections_first = _connection_count(engine, database)
    report.work_items_open_first = _open_work_items(engine, population.ws)
    samples: list[int] = []

    def watch(pids: list[int]) -> None:
        rss = _rss_kb(pids)
        if rss:
            samples.append(rss)

    report.load = run_load(
        engine,
        database_url,
        population,
        profile,
        seconds=minutes * 60.0,
        workers=workers,
        worker_watch=watch,
    )
    report.connections_last = _connection_count(engine, database)
    report.work_items_open_last = _open_work_items(engine, population.ws)
    report.rss_first_kb = samples[0] if samples else 0
    report.rss_last_kb = samples[-1] if samples else 0
    report.rss_peak_kb = max(samples, default=0)
    report.stuck_runs, report.dead_letters, report.duplicate_deliveries = _soak_endstate(
        engine, population.ws
    )
    return report


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
