"""Seed helpers over the real schedule tables (migration 0016, owned by the core package) for
the metrics tests: one Schedule with a pinned ScheduleVersion, then Runs in any state."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def ensure_channel(session: Session, workspace_id: uuid.UUID, channel_id: str) -> uuid.UUID:
    row = session.execute(
        text("SELECT id FROM channels WHERE channel_id = :c"), {"c": channel_id}
    ).first()
    if row is not None:
        return uuid.UUID(str(row[0]))
    cid = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
            "VALUES (:i, :c, :w, 'work', :c)"
        ),
        {"i": cid, "c": channel_id, "w": workspace_id},
    )
    return cid


def insert_schedule(
    session: Session,
    workspace_id: uuid.UUID,
    schedule_id: str,
    *,
    created_by: uuid.UUID,
    channel_uuid: uuid.UUID,
    name: str = "s",
    status: str = "ENABLED",
    next_run_at: dt.datetime | None = None,
) -> tuple[uuid.UUID, str]:
    """Insert a Schedule and its version 1; returns (version uuid, version hash)."""
    sid = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO schedules (id, schedule_id, workspace_id, name, status, next_run_at, "
            "created_by) VALUES (:i, :s, :w, :n, :st, :nx, :by) "
            "ON CONFLICT (schedule_id) DO NOTHING"
        ),
        {
            "i": sid,
            "s": schedule_id,
            "w": workspace_id,
            "n": name,
            "st": status,
            "nx": next_run_at,
            "by": created_by,
        },
    )
    snapshot = {"schedule_id": schedule_id, "cron": "*/5 * * * *", "tz": "UTC"}
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    vid = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO schedule_versions (id, schedule_version_id, schedule_id, version, name, "
            "channel_id, cron_expression, timezone, execution_principal_id, agent_selection, "
            "action_template, concurrency_policy, missed_run_policy, backfill_limit, "
            "backfill_window_seconds, max_duration_seconds, retry_policy, budget_policy, "
            "documentation_policy, snapshot_hash, created_by) VALUES (:i, :sv, :s, 1, :n, :c, "
            "'*/5 * * * *', 'UTC', :p, CAST('{\"mode\": \"capability\"}' AS jsonb), "
            "CAST('{\"action\": \"task_create\"}' AS jsonb), 'FORBID', 'RUN_ONCE', 0, 0, 3600, "
            "CAST('{\"max_attempts\": 3}' AS jsonb), CAST('{}' AS jsonb), CAST('{}' AS jsonb), "
            ":h, :by) ON CONFLICT (schedule_id, version) DO NOTHING"
        ),
        {
            "i": vid,
            "sv": f"sv-{schedule_id}-1",
            "s": schedule_id,
            "n": name,
            "c": channel_uuid,
            "p": created_by,
            "h": digest,
            "by": created_by,
        },
    )
    session.execute(
        text("UPDATE schedules SET current_version_id = :v WHERE schedule_id = :s"),
        {"v": vid, "s": schedule_id},
    )
    row = session.execute(
        text(
            "SELECT id, snapshot_hash FROM schedule_versions WHERE schedule_id = :s AND version = 1"
        ),
        {"s": schedule_id},
    ).first()
    assert row is not None
    return uuid.UUID(str(row[0])), str(row[1])


def insert_run(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    schedule_id: str,
    version: tuple[uuid.UUID, str],
    **cols: Any,
) -> str:
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "run_id": f"run-{uuid.uuid4().hex[:12]}",
        "workspace_id": workspace_id,
        "schedule_id": schedule_id,
        "schedule_version_id": version[0],
        "run_kind": "SCHEDULED",
        "occurrence_key": uuid.uuid4().hex,
        "scheduled_for": dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        "status": "PENDING",
        "idempotency_key": f"SCHEDULED:{schedule_id}:{uuid.uuid4().hex}",
        "version_hash": version[1],
        "claimed_by": None,
        "claimed_at": None,
        "lease_expires_at": None,
        "started_at": None,
        "finished_at": None,
        "error_code": None,
        "retry_of_run_id": None,
        "planner_note": None,
    }
    row.update(cols)
    session.execute(
        text(
            "INSERT INTO schedule_runs (id, run_id, workspace_id, schedule_id, "
            "schedule_version_id, run_kind, occurrence_key, scheduled_for, status, "
            "idempotency_key, version_hash, claimed_by, claimed_at, lease_expires_at, "
            "started_at, finished_at, error_code, retry_of_run_id, planner_note) VALUES "
            "(:id, :run_id, :workspace_id, :schedule_id, :schedule_version_id, :run_kind, "
            ":occurrence_key, :scheduled_for, :status, :idempotency_key, :version_hash, "
            ":claimed_by, :claimed_at, :lease_expires_at, :started_at, :finished_at, "
            ":error_code, :retry_of_run_id, :planner_note)"
        ),
        row,
    )
    return str(row["run_id"])
