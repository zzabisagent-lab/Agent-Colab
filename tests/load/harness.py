"""Wall-clock traffic driver and measurement for V-P7-03 and V-P7-04 (P7-04).

A real uvicorn server on a loopback port, real scheduler worker processes, and threads issuing
real HTTP writes and reads at the profile's rates. Nothing is simulated: latency is measured
around ``httpx`` calls, Event and Run counts come from the database, and PostgreSQL CPU is read
from ``/proc`` so the §21.1 "DB CPU < 70 %" context is measured rather than assumed.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.secrets.envelope import new_master_key
from tests.load.population import Population
from tests.load.profile import Profile
from tests.load.samples import SampleContext

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
            "read_rps": round(self.reads / self.seconds, 2) if self.seconds else 0.0,
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


#: Child processes write to files, never to a pipe. An undrained ``subprocess.PIPE`` fills at the
#: 64 KiB kernel buffer and then blocks the child mid-request: the server froze with open
#: transactions and every request timed out, which looked exactly like a database bottleneck.
#: API server processes. One interpreter is GIL-bound at ~25 requests/s on a 24-core host, which is
#: below the §21.1 peak profile; the peak run therefore drives a multi-process server, as a
#: deployment does. Peak offers 90 requests/s, and a four-process server measured ~72: a 30-minute
#: run settled at 47 writes/s against the 60 it was offered, so the default carries real headroom.
API_WORKERS = int(os.environ.get("AGENT_COLAB_API_WORKERS", "8"))
_LOG_ROOT = os.environ.get("AGENT_COLAB_LOAD_LOG_DIR", tempfile.gettempdir())
LOG_DIR = Path(_LOG_ROOT) / "agent-colab-load"


def _child_log(name: str) -> Any:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return open(LOG_DIR / f"{name}.log", "wb")  # closed by the caller's finally


@contextlib.contextmanager
def running_server(
    database_url: str, *, workers: int = 1, pids: list[int] | None = None
) -> Iterator[str]:
    """The real server process (the ``agent-colab`` entry point), not an in-process thread.

    Measuring through a server that shares an interpreter with the measurer would blend the two;
    this is the same process a deployment runs.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "AGENT_COLAB_DATABASE_URL": database_url,
        "AGENT_COLAB_BASE_URL": base,
        "AGENT_COLAB_BIND_HOST": "127.0.0.1",
        "AGENT_COLAB_BIND_PORT": str(port),
        "AGENT_COLAB_MASTER_KEY_B64": new_master_key(),
        "AGENT_COLAB_GATEWAY_DRAIN": "0",
    }
    log = _child_log(f"server-{port}")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "server.main",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            str(workers),
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=log,
    )
    if pids is not None:
        pids.append(proc.pid)
    try:
        deadline = time.monotonic() + 60
        with httpx.Client(base_url=base, timeout=5.0) as client:
            while time.monotonic() < deadline:
                if proc.poll() is not None:  # pragma: no cover - startup failure
                    raise RuntimeError(f"server exited with {proc.returncode}")
                try:
                    if client.get("/healthz").status_code < 500:
                        break
                except httpx.HTTPError:
                    time.sleep(0.2)
            else:  # pragma: no cover - defensive
                raise RuntimeError("server did not become healthy")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
        log.close()


@contextlib.contextmanager
def running_workers(database_url: str, workspace: str, count: int) -> Iterator[list[Any]]:
    """Real scheduler worker processes, so Runs are claimed the way production claims them."""
    procs: list[subprocess.Popen[bytes]] = []
    logs: list[Any] = []
    env = {**os.environ, "AGENT_COLAB_DATABASE_URL": database_url}
    try:
        for i in range(count):
            logs.append(_child_log(f"worker-{i}"))
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
                    stdout=logs[-1],
                    stderr=logs[-1],
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
        for handle in logs:
            handle.close()


# ---------------------------------------------------------------- traffic


def _spawn_generators(
    base: str,
    population: Population,
    profile: Profile,
    seconds: float,
    out_dir: Path,
) -> list[tuple[subprocess.Popen[bytes], Path]]:
    """One generator process per share of each rate, so the client never becomes the bottleneck."""
    channels = ",".join(str(c) for c in population.channel_uuids[:50])
    spawned: list[tuple[subprocess.Popen[bytes], Path]] = []
    for kind, token, rate in (
        ("write", population.owner_token, profile.api_writes_per_s),
        ("read", population.ops_token, profile.messages_per_s),
    ):
        procs = _fan_out(rate)
        for index in range(procs):
            out = out_dir / f"{kind}-{index}.json"
            progress = out_dir / f"{kind}-{index}.progress.json"
            spawned.append(
                (
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "tests.load.generator",
                            "--base",
                            base,
                            "--token",
                            token,
                            "--kind",
                            kind,
                            "--rate",
                            str(rate / procs),
                            "--seconds",
                            str(seconds),
                            "--channels",
                            channels,
                            "--out",
                            str(out),
                            "--progress",
                            str(progress),
                        ],
                        cwd=ROOT,
                        env=os.environ.copy(),
                        stdout=_child_log(f"gen-{kind}-{index}"),
                        stderr=subprocess.STDOUT,
                    ),
                    out,
                )
            )
    return spawned


def _spawn_heartbeats(
    base: str, population: Population, seconds: float, out_dir: Path
) -> subprocess.Popen[bytes] | None:
    """One process beating for every seeded Agent, so heartbeat behaviour is exercised for real."""
    if not population.agent_ids:
        return None
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.load.heartbeat",
            "--base",
            base,
            "--token",
            population.ops_token,
            "--agents",
            ",".join(population.agent_ids),
            "--seconds",
            str(seconds),
            "--progress",
            str(out_dir / "heartbeat.progress.json"),
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        stdout=_child_log("heartbeat"),
        stderr=subprocess.STDOUT,
    )


def _stop(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        proc.kill()


def _collect(spawned: list[tuple[subprocess.Popen[bytes], Path]], report: Report) -> None:
    for proc, out in spawned:
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
        if not out.exists():
            continue
        data = json.loads(out.read_text(encoding="utf-8"))
        latencies = [float(v) for v in data["latency_ms"]]
        if data["kind"] == "write":
            report.writes += len(latencies)
            report.write_latency_ms.extend(latencies)
        else:
            report.reads += len(latencies)
            report.read_latency_ms.extend(latencies)
        for status, count in data["statuses"].items():
            report.statuses[int(status)] = report.statuses.get(int(status), 0) + int(count)


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


#: A driver thread issues at most this many requests per second, so the per-thread interval stays
#: well above the service time we measure (~70 ms for a write). Rates above it are split.
PER_THREAD_RPS = 5.0


def _fan_out(rate_per_s: float) -> int:
    """How many threads a rate needs to be issued without falling behind."""
    return max(1, int(-(-rate_per_s // PER_THREAD_RPS)))


def run_load(
    engine: Engine,
    database_url: str,
    population: Population,
    profile: Profile,
    *,
    seconds: float,
    workers: int = 2,
    api_workers: int = API_WORKERS,
    sample_interval_s: float = 5.0,
    worker_watch: Any = None,
    sample_sink: Any = None,
    sample_period_s: float = 60.0,
    heartbeats: bool = False,
) -> Report:
    """Drive the profile for ``seconds`` of wall clock and return the measured report.

    ``sample_sink`` is called with a :class:`~tests.load.samples.SampleContext` every
    ``sample_period_s``, and once more when the window closes; a soak writes those to disk so the
    whole run can be asserted afterwards rather than only its endpoints.
    """
    report = Report(profile=profile.name, seconds=seconds)
    report.events_before, report.runs_before = _counts(engine, population.ws)
    cpu = DbCpu()
    with tempfile.TemporaryDirectory(prefix="agent-colab-load-") as tmp:
        out_dir = Path(tmp)
        server_pids: list[int] = []
        with (
            running_server(database_url, workers=api_workers, pids=server_pids) as base,
            running_workers(database_url, str(population.ws), workers) as procs,
        ):
            started = time.monotonic()
            spawned = _spawn_generators(base, population, profile, seconds, out_dir)
            beater = _spawn_heartbeats(base, population, seconds, out_dir) if heartbeats else None
            deadline = started + seconds
            next_sample = started + sample_period_s

            def emit(final: bool) -> None:
                if sample_sink is None:
                    return
                sample_sink(
                    SampleContext(
                        elapsed_s=time.monotonic() - started,
                        server_pids=list(server_pids),
                        worker_pids=[p.pid for p in procs if p.poll() is None],
                        out_dir=out_dir,
                        db_cpu_pct=cpu.samples[-1] if cpu.samples else 0.0,
                        final=final,
                    )
                )

            while time.monotonic() < deadline:
                time.sleep(min(sample_interval_s, max(0.1, deadline - time.monotonic())))
                cpu.sample()
                if worker_watch is not None:
                    worker_watch([p.pid for p in procs if p.poll() is None])
                if time.monotonic() >= next_sample:
                    emit(False)
                    next_sample += sample_period_s
            emit(True)
            if beater is not None:
                _stop(beater)
            _collect(spawned, report)
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


def process_tree(roots: list[int]) -> list[int]:
    """Every live descendant of ``roots``, plus the roots.

    A multi-process API server and the scheduler workers fork children; resident memory measured
    only at the parent would miss exactly the processes that do the work.
    """
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        _head, _, tail = stat.rpartition(") ")
        fields = tail.split()
        if len(fields) > 1:
            parents[int(entry.name)] = int(fields[1])
    tree = set(roots)
    for _ in range(8):  # process trees here are two deep; the bound just stops a cycle
        grew = False
        for pid, ppid in parents.items():
            if ppid in tree and pid not in tree:
                tree.add(pid)
                grew = True
        if not grew:
            break
    return sorted(tree)


def rss_kb(pids: list[int]) -> int:
    """Summed resident memory of the given processes, in kilobytes."""
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


_rss_kb = rss_kb


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
    api_workers: int = API_WORKERS,
    sample_sink: Any = None,
    sample_period_s: float = 60.0,
    heartbeats: bool = True,
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
        api_workers=api_workers,
        worker_watch=watch,
        sample_sink=sample_sink,
        sample_period_s=sample_period_s,
        heartbeats=heartbeats,
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
