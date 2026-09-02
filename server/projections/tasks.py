"""``tasks_projection`` projector (P1-04). Folds task-aggregate Events and verification results
(``verification_run`` Events carrying the Task's ``task_id``) with ``server.domain.task``."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.task import (
    CREATION_EVENTS,
    VERIFICATION_RESULT_EVENTS,
    TaskState,
    TaskStatus,
    TaskTransitionError,
    apply_event,
)
from server.projections.base import register_projector

_UPSERT = text(
    "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, parent_task_id, "
    "channel_id, "
    "title, domain, risk, status, verification_status, assignee_account_id, delegated_by, "
    "delegation_depth, join_policy, policy_snapshot, policy_snapshot_hash, criteria_revision, "
    "latest_progress, last_event_id, last_aggregate_seq, created_at, updated_at) VALUES "
    "(:task_id, :workspace_id, :root_task_id, :parent_task_id, :channel_id, :title, :domain, "
    ":risk, "
    ":status, :verification_status, :assignee_account_id, :delegated_by, :delegation_depth, "
    "CAST(:join_policy AS jsonb), CAST(:policy_snapshot AS jsonb), :policy_snapshot_hash, "
    ":criteria_revision, :latest_progress, :last_event_id, :last_aggregate_seq, :created_at, "
    ":updated_at) ON CONFLICT (task_id) DO UPDATE SET status = EXCLUDED.status, "
    "verification_status = EXCLUDED.verification_status, assignee_account_id = "
    "EXCLUDED.assignee_account_id, delegated_by = EXCLUDED.delegated_by, delegation_depth = "
    "EXCLUDED.delegation_depth, join_policy = EXCLUDED.join_policy, "
    "policy_snapshot = EXCLUDED.policy_snapshot, policy_snapshot_hash = "
    "EXCLUDED.policy_snapshot_hash, criteria_revision = EXCLUDED.criteria_revision, "
    "latest_progress = EXCLUDED.latest_progress, last_event_id = EXCLUDED.last_event_id, "
    "last_aggregate_seq = EXCLUDED.last_aggregate_seq, updated_at = EXCLUDED.updated_at"
)


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def load_state(session: Session, task_id: str) -> TaskState:
    """Read the projection row into a ``TaskState`` (for incremental folding)."""
    row = (
        session.execute(text("SELECT * FROM tasks_projection WHERE task_id = :t"), {"t": task_id})
        .mappings()
        .first()
    )
    state = TaskState(task_id=task_id)
    if row is None:
        return state
    state.exists = True
    state.status = TaskStatus(row["status"])
    state.workspace_id = str(row["workspace_id"])
    state.root_task_id = row["root_task_id"]
    state.parent_task_id = row["parent_task_id"]
    state.channel_id = str(row["channel_id"]) if row["channel_id"] else None
    state.title = row["title"]
    state.domain = row["domain"]
    state.risk = row["risk"]
    state.assignee_account_id = (
        str(row["assignee_account_id"]) if row["assignee_account_id"] else None
    )
    state.delegated_by = str(row["delegated_by"]) if row["delegated_by"] else None
    state.delegation_depth = int(row["delegation_depth"])
    state.join_policy = dict(row["join_policy"] or {})
    state.policy_snapshot_hash = row["policy_snapshot_hash"]
    state.criteria_revision = int(row["criteria_revision"])
    state.latest_progress = row["latest_progress"]
    state.verification_status = row["verification_status"]
    state.last_event_id = row["last_event_id"]
    state.last_aggregate_seq = int(row["last_aggregate_seq"])
    state.created_at = row["created_at"].isoformat() if row["created_at"] else None
    state.updated_at = row["updated_at"].isoformat() if row["updated_at"] else None
    state.active_verification_id = (row["policy_snapshot"] or {}).get("active_verification_id")
    return state


def write_state(session: Session, state: TaskState, occurred_at: Any) -> None:
    """Upsert the projection row from a folded state (``occurred_at`` = the Event's timestamp)."""
    session.execute(
        _UPSERT,
        {
            "task_id": state.task_id,
            "workspace_id": _uuid_or_none(state.workspace_id),
            "root_task_id": state.root_task_id or state.task_id,
            "parent_task_id": state.parent_task_id,
            "channel_id": _uuid_or_none(state.channel_id),
            "title": state.title,
            "domain": state.domain,
            "risk": state.risk,
            "status": state.status.value,
            "verification_status": state.verification_status,
            "assignee_account_id": _uuid_or_none(state.assignee_account_id),
            "delegated_by": _uuid_or_none(state.delegated_by),
            "delegation_depth": state.delegation_depth,
            "join_policy": json.dumps(state.join_policy),
            "policy_snapshot": json.dumps({"active_verification_id": state.active_verification_id}),
            "policy_snapshot_hash": state.policy_snapshot_hash,
            "criteria_revision": state.criteria_revision,
            "latest_progress": state.latest_progress,
            "last_event_id": state.last_event_id,
            "last_aggregate_seq": state.last_aggregate_seq,
            "created_at": state.created_at or occurred_at,
            "updated_at": occurred_at,
        },
    )


class TasksProjector:
    name = "tasks"
    table = "tasks_projection"
    primary_key = "task_id"

    def __init__(self) -> None:
        self.skipped: list[str] = []

    def apply(self, session: Session, event: dict[str, Any]) -> None:
        etype = event["type"]
        if event.get("aggregate_type") == "task":
            task_id = event["aggregate_id"]
        elif etype in VERIFICATION_RESULT_EVENTS and event.get("task_id"):
            task_id = str(event["task_id"])
        else:
            return
        state = load_state(session, task_id)
        if not state.exists and etype not in CREATION_EVENTS:
            return
        try:
            apply_event(state, event)
        except TaskTransitionError as exc:
            # An Event that violates the transition table can only come from outside the command
            # handlers (raw inserts, fixtures). A rebuild never aborts on it: the Event is skipped
            # and reported; the integrity job (server.events.integrity) surfaces such streams.
            self.skipped.append(f"{event['event_id']}: {exc.code}")
            return
        write_state(session, state, event["occurred_at"])


register_projector(TasksProjector())
