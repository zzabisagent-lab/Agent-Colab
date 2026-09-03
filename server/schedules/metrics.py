"""Scheduler metrics and alerts (P5-09; development plan §10A.5, validation plan V-P5-25/27).

Everything here derives from Run history (``schedule_runs``, ``schedule_run_attempts``), never
from counters kept elsewhere (ADR-0012 decision 8), with two exceptions history cannot express:
planner conflicts that never became rows (``schedule_metrics_counters``) and alert emissions kept
for hourly deduplication (``schedule_alert_emissions``). The pure functions take plain rows so
that unit tests need no database; :func:`snapshot` loads the rows for one Workspace.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock

SCHEMA_ID = "colab.schedule-metrics.v1"
DEFAULT_WINDOW_S = 24 * 3600
DEFAULT_POLL_S = 15
PENDING_STATUSES = ("PENDING", "DUE")
ACTIVE_STATUSES = ("CLAIMED", "TASK_CREATED", "RUNNING", "VERIFYING", "CANCEL_REQUESTED")
TERMINAL_STATUSES = ("SUCCEEDED", "FAILED", "SKIPPED", "TIMED_OUT", "CANCELLED")
POLICY_SKIP_CODES = ("SKIPPED_POLICY", "SKIPPED_AGENT_UNAVAILABLE", "BUDGET_EXCEEDED")
BACKFILL_WARNING_CODE = "BACKFILL_LIMITED_WARNING"

ALERT_START_DELAY = "START_DELAY_P95_ABOVE_60S"
ALERT_STUCK_LEASES = "STUCK_LEASES"
ALERT_FAILURE_RATE = "FAILURE_RATE"
ALERT_BUDGET = "BUDGET_EXCEEDED"


@dataclass(frozen=True)
class RunRow:
    """The columns of ``schedule_runs`` the metrics need (the core package owns the table)."""

    run_id: str
    schedule_id: str
    status: str
    kind: str = "SCHEDULED"
    scheduled_for: dt.datetime | None = None
    claimed_at: dt.datetime | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: dt.datetime | None = None
    error_code: str | None = None
    retry_of_run_id: str | None = None
    planner_note: str | None = None  # e.g. BACKFILL_TRUNCATED / MISSED_RUN_ONCE


@dataclass(frozen=True)
class ScheduleRow:
    schedule_id: str
    name: str | None = None
    status: str | None = None
    next_run_at: dt.datetime | None = None


@dataclass(frozen=True)
class Thresholds:
    """Alert thresholds (development plan §21.1: Run start delay p95 ≤ 60 s under normal load)."""

    start_delay_p95_s: float = 60.0
    stuck_leases: int = 1
    failure_rate: float = 0.5
    failure_rate_min_runs: int = 5


DEFAULT_THRESHOLDS = Thresholds()


@dataclass
class Percentiles:
    count: int = 0
    p50: float = 0.0
    p95: float = 0.0
    max: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {"count": self.count, "p50": self.p50, "p95": self.p95, "max": self.max}


# ------------------------------------------------------------------ pure computations


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (``p`` in [0, 100]); 0.0 for no values."""
    if not values:
        return 0.0
    if not 0 <= p <= 100:
        raise ValueError("percentile must be within [0, 100]")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return float(ordered[rank - 1])


def percentiles(values: Sequence[float]) -> Percentiles:
    return Percentiles(
        count=len(values),
        p50=percentile(values, 50),
        p95=percentile(values, 95),
        max=float(max(values)) if values else 0.0,
    )


def _seconds(later: dt.datetime | None, earlier: dt.datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return max(0.0, (later - earlier).total_seconds())


def lag_values(rows: Iterable[RunRow], now: dt.datetime) -> list[float]:
    """Seconds a DUE/CLAIMED Run has waited past its ``scheduled_for``."""
    out: list[float] = []
    for r in rows:
        if r.status in ("DUE", "CLAIMED") and r.scheduled_for is not None:
            out.append(max(0.0, (now - r.scheduled_for).total_seconds()))
    return out


def start_delay_values(rows: Iterable[RunRow]) -> list[float]:
    """Seconds between ``scheduled_for`` and the actual start of every started Run."""
    out: list[float] = []
    for r in rows:
        delay = _seconds(r.started_at, r.scheduled_for)
        if delay is not None and r.kind == "SCHEDULED":
            out.append(delay)
    return out


def stuck_leases(rows: Iterable[RunRow], now: dt.datetime, poll_s: int) -> int:
    """CLAIMED Runs whose lease expired more than one poll interval ago (nobody recovered)."""
    limit = now - dt.timedelta(seconds=poll_s)
    return sum(
        1
        for r in rows
        if r.status == "CLAIMED" and r.lease_expires_at is not None and r.lease_expires_at < limit
    )


def compute(
    rows: Sequence[RunRow],
    now: dt.datetime,
    *,
    poll_s: int = DEFAULT_POLL_S,
    window_s: int = DEFAULT_WINDOW_S,
    schedules: Sequence[ScheduleRow] = (),
    duplicates_prevented: Mapping[str, int] | None = None,
    budget_alerts: int = 0,
) -> dict[str, Any]:
    """The metrics snapshot for the given Run rows (pure; no database)."""
    by_status = Counter(r.status for r in rows)
    failures = by_status.get("FAILED", 0)
    timed_out = by_status.get("TIMED_OUT", 0)
    succeeded = by_status.get("SUCCEEDED", 0)
    finished = failures + timed_out + succeeded
    skips = Counter(str(r.error_code or "SKIPPED") for r in rows if r.status == "SKIPPED")
    policy_denials = sum(v for code, v in skips.items() if code in POLICY_SKIP_CODES)
    backfill_warnings = sum(
        1
        for r in rows
        if r.error_code == BACKFILL_WARNING_CODE or str(r.planner_note or "").startswith("BACKFILL")
    )
    dupes = dict(duplicates_prevented or {})
    per_schedule: dict[str, dict[str, Any]] = {}
    for s in schedules:
        per_schedule[s.schedule_id] = {
            "schedule_id": s.schedule_id,
            "name": s.name,
            "status": s.status,
            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            "runs_in_window": 0,
            "failures": 0,
            "last_status": None,
            "last_error_code": None,
            "duplicates_prevented": int(dupes.get(s.schedule_id, 0)),
        }
    for r in sorted(rows, key=lambda x: (x.scheduled_for or now, x.run_id)):
        entry = per_schedule.setdefault(
            r.schedule_id,
            {
                "schedule_id": r.schedule_id,
                "name": None,
                "status": None,
                "next_run_at": None,
                "runs_in_window": 0,
                "failures": 0,
                "last_status": None,
                "last_error_code": None,
                "duplicates_prevented": int(dupes.get(r.schedule_id, 0)),
            },
        )
        entry["runs_in_window"] += 1
        entry["failures"] += int(r.status in ("FAILED", "TIMED_OUT"))
        if r.status in TERMINAL_STATUSES or r.status in ACTIVE_STATUSES:
            entry["last_status"] = r.status
            entry["last_error_code"] = r.error_code
    return {
        "schema_id": SCHEMA_ID,
        "generated_at": now.isoformat(),
        "window_s": window_s,
        "poll_s": poll_s,
        "due": by_status.get("DUE", 0),
        "running": sum(by_status.get(s, 0) for s in ACTIVE_STATUSES),
        "runs_in_window": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "lag_s": percentiles(lag_values(rows, now)).as_dict(),
        "start_delay_s": percentiles(start_delay_values(rows)).as_dict(),
        "failures": failures,
        "timed_out": timed_out,
        "succeeded": succeeded,
        "failure_rate": (failures + timed_out) / finished if finished else 0.0,
        "skips_by_code": dict(sorted(skips.items())),
        "policy_denials": policy_denials,
        "backfill_warnings": backfill_warnings,
        "duplicates_prevented": sum(dupes.values()),
        "stuck_leases": stuck_leases(rows, now, poll_s),
        "budget_alerts": budget_alerts,
        "schedules": sorted(per_schedule.values(), key=lambda e: str(e["schedule_id"])),
    }


def alerts(
    snapshot: Mapping[str, Any], thresholds: Thresholds = DEFAULT_THRESHOLDS
) -> list[dict[str, Any]]:
    """Alert dicts for a snapshot: stable keys, severity, the measured value and the threshold.

    The start-delay alert fires strictly above the threshold (a p95 of exactly 60 s meets the
    target and does not alert).
    """
    out: list[dict[str, Any]] = []
    p95 = float(snapshot["start_delay_s"]["p95"])
    if p95 > thresholds.start_delay_p95_s:
        out.append(
            {
                "key": ALERT_START_DELAY,
                "severity": "warning",
                "value": p95,
                "threshold": thresholds.start_delay_p95_s,
                "detail": (
                    f"Run start delay p95 {p95:.1f} s exceeds {thresholds.start_delay_p95_s:.0f} s"
                ),
            }
        )
    stuck = int(snapshot["stuck_leases"])
    if stuck >= thresholds.stuck_leases:
        out.append(
            {
                "key": ALERT_STUCK_LEASES,
                "severity": "critical",
                "value": stuck,
                "threshold": thresholds.stuck_leases,
                "detail": f"{stuck} claimed Run(s) with an expired lease and no recovery",
            }
        )
    finished = int(snapshot["failures"]) + int(snapshot["timed_out"]) + int(snapshot["succeeded"])
    rate = float(snapshot["failure_rate"])
    if finished >= thresholds.failure_rate_min_runs and rate >= thresholds.failure_rate:
        out.append(
            {
                "key": ALERT_FAILURE_RATE,
                "severity": "warning",
                "value": rate,
                "threshold": thresholds.failure_rate,
                "detail": f"{rate:.0%} of {finished} finished Runs failed or timed out",
            }
        )
    budget = int(snapshot["budget_alerts"])
    if budget > 0:
        out.append(
            {
                "key": ALERT_BUDGET,
                "severity": "warning",
                "value": budget,
                "threshold": 1,
                "detail": f"{budget} Run(s) skipped for budget overruns in the window",
            }
        )
    return out


# ------------------------------------------------------------------ database access


def _table_exists(session: Session, name: str) -> bool:
    return session.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar() is not None


def _dt(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return dt.datetime.fromisoformat(str(value))


def load_runs(
    session: Session, workspace_id: uuid.UUID, now: dt.datetime, window_s: int
) -> list[RunRow]:
    """Runs scheduled within the window plus every non-terminal Run (whatever its age)."""
    if not _table_exists(session, "schedule_runs"):
        return []
    rows = session.execute(
        text(
            "SELECT run_id, schedule_id, status, run_kind, scheduled_for, claimed_at, started_at, "
            "finished_at, claimed_by, lease_expires_at, error_code, retry_of_run_id, "
            "planner_note FROM schedule_runs WHERE workspace_id = :w AND "
            "(scheduled_for >= :since OR status NOT IN ('SUCCEEDED','FAILED','SKIPPED',"
            "'TIMED_OUT','CANCELLED')) ORDER BY scheduled_for, run_id"
        ),
        {"w": workspace_id, "since": now - dt.timedelta(seconds=window_s)},
    ).all()
    return [
        RunRow(
            run_id=str(r[0]),
            schedule_id=str(r[1]),
            status=str(r[2]),
            kind=str(r[3] or "SCHEDULED"),
            scheduled_for=_dt(r[4]),
            claimed_at=_dt(r[5]),
            started_at=_dt(r[6]),
            finished_at=_dt(r[7]),
            lease_owner=None if r[8] is None else str(r[8]),
            lease_expires_at=_dt(r[9]),
            error_code=None if r[10] is None else str(r[10]),
            retry_of_run_id=None if r[11] is None else str(r[11]),
            planner_note=None if r[12] is None else str(r[12]),
        )
        for r in rows
    ]


def load_schedules(session: Session, workspace_id: uuid.UUID) -> list[ScheduleRow]:
    if not _table_exists(session, "schedules"):
        return []
    rows = session.execute(
        text(
            "SELECT schedule_id, name, status, next_run_at FROM schedules "
            "WHERE workspace_id = :w ORDER BY schedule_id"
        ),
        {"w": workspace_id},
    ).all()
    return [ScheduleRow(str(r[0]), r[1], r[2], _dt(r[3])) for r in rows]


def duplicates_prevented(session: Session, workspace_id: uuid.UUID) -> dict[str, int]:
    rows = session.execute(
        text(
            "SELECT schedule_id, value FROM schedule_metrics_counters "
            "WHERE workspace_id = :w AND counter = 'duplicates_prevented'"
        ),
        {"w": workspace_id},
    ).all()
    return {str(r[0]): int(r[1]) for r in rows}


def record_duplicate_prevented(
    session: Session, workspace_id: uuid.UUID | str, schedule_id: str, now: dt.datetime
) -> int:
    """Hook for the planner: an occurrence-key conflict prevented a duplicate Run (V-P5-06)."""
    row = session.execute(
        text(
            "INSERT INTO schedule_metrics_counters (workspace_id, schedule_id, counter, value, "
            "updated_at) VALUES (:w, :s, 'duplicates_prevented', 1, :now) "
            "ON CONFLICT (workspace_id, schedule_id, counter) DO UPDATE SET "
            "value = schedule_metrics_counters.value + 1, updated_at = EXCLUDED.updated_at "
            "RETURNING value"
        ),
        {"w": uuid.UUID(str(workspace_id)), "s": schedule_id, "now": now},
    ).scalar_one()
    return int(row)


def budget_alert_count(
    session: Session, workspace_id: uuid.UUID, now: dt.datetime, window_s: int
) -> int:
    """Budget overruns raised by the execution package (``budget_alerts``, migration 0017)."""
    if not _table_exists(session, "budget_alerts"):
        return 0
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM budget_alerts WHERE workspace_id = :w "
                "AND kind = 'budget_exceeded' AND raised_at >= :since"
            ),
            {"w": workspace_id, "since": now - dt.timedelta(seconds=window_s)},
        ).scalar_one()
    )


def snapshot(
    session: Session,
    workspace_id: uuid.UUID | str,
    now: dt.datetime,
    *,
    window_s: int = DEFAULT_WINDOW_S,
    poll_s: int = DEFAULT_POLL_S,
) -> dict[str, Any]:
    ws = uuid.UUID(str(workspace_id))
    rows = load_runs(session, ws, now, window_s)
    snap = compute(
        rows,
        now,
        poll_s=poll_s,
        window_s=window_s,
        schedules=load_schedules(session, ws),
        duplicates_prevented=duplicates_prevented(session, ws),
        budget_alerts=budget_alert_count(session, ws, now, window_s),
    )
    snap["backfill_warnings"] += planner_backfill_warnings(session, ws, now, window_s)
    return snap


def planner_backfill_warnings(
    session: Session, workspace_id: uuid.UUID, now: dt.datetime, window_s: int
) -> int:
    """Planner findings that never became Runs (missed occurrences truncated by the backfill
    window/limit) recorded by the core package in ``schedule_planner_notes``."""
    if not _table_exists(session, "schedule_planner_notes"):
        return 0
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM schedule_planner_notes n JOIN schedules s "
                "ON s.schedule_id = n.schedule_id WHERE s.workspace_id = :w "
                "AND n.reason LIKE 'BACKFILL%' AND n.noted_at >= :since"
            ),
            {"w": workspace_id, "since": now - dt.timedelta(seconds=window_s)},
        ).scalar_one()
    )


def schedule_snapshot(
    session: Session,
    workspace_id: uuid.UUID | str,
    schedule_id: str,
    now: dt.datetime,
    *,
    window_s: int = DEFAULT_WINDOW_S,
    poll_s: int = DEFAULT_POLL_S,
) -> dict[str, Any]:
    ws = uuid.UUID(str(workspace_id))
    rows = [r for r in load_runs(session, ws, now, window_s) if r.schedule_id == schedule_id]
    schedules = [s for s in load_schedules(session, ws) if s.schedule_id == schedule_id]
    dupes = duplicates_prevented(session, ws)
    return compute(
        rows,
        now,
        poll_s=poll_s,
        window_s=window_s,
        schedules=schedules,
        duplicates_prevented={schedule_id: dupes.get(schedule_id, 0)},
        budget_alerts=0,
    )


# ------------------------------------------------------------------ alert emission


@dataclass
class EmissionResult:
    emitted: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)


def hour_bucket(now: dt.datetime) -> dt.datetime:
    return now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)


def emit_alerts(
    session: Session,
    workspace_id: uuid.UUID | str,
    alert_items: Sequence[Mapping[str, Any]],
    clock: Clock,
    *,
    ops_channel: str | None,
) -> EmissionResult:
    """Send each alert once per hour to the ops channel through the notification outbox.

    ``schedule_alert_emissions`` is the deduplication ledger (unique per Workspace, key and hour);
    the outbox row uses the same key so a retry never produces a second message.
    """
    from server.notifications.outbox import enqueue

    ws = uuid.UUID(str(workspace_id))
    now = clock.now()
    bucket = hour_bucket(now)
    result = EmissionResult()
    for alert in alert_items:
        key = str(alert["key"])
        inserted = session.execute(
            text(
                "INSERT INTO schedule_alert_emissions (workspace_id, alert_key, hour_bucket, "
                "severity, payload, emitted_at) VALUES (:w, :k, :b, :sev, CAST(:p AS jsonb), :now) "
                "ON CONFLICT (workspace_id, alert_key, hour_bucket) DO NOTHING RETURNING id"
            ),
            {
                "w": ws,
                "k": key,
                "b": bucket,
                "sev": str(alert.get("severity", "warning")),
                "p": json.dumps(dict(alert), default=str),
                "now": now,
            },
        ).first()
        if inserted is None:
            result.suppressed.append(key)
            continue
        result.emitted.append(key)
        if ops_channel:
            enqueue(
                session,
                str(ws),
                "notification",
                f"mattermost:{ops_channel}",
                f"schedule-alert|{key}|{bucket.strftime('%Y%m%dT%H')}",
                {
                    "event_type": "SCHEDULE_ALERT",
                    "alert_key": key,
                    "severity": str(alert.get("severity", "warning")),
                    "message": f":rotating_light: Scheduler alert {key}: {alert.get('detail', '')}",
                    "value": alert.get("value"),
                    "threshold": alert.get("threshold"),
                },
                None,
                now,
            )
    return result


def evaluate_and_emit(
    session: Session,
    workspace_id: uuid.UUID | str,
    clock: Clock,
    *,
    ops_channel: str | None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    window_s: int = DEFAULT_WINDOW_S,
    poll_s: int = DEFAULT_POLL_S,
) -> tuple[dict[str, Any], list[dict[str, Any]], EmissionResult]:
    """One maintenance-tick step: snapshot → alerts → hourly-deduplicated emission."""
    snap = snapshot(session, workspace_id, clock.now(), window_s=window_s, poll_s=poll_s)
    found = alerts(snap, thresholds)
    emitted = emit_alerts(session, workspace_id, found, clock, ops_channel=ops_channel)
    return snap, found, emitted


def overview_block(session: Session, workspace_id: uuid.UUID, now: dt.datetime) -> dict[str, Any]:
    """The ``schedules`` block of the ops overview (development plan §11.1)."""
    snap = snapshot(session, workspace_id, now)
    return {
        "due": snap["due"],
        "running": snap["running"],
        "runs_in_window": snap["runs_in_window"],
        "lag_p95_s": snap["lag_s"]["p95"],
        "start_delay_p95_s": snap["start_delay_s"]["p95"],
        "failures": snap["failures"],
        "policy_denials": snap["policy_denials"],
        "stuck_leases": snap["stuck_leases"],
        "duplicates_prevented": snap["duplicates_prevented"],
        "alerts": [a["key"] for a in alerts(snap)],
    }
