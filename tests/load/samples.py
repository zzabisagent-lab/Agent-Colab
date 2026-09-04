"""Per-minute soak samples: what is written during a run and what a completed run must show.

A soak fails through *growth* and through *stuck state*, and neither is visible in a start/end
pair: a leak that only appears after eight hours, or a lease that stops being reclaimed at hour
twenty, looks identical to a healthy run when only the first and last rows are compared. So the
runner appends one JSON object per minute and the assertions read the whole series. The same
module owns both ends, so a field can never be sampled under one name and asserted under another.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

#: A soak run is 24 hours (development plan §21.1, criterion V-P7-04). A file is only allowed to
#: satisfy the criterion when its final sample says the run actually reached that duration; the
#: minute of slack covers the gap between the last periodic sample and the closing one.
REQUIRED_SECONDS = 24 * 60 * 60
COVERAGE_SLACK_S = 60.0

#: An Agent is swept offline after three missed heartbeats or 90 seconds (development plan §7C).
#: The driver beats every 20 seconds, so 90 seconds is exactly "three in a row were missed".
HEARTBEAT_INTERVAL_S = 20.0
HEARTBEAT_STALE_S = 90.0

#: Private (unshared) memory over a day, which is what a leak actually grows. Ten per cent across
#: 24 hours is far above allocator noise — arena fragmentation and per-tick buffers — and far below
#: any leak that matters: something losing even a kilobyte per request would pass a tenth of a
#: percent per hour and blow through this by mid-morning.
PRIVATE_GROWTH_LIMIT = 1.10
#: Summed VmRSS counts a shared page once per process that maps it, so across a pre-forked pool its
#: *absolute* value overstates memory by the shared set — here about 270 MB of a 1,260 MB reading.
#: That inflated denominator makes the same growth look smaller as a ratio, which is why the leak
#: bound reads private memory instead. Measured growth is identical in both (the two series move by
#: the same bytes), so RSS carries no separate signal; it is kept as a ceiling and a shape check.
#: Growth that decelerates is warm-up — caches filling, allocator arenas reaching steady state.
#: A leak under steady load holds its slope, so the second half of the run is compared with the
#: first rather than a rate being assumed in advance.
RSS_CEILING = 1.35
#: Peak against the opening level: a transient spike is normal, a doubling is not.
RSS_PEAK_LIMIT = 1.5

#: Connections are pooled across eight API worker processes and two scheduler workers, each with
#: its own pool, so the count moves as pools fill and idle connections are recycled. What a soak
#: watches for is a *climb*: connections that are never returned. So the series is compared with a
#: warmed baseline (the first hour after startup) rather than with its own first sample.
CONNECTION_DRIFT_LIMIT = 5
CONNECTION_SPREAD_LIMIT = 20
WARM_FROM_S = 600.0
WARM_TO_S = 3600.0

#: A claimed Run whose lease has expired is only *stuck* if nobody reclaims it. Two consecutive
#: samples is two minutes, far longer than a reclaim tick.
STUCK_TOLERANCE_SAMPLES = 2


@dataclass
class SampleContext:
    """What the harness knows at sample time and the database cannot tell us."""

    elapsed_s: float
    server_pids: list[int]
    worker_pids: list[int]
    out_dir: Path
    db_cpu_pct: float = 0.0
    final: bool = False


def generator_totals(out_dir: Path) -> tuple[int, int, int]:
    """Cumulative writes, reads and errors from the generator progress files."""
    writes = reads = errors = 0
    for path in sorted(out_dir.glob("*.progress.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # a half-written file is skipped, not fatal
            continue
        count = int(data.get("requests", 0))
        if data.get("kind") == "write":
            writes += count
        else:
            reads += count
        errors += int(data.get("errors", 0))
    return writes, reads, errors


def _scalar(session: Session, sql: str, params: dict[str, Any]) -> int:
    return int(session.execute(text(sql), params).scalar_one())


def database_sample(engine: Engine, ws: uuid.UUID, database: str) -> dict[str, Any]:
    """Everything the soak watches that lives in PostgreSQL.

    JIT is disabled for the sampler's own session. These are unindexed aggregates over tables that
    reach millions of rows during a soak, so their planned cost crosses ``jit_above_cost`` partway
    through the run and PostgreSQL starts trying to compile them. Compilation buys nothing here —
    the queries run once a minute and are I/O bound — and on an installation whose ``llvmjit.so``
    cannot resolve its LLVM runtime it fails the query outright, which silently blanks every
    database field from the moment the tables grow past the threshold. A soak must not depend on
    the host's JIT configuration to record its own evidence.
    """
    with Session(engine) as s:
        s.execute(text("SET jit = off"))
        events = _scalar(s, "SELECT count(*) FROM events WHERE workspace_id = :w", {"w": ws})
        runs = _scalar(
            s,
            "SELECT count(*) FROM schedule_runs r JOIN schedules c "
            "ON c.schedule_id = r.schedule_id WHERE c.workspace_id = :w",
            {"w": ws},
        )
        duplicate_occurrences = _scalar(
            s,
            "SELECT count(*) FROM (SELECT r.schedule_id, r.occurrence_key FROM schedule_runs r "
            "JOIN schedules c ON c.schedule_id = r.schedule_id WHERE c.workspace_id = :w "
            "AND r.occurrence_key IS NOT NULL GROUP BY 1, 2 HAVING count(*) > 1) d",
            {"w": ws},
        )
        duplicate_events = _scalar(
            s,
            "SELECT count(*) FROM (SELECT aggregate_type, aggregate_id, aggregate_seq FROM events "
            "WHERE workspace_id = :w GROUP BY 1, 2, 3 HAVING count(*) > 1) d",
            {"w": ws},
        )
        open_work_items = _scalar(
            s,
            "SELECT count(*) FROM work_items WHERE workspace_id = :w "
            "AND status NOT IN ('RESULT_RECEIVED','REJECTED','EXPIRED','CANCELLED')",
            {"w": ws},
        )
        stuck = _scalar(
            s,
            "SELECT count(*) FROM schedule_runs r JOIN schedules c "
            "ON c.schedule_id = r.schedule_id WHERE c.workspace_id = :w "
            "AND r.status = 'CLAIMED' AND r.lease_expires_at < now()",
            {"w": ws},
        )
        outbox_dead = _scalar(
            s,
            "SELECT count(*) FROM delivery_outbox WHERE workspace_id = :w AND status = 'dead'",
            {"w": ws},
        )
        bridge_dead = _scalar(
            s, "SELECT count(*) FROM bridge_dead_letters WHERE workspace_id = :w", {"w": ws}
        )
        duplicate_deliveries = _scalar(
            s,
            "SELECT count(*) FROM (SELECT dedupe_key FROM delivery_outbox WHERE workspace_id = :w "
            "GROUP BY dedupe_key HAVING count(*) > 1) d",
            {"w": ws},
        )
        relays = _scalar(
            s, "SELECT count(*) FROM message_mappings WHERE workspace_id = :w", {"w": ws}
        )
        relays_failed = _scalar(
            s,
            "SELECT count(*) FROM message_mappings WHERE workspace_id = :w "
            "AND delivery_status IN ('failed','dead')",
            {"w": ws},
        )
        # the same origin message relayed twice into the same platform, whatever its dedupe key
        relay_duplicates = _scalar(
            s,
            "SELECT count(*) FROM (SELECT bridge_id, origin_platform, origin_message_id, "
            "destination_platform FROM message_mappings WHERE workspace_id = :w "
            "GROUP BY 1, 2, 3, 4 HAVING count(*) > 1) d",
            {"w": ws},
        )
        heartbeats = _scalar(
            s,
            "SELECT count(*) FROM agent_heartbeats h JOIN agents a ON a.agent_id = h.agent_id "
            "WHERE a.workspace_id = :w",
            {"w": ws},
        )
        age = s.execute(
            text(
                "SELECT coalesce(max(extract(epoch FROM now() - last_heartbeat_at)), -1) "
                "FROM agents WHERE workspace_id = :w"
            ),
            {"w": ws},
        ).scalar_one()
        stale_agents = _scalar(
            s,
            "SELECT count(*) FROM agents WHERE workspace_id = :w AND (last_heartbeat_at IS NULL "
            "OR last_heartbeat_at < now() - make_interval(secs => :s))",
            {"w": ws, "s": HEARTBEAT_STALE_S},
        )
        connections = _scalar(
            s, "SELECT count(*) FROM pg_stat_activity WHERE datname = :d", {"d": database}
        )
    return {
        "events": events,
        "runs": runs,
        "duplicate_occurrence_keys": duplicate_occurrences,
        "duplicate_events": duplicate_events,
        "open_work_items": open_work_items,
        "stuck_claimed_runs": stuck,
        "dead_letters": outbox_dead + bridge_dead,
        "duplicate_deliveries": duplicate_deliveries,
        "bridge_relays": relays,
        "bridge_relays_failed": relays_failed,
        "bridge_relay_duplicates": relay_duplicates,
        "heartbeats": heartbeats,
        "heartbeat_age_s": round(float(age), 1) if float(age) >= 0 else None,
        "stale_agents": stale_agents,
        "db_connections": connections,
    }


def read_samples(path: Path) -> list[dict[str, Any]]:
    """Every complete JSON object in the file; a truncated final line is ignored, not fatal."""
    samples: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except ValueError:
                continue
    return samples


def coverage_seconds(samples: list[dict[str, Any]]) -> float:
    return float(samples[-1]["elapsed_s"]) if samples else 0.0


def utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
