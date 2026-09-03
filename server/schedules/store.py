"""Row access for Schedules, ScheduleVersions and ScheduleRuns (development plan §6.6).

Pure persistence: no policy, no transitions (those live in ``server.schedules.contract`` and the
command handlers). Versions are immutable snapshots; Runs pin ``schedule_version_id`` and the
version's ``snapshot_hash`` at creation and the DB trigger keeps that pin.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.events.canonical import canonical_json

VERSION_CONTENT_FIELDS: tuple[str, ...] = (
    "name",
    "cron_expression",
    "timezone",
    "channel_id",
    "execution_principal_id",
    "agent_selection",
    "action_template",
    "concurrency_policy",
    "missed_run_policy",
    "backfill_limit",
    "backfill_window_seconds",
    "max_duration_seconds",
    "min_interval_minutes",
    "retry_policy",
    "budget_policy",
    "documentation_policy",
    "starts_at",
    "ends_at",
)


def iso_ms(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    value = value.astimezone(dt.UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def parse_ts(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)


def snapshot_hash(content: dict[str, Any]) -> str:
    """SHA-256 over the RFC 8785 canonical form of the version content (redacted: refs only)."""
    body = {k: content.get(k) for k in VERSION_CONTENT_FIELDS}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def new_schedule_id() -> str:
    return f"sch-{uuid.uuid4().hex[:12]}"


def new_version_id() -> str:
    return f"schv-{uuid.uuid4().hex[:12]}"


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class ScheduleRow:
    id: uuid.UUID
    schedule_id: str
    workspace_id: uuid.UUID
    name: str
    status: str
    current_version_id: uuid.UUID | None
    next_run_at: dt.datetime | None
    last_planned_until: dt.datetime | None
    last_event_id: str | None
    created_by: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclass(frozen=True)
class VersionRow:
    id: uuid.UUID
    schedule_version_id: str
    schedule_id: str
    version: int
    name: str
    channel_id: uuid.UUID
    cron_expression: str
    timezone: str
    execution_principal_id: uuid.UUID
    agent_selection: dict[str, Any]
    action_template: dict[str, Any]
    concurrency_policy: str
    missed_run_policy: str
    backfill_limit: int
    backfill_window_seconds: int
    max_duration_seconds: int
    min_interval_minutes: int
    retry_policy: dict[str, Any]
    budget_policy: dict[str, Any]
    documentation_policy: dict[str, Any]
    starts_at: dt.datetime | None
    ends_at: dt.datetime | None
    snapshot_hash: str
    created_by: uuid.UUID
    event_id: str | None
    created_at: dt.datetime

    def content(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cron_expression": self.cron_expression,
            "timezone": self.timezone,
            "channel_id": str(self.channel_id),
            "execution_principal_id": str(self.execution_principal_id),
            "agent_selection": dict(self.agent_selection),
            "action_template": dict(self.action_template),
            "concurrency_policy": self.concurrency_policy,
            "missed_run_policy": self.missed_run_policy,
            "backfill_limit": self.backfill_limit,
            "backfill_window_seconds": self.backfill_window_seconds,
            "max_duration_seconds": self.max_duration_seconds,
            "min_interval_minutes": self.min_interval_minutes,
            "retry_policy": dict(self.retry_policy),
            "budget_policy": dict(self.budget_policy),
            "documentation_policy": dict(self.documentation_policy),
            "starts_at": iso_ms(self.starts_at),
            "ends_at": iso_ms(self.ends_at),
        }

    def view(self) -> dict[str, Any]:
        return {
            "schedule_version_id": self.schedule_version_id,
            "schedule_id": self.schedule_id,
            "version": self.version,
            **self.content(),
            "snapshot_hash": self.snapshot_hash,
            "created_by": str(self.created_by),
            "event_id": self.event_id,
            "created_at": iso_ms(self.created_at),
        }


@dataclass(frozen=True)
class RunRow:
    id: uuid.UUID
    run_id: str
    workspace_id: uuid.UUID
    schedule_id: str
    schedule_version_id: uuid.UUID
    run_kind: str
    occurrence_key: str | None
    scheduled_for: dt.datetime
    local_scheduled_for: dt.datetime | None
    retry_of_run_id: str | None
    request_key: str | None
    status: str
    attempt_count: int
    task_id: str | None
    idempotency_key: str
    version_hash: str
    claimed_by: str | None
    claimed_at: dt.datetime | None
    lease_expires_at: dt.datetime | None
    heartbeat_at: dt.datetime | None
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    result_event_id: str | None
    error_code: str | None
    cancel_requested_at: dt.datetime | None
    cancelled_at: dt.datetime | None
    planner_note: str | None
    requested_by: uuid.UUID | None
    created_at: dt.datetime
    updated_at: dt.datetime
    version_public_id: str | None = None
    version_no: int | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "schedule_id": self.schedule_id,
            "schedule_version_id": self.version_public_id or str(self.schedule_version_id),
            "version": self.version_no,
            "run_kind": self.run_kind,
            "occurrence_key": self.occurrence_key,
            "scheduled_for": iso_ms(self.scheduled_for),
            "local_scheduled_for": (
                None
                if self.local_scheduled_for is None
                else self.local_scheduled_for.strftime("%Y-%m-%dT%H:%M")
            ),
            "retry_of_run_id": self.retry_of_run_id,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "task_id": self.task_id,
            "idempotency_key": self.idempotency_key,
            "version_hash": self.version_hash,
            "claimed_by": self.claimed_by,
            "lease_expires_at": iso_ms(self.lease_expires_at),
            "started_at": iso_ms(self.started_at),
            "finished_at": iso_ms(self.finished_at),
            "result_event_id": self.result_event_id,
            "error_code": self.error_code,
            "cancel_requested_at": iso_ms(self.cancel_requested_at),
            "cancelled_at": iso_ms(self.cancelled_at),
            "planner_note": self.planner_note,
            "attempts": list(self.attempts),
        }


# ------------------------------------------------------------------------------- schedules
_SCHEDULE_COLS = (
    "id, schedule_id, workspace_id, name, status, current_version_id, next_run_at, "
    "last_planned_until, last_event_id, created_by, created_at, updated_at"
)


def _schedule(row: Any) -> ScheduleRow:
    m = row._mapping
    return ScheduleRow(
        id=m["id"],
        schedule_id=str(m["schedule_id"]),
        workspace_id=m["workspace_id"],
        name=str(m["name"]),
        status=str(m["status"]),
        current_version_id=m["current_version_id"],
        next_run_at=m["next_run_at"],
        last_planned_until=m["last_planned_until"],
        last_event_id=m["last_event_id"],
        created_by=m["created_by"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


def load_schedule(
    session: Session, workspace_id: uuid.UUID, schedule_id: str, *, for_update: bool = False
) -> ScheduleRow | None:
    lock = " FOR UPDATE" if for_update else ""
    row = session.execute(
        text(
            f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE schedule_id = :s AND workspace_id = :w"  # noqa: S608 - constant column list, bound parameters
            f"{lock}"
        ),
        {"s": schedule_id, "w": workspace_id},
    ).first()
    return None if row is None else _schedule(row)


def list_schedules(session: Session, workspace_id: uuid.UUID) -> list[ScheduleRow]:
    rows = session.execute(
        text(
            f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE workspace_id = :w ORDER BY schedule_id"  # noqa: S608 - constant column list, bound parameters
        ),
        {"w": workspace_id},
    ).all()
    return [_schedule(r) for r in rows]


def enabled_schedules(session: Session, workspace_id: uuid.UUID) -> list[ScheduleRow]:
    rows = session.execute(
        text(
            f"SELECT {_SCHEDULE_COLS} FROM schedules "  # noqa: S608 - constant column list, bound parameters
            "WHERE workspace_id = :w AND status = 'ENABLED' "
            "ORDER BY schedule_id FOR UPDATE SKIP LOCKED"
        ),
        {"w": workspace_id},
    ).all()
    return [_schedule(r) for r in rows]


def insert_schedule(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    schedule_id: str,
    name: str,
    created_by: uuid.UUID,
    now: dt.datetime,
) -> uuid.UUID:
    sid = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO schedules (id, schedule_id, workspace_id, name, status, created_by, "
            "created_at, updated_at) VALUES (:i, :s, :w, :n, 'DRAFT', :c, :t, :t)"
        ),
        {"i": sid, "s": schedule_id, "w": workspace_id, "n": name, "c": created_by, "t": now},
    )
    return sid


def update_schedule(session: Session, schedule_id: str, now: dt.datetime, **cols: Any) -> None:
    assignments = ", ".join(f"{k} = :{k}" for k in cols)
    session.execute(
        text(f"UPDATE schedules SET {assignments}, updated_at = :now WHERE schedule_id = :s"),  # noqa: S608 - constant column list, bound parameters
        {**cols, "now": now, "s": schedule_id},
    )


# -------------------------------------------------------------------------------- versions
_VERSION_COLS = (
    "id, schedule_version_id, schedule_id, version, name, channel_id, cron_expression, timezone, "
    "execution_principal_id, agent_selection, action_template, concurrency_policy, "
    "missed_run_policy, backfill_limit, backfill_window_seconds, max_duration_seconds, "
    "min_interval_minutes, retry_policy, budget_policy, documentation_policy, starts_at, ends_at, "
    "snapshot_hash, created_by, event_id, created_at"
)


def _jsonb(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return dict(json.loads(value))


def _version(row: Any) -> VersionRow:
    m = row._mapping
    return VersionRow(
        id=m["id"],
        schedule_version_id=str(m["schedule_version_id"]),
        schedule_id=str(m["schedule_id"]),
        version=int(m["version"]),
        name=str(m["name"]),
        channel_id=m["channel_id"],
        cron_expression=str(m["cron_expression"]),
        timezone=str(m["timezone"]),
        execution_principal_id=m["execution_principal_id"],
        agent_selection=_jsonb(m["agent_selection"]),
        action_template=_jsonb(m["action_template"]),
        concurrency_policy=str(m["concurrency_policy"]),
        missed_run_policy=str(m["missed_run_policy"]),
        backfill_limit=int(m["backfill_limit"]),
        backfill_window_seconds=int(m["backfill_window_seconds"]),
        max_duration_seconds=int(m["max_duration_seconds"]),
        min_interval_minutes=int(m["min_interval_minutes"]),
        retry_policy=_jsonb(m["retry_policy"]),
        budget_policy=_jsonb(m["budget_policy"]),
        documentation_policy=_jsonb(m["documentation_policy"]),
        starts_at=m["starts_at"],
        ends_at=m["ends_at"],
        snapshot_hash=str(m["snapshot_hash"]),
        created_by=m["created_by"],
        event_id=m["event_id"],
        created_at=m["created_at"],
    )


def load_version(session: Session, version_uuid: uuid.UUID) -> VersionRow | None:
    row = session.execute(
        text(f"SELECT {_VERSION_COLS} FROM schedule_versions WHERE id = :i"),  # noqa: S608 - constant column list, bound parameters
        {"i": version_uuid},
    ).first()
    return None if row is None else _version(row)


def load_version_by_public_id(session: Session, schedule_version_id: str) -> VersionRow | None:
    row = session.execute(
        text(f"SELECT {_VERSION_COLS} FROM schedule_versions WHERE schedule_version_id = :i"),  # noqa: S608 - constant column list, bound parameters
        {"i": schedule_version_id},
    ).first()
    return None if row is None else _version(row)


def list_versions(session: Session, schedule_id: str) -> list[VersionRow]:
    rows = session.execute(
        text(
            f"SELECT {_VERSION_COLS} FROM schedule_versions "  # noqa: S608 - constant column list, bound parameters
            "WHERE schedule_id = :s ORDER BY version"
        ),
        {"s": schedule_id},
    ).all()
    return [_version(r) for r in rows]


def insert_version(
    session: Session,
    *,
    schedule_id: str,
    version: int,
    content: dict[str, Any],
    created_by: uuid.UUID,
    event_id: str | None,
    now: dt.datetime,
    schedule_version_id: str | None = None,
) -> VersionRow:
    vid = uuid.uuid4()
    public_id = schedule_version_id or new_version_id()
    session.execute(
        text(
            "INSERT INTO schedule_versions (id, schedule_version_id, schedule_id, version, name, "
            "channel_id, cron_expression, timezone, execution_principal_id, agent_selection, "
            "action_template, concurrency_policy, missed_run_policy, backfill_limit, "
            "backfill_window_seconds, max_duration_seconds, min_interval_minutes, retry_policy, "
            "budget_policy, documentation_policy, starts_at, ends_at, snapshot_hash, created_by, "
            "event_id, created_at) VALUES (:i, :pid, :s, :v, :name, :chan, :cron, :tz, :principal, "
            "CAST(:sel AS jsonb), CAST(:tpl AS jsonb), :conc, :missed, :bfl, :bfw, :maxd, :mini, "
            "CAST(:retry AS jsonb), CAST(:budget AS jsonb), CAST(:doc AS jsonb), :starts, :ends, "
            ":hash, :by, :ev, :now)"
        ),
        {
            "i": vid,
            "pid": public_id,
            "s": schedule_id,
            "v": version,
            "name": content["name"],
            "chan": uuid.UUID(str(content["channel_id"])),
            "cron": content["cron_expression"],
            "tz": content["timezone"],
            "principal": uuid.UUID(str(content["execution_principal_id"])),
            "sel": json.dumps(content["agent_selection"]),
            "tpl": json.dumps(content["action_template"]),
            "conc": content["concurrency_policy"],
            "missed": content["missed_run_policy"],
            "bfl": int(content["backfill_limit"]),
            "bfw": int(content["backfill_window_seconds"]),
            "maxd": int(content["max_duration_seconds"]),
            "mini": int(content.get("min_interval_minutes", 5)),
            "retry": json.dumps(content["retry_policy"]),
            "budget": json.dumps(content["budget_policy"]),
            "doc": json.dumps(content["documentation_policy"]),
            "starts": parse_ts(content.get("starts_at")),
            "ends": parse_ts(content.get("ends_at")),
            "hash": snapshot_hash(content),
            "by": created_by,
            "ev": event_id,
            "now": now,
        },
    )
    loaded = load_version(session, vid)
    assert loaded is not None
    return loaded


# ------------------------------------------------------------------------------------ runs
_RUN_COLS = (
    "r.id, r.run_id, r.workspace_id, r.schedule_id, r.schedule_version_id, r.run_kind, "
    "r.occurrence_key, r.scheduled_for, r.local_scheduled_for, r.retry_of_run_id, r.request_key, "
    "r.status, r.attempt_count, r.task_id, r.idempotency_key, r.version_hash, r.claimed_by, "
    "r.claimed_at, r.lease_expires_at, r.heartbeat_at, r.started_at, r.finished_at, "
    "r.result_event_id, r.error_code, r.cancel_requested_at, r.cancelled_at, r.planner_note, "
    "r.requested_by, r.created_at, r.updated_at, v.schedule_version_id AS version_public_id, "
    "v.version AS version_no"
)
_RUN_FROM = "FROM schedule_runs r JOIN schedule_versions v ON v.id = r.schedule_version_id"


def _run(row: Any) -> RunRow:
    m = row._mapping
    return RunRow(
        id=m["id"],
        run_id=str(m["run_id"]),
        workspace_id=m["workspace_id"],
        schedule_id=str(m["schedule_id"]),
        schedule_version_id=m["schedule_version_id"],
        run_kind=str(m["run_kind"]),
        occurrence_key=m["occurrence_key"],
        scheduled_for=m["scheduled_for"],
        local_scheduled_for=m["local_scheduled_for"],
        retry_of_run_id=m["retry_of_run_id"],
        request_key=m["request_key"],
        status=str(m["status"]),
        attempt_count=int(m["attempt_count"]),
        task_id=m["task_id"],
        idempotency_key=str(m["idempotency_key"]),
        version_hash=str(m["version_hash"]),
        claimed_by=m["claimed_by"],
        claimed_at=m["claimed_at"],
        lease_expires_at=m["lease_expires_at"],
        heartbeat_at=m["heartbeat_at"],
        started_at=m["started_at"],
        finished_at=m["finished_at"],
        result_event_id=m["result_event_id"],
        error_code=m["error_code"],
        cancel_requested_at=m["cancel_requested_at"],
        cancelled_at=m["cancelled_at"],
        planner_note=m["planner_note"],
        requested_by=m["requested_by"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        version_public_id=m["version_public_id"],
        version_no=int(m["version_no"]),
    )


def load_run(
    session: Session,
    run_id: str,
    *,
    workspace_id: uuid.UUID | None = None,
    for_update: bool = False,
) -> RunRow | None:
    cond = " AND r.workspace_id = :w" if workspace_id is not None else ""
    lock = " FOR UPDATE OF r" if for_update else ""
    row = session.execute(
        text(f"SELECT {_RUN_COLS} {_RUN_FROM} WHERE r.run_id = :r{cond}{lock}"),
        {"r": run_id, "w": workspace_id},
    ).first()
    return None if row is None else _run(row)


def list_runs(
    session: Session,
    schedule_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
    before: dt.datetime | None = None,
) -> list[RunRow]:
    rows = session.execute(
        text(
            f"SELECT {_RUN_COLS} {_RUN_FROM} WHERE r.schedule_id = :s "
            "AND (CAST(:st AS text) IS NULL OR r.status = CAST(:st AS text)) "
            "AND (CAST(:before AS timestamptz) IS NULL "
            "OR r.scheduled_for < CAST(:before AS timestamptz)) "
            "ORDER BY r.scheduled_for DESC, r.run_id DESC LIMIT :lim"
        ),
        {"s": schedule_id, "st": status, "before": before, "lim": limit},
    ).all()
    return [_run(r) for r in rows]


def active_runs(session: Session, schedule_id: str) -> list[RunRow]:
    rows = session.execute(
        text(
            f"SELECT {_RUN_COLS} {_RUN_FROM} WHERE r.schedule_id = :s AND r.status IN "
            "('CLAIMED','TASK_CREATED','RUNNING','VERIFYING','CANCEL_REQUESTED') "
            "ORDER BY r.scheduled_for"
        ),
        {"s": schedule_id},
    ).all()
    return [_run(r) for r in rows]


def pending_runs(session: Session, schedule_id: str, *, for_update: bool = False) -> list[RunRow]:
    lock = " FOR UPDATE OF r" if for_update else ""
    rows = session.execute(
        text(
            f"SELECT {_RUN_COLS} {_RUN_FROM} WHERE r.schedule_id = :s "
            f"AND r.status IN ('PENDING','DUE') ORDER BY r.scheduled_for{lock}"
        ),
        {"s": schedule_id},
    ).all()
    return [_run(r) for r in rows]


def find_run_by_request_key(
    session: Session, schedule_id: str, run_kind: str, request_key: str
) -> RunRow | None:
    row = session.execute(
        text(
            f"SELECT {_RUN_COLS} {_RUN_FROM} WHERE r.schedule_id = :s AND r.run_kind = :k "
            "AND r.request_key = :q"
        ),
        {"s": schedule_id, "k": run_kind, "q": request_key},
    ).first()
    return None if row is None else _run(row)


def insert_run(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    schedule_id: str,
    version: VersionRow,
    run_kind: str,
    scheduled_for: dt.datetime,
    idempotency_key: str,
    status: str,
    now: dt.datetime,
    occurrence_key: str | None = None,
    local_scheduled_for: dt.datetime | None = None,
    retry_of_run_id: str | None = None,
    request_key: str | None = None,
    requested_by: uuid.UUID | None = None,
    planner_note: str | None = None,
    run_id: str | None = None,
) -> RunRow | None:
    """Insert exactly once per ``(schedule_id, occurrence_key)`` / idempotency key.

    Returns the new row, or ``None`` when a conflicting row already exists (the caller reads the
    existing one)."""
    rid = run_id or new_run_id()
    row = session.execute(
        text(
            "INSERT INTO schedule_runs (id, run_id, workspace_id, "
            "schedule_id, schedule_version_id, "
            "run_kind, occurrence_key, scheduled_for, local_scheduled_for, retry_of_run_id, "
            "request_key, status, idempotency_key, version_hash, requested_by, planner_note, "
            "created_at, updated_at) VALUES (:i, :rid, :w, :s, :v, :kind, :occ, :sf, :lsf, :retry, "
            ":rq, :st, :idem, :hash, :by, :note, :now, :now) "
            "ON CONFLICT DO NOTHING RETURNING run_id"
        ),
        {
            "i": uuid.uuid4(),
            "rid": rid,
            "w": workspace_id,
            "s": schedule_id,
            "v": version.id,
            "kind": run_kind,
            "occ": occurrence_key,
            "sf": scheduled_for,
            "lsf": local_scheduled_for,
            "retry": retry_of_run_id,
            "rq": request_key,
            "st": status,
            "idem": idempotency_key,
            "hash": version.snapshot_hash,
            "by": requested_by,
            "note": planner_note,
            "now": now,
        },
    ).first()
    if row is None:
        return None
    return load_run(session, rid)


def update_run(session: Session, run_id: str, now: dt.datetime, **cols: Any) -> None:
    assignments = ", ".join(f"{k} = :{k}" for k in cols)
    session.execute(
        text(f"UPDATE schedule_runs SET {assignments}, updated_at = :now WHERE run_id = :r"),  # noqa: S608 - constant column list, bound parameters
        {**cols, "now": now, "r": run_id},
    )


def attempts_of(session: Session, run_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT attempt_no, started_at, finished_at, result, error_code, runner_id "
            "FROM schedule_run_attempts WHERE run_id = :r ORDER BY attempt_no"
        ),
        {"r": run_id},
    ).all()
    return [
        {
            "attempt_no": int(r[0]),
            "started_at": iso_ms(r[1]),
            "finished_at": iso_ms(r[2]),
            "result": r[3],
            "error_code": r[4],
            "runner_id": r[5],
        }
        for r in rows
    ]


def add_attempt(
    session: Session,
    run_id: str,
    *,
    runner_id: str,
    started_at: dt.datetime,
    finished_at: dt.datetime | None = None,
    result: str | None = None,
    error_code: str | None = None,
) -> int:
    """Append an attempt row and keep ``attempt_count`` equal to the row count (§6.6)."""
    attempt_no = int(
        session.execute(
            text(
                "SELECT COALESCE(max(attempt_no), 0) + 1 FROM schedule_run_attempts "
                "WHERE run_id = :r"
            ),
            {"r": run_id},
        ).scalar_one()
    )
    session.execute(
        text(
            "INSERT INTO schedule_run_attempts (id, run_id, attempt_no, started_at, finished_at, "
            "result, error_code, runner_id) VALUES (:i, :r, :n, :s, :f, :res, :e, :runner)"
        ),
        {
            "i": uuid.uuid4(),
            "r": run_id,
            "n": attempt_no,
            "s": started_at,
            "f": finished_at,
            "res": result,
            "e": error_code,
            "runner": runner_id,
        },
    )
    session.execute(
        text(
            "UPDATE schedule_runs SET attempt_count = (SELECT count(*) FROM schedule_run_attempts "
            "WHERE run_id = :r) WHERE run_id = :r"
        ),
        {"r": run_id},
    )
    return attempt_no


def finish_attempt(
    session: Session,
    run_id: str,
    attempt_no: int,
    *,
    finished_at: dt.datetime,
    result: str,
    error_code: str | None,
) -> None:
    session.execute(
        text(
            "UPDATE schedule_run_attempts SET finished_at = :f, result = :res, error_code = :e "
            "WHERE run_id = :r AND attempt_no = :n"
        ),
        {"f": finished_at, "res": result, "e": error_code, "r": run_id, "n": attempt_no},
    )


def add_planner_note(
    session: Session,
    *,
    schedule_id: str,
    occurrence_key: str,
    reason: str,
    now: dt.datetime,
    local_time: str | None = None,
    scheduled_for: dt.datetime | None = None,
    detail: str | None = None,
) -> bool:
    row = session.execute(
        text(
            "INSERT INTO schedule_planner_notes (schedule_id, occurrence_key, local_time, "
            "scheduled_for, reason, detail, noted_at) VALUES (:s, :k, :l, :sf, :r, :d, :now) "
            "ON CONFLICT (schedule_id, occurrence_key, reason) DO NOTHING RETURNING id"
        ),
        {
            "s": schedule_id,
            "k": occurrence_key,
            "l": local_time,
            "sf": scheduled_for,
            "r": reason,
            "d": detail,
            "now": now,
        },
    ).first()
    return row is not None


def planner_notes(session: Session, schedule_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT occurrence_key, local_time, scheduled_for, reason, detail, noted_at "
            "FROM schedule_planner_notes WHERE schedule_id = :s ORDER BY id DESC LIMIT :lim"
        ),
        {"s": schedule_id, "lim": limit},
    ).all()
    return [
        {
            "occurrence_key": r[0],
            "local_time": r[1],
            "scheduled_for": iso_ms(r[2]),
            "reason": r[3],
            "detail": r[4],
            "noted_at": iso_ms(r[5]),
        }
        for r in rows
    ]


def system_actor(session: Session, workspace_id: uuid.UUID, fallback: uuid.UUID) -> uuid.UUID:
    """The Workspace's system service Account (lowest service account_id), else ``fallback``."""
    row = session.execute(
        text(
            "SELECT id FROM accounts WHERE workspace_id = :w AND account_type = 'service' "
            "ORDER BY account_id LIMIT 1"
        ),
        {"w": workspace_id},
    ).first()
    return fallback if row is None else uuid.UUID(str(row[0]))
