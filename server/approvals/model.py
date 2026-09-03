"""Approval states, transitions, and subject-type registry (spec §8.4, development plan §6.7)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.defaults import APPROVAL_EXPIRY_HOURS, APPROVAL_REMINDER_RATIO


class ApprovalError(ValueError):
    def __init__(self, code: str, detail: str, status: int = 409) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"  # spec §8.4 REQUESTED/PENDING: created by APPROVAL_REQUESTED
    APPROVED = "APPROVED"
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    CONSUMED = "CONSUMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


TERMINAL: frozenset[ApprovalStatus] = frozenset(
    {
        ApprovalStatus.CONSUMED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.CANCELLED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.REVOKED,
    }
)
CONSUMABLE: frozenset[ApprovalStatus] = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.PARTIALLY_CONSUMED}
)

# (from, event) -> to; exactly the paths of spec §8.4
TRANSITIONS: dict[tuple[ApprovalStatus, str], ApprovalStatus] = {
    (ApprovalStatus.PENDING, "APPROVAL_GRANTED"): ApprovalStatus.APPROVED,
    (ApprovalStatus.PENDING, "APPROVAL_REJECTED"): ApprovalStatus.REJECTED,
    (ApprovalStatus.PENDING, "APPROVAL_CANCELLED"): ApprovalStatus.CANCELLED,
    (ApprovalStatus.PENDING, "APPROVAL_EXPIRED"): ApprovalStatus.EXPIRED,
    (ApprovalStatus.PENDING, "APPROVAL_REVOKED"): ApprovalStatus.REVOKED,
    (ApprovalStatus.APPROVED, "APPROVAL_CONSUMED"): ApprovalStatus.PARTIALLY_CONSUMED,
    (ApprovalStatus.APPROVED, "APPROVAL_CANCELLED"): ApprovalStatus.CANCELLED,
    (ApprovalStatus.APPROVED, "APPROVAL_EXPIRED"): ApprovalStatus.EXPIRED,
    (ApprovalStatus.APPROVED, "APPROVAL_REVOKED"): ApprovalStatus.REVOKED,
    (ApprovalStatus.PARTIALLY_CONSUMED, "APPROVAL_CONSUMED"): ApprovalStatus.PARTIALLY_CONSUMED,
    (ApprovalStatus.PARTIALLY_CONSUMED, "APPROVAL_CANCELLED"): ApprovalStatus.CANCELLED,
    (ApprovalStatus.PARTIALLY_CONSUMED, "APPROVAL_EXPIRED"): ApprovalStatus.EXPIRED,
    (ApprovalStatus.PARTIALLY_CONSUMED, "APPROVAL_REVOKED"): ApprovalStatus.REVOKED,
}


def next_status(current: ApprovalStatus, event_type: str) -> ApprovalStatus:
    if current in TERMINAL:
        raise ApprovalError("APPROVAL_TERMINAL", f"{current} is terminal")
    target = TRANSITIONS.get((current, event_type))
    if target is None:
        raise ApprovalError("APPROVAL_TRANSITION_INVALID", f"{current} + {event_type}")
    return target


def status_after_consumption(used_count: int, max_uses: int | None) -> ApprovalStatus:
    """CONSUMED once the last allowed use is taken; otherwise PARTIALLY_CONSUMED."""
    if max_uses is not None and used_count >= max_uses:
        return ApprovalStatus.CONSUMED
    return ApprovalStatus.PARTIALLY_CONSUMED


def default_expiry(valid_from: dt.datetime) -> dt.datetime:
    return valid_from + dt.timedelta(hours=APPROVAL_EXPIRY_HOURS)


def reminder_at(valid_from: dt.datetime, expires_at: dt.datetime) -> dt.datetime:
    """Reminder at 50% of the validity window (development plan §7E)."""
    return valid_from + (expires_at - valid_from) * APPROVAL_REMINDER_RATIO


# ---------------------------------------------------------------------------- subjects
SUBJECT_TYPES: tuple[str, ...] = ("task", "schedule", "run", "action")
# activating phase per subject (development plan §6.7): task/action in Phase 1, schedule/run in 5
SUBJECT_ACTIVATION: dict[str, int] = {"task": 1, "schedule": 5, "run": 5, "action": 1}
CURRENT_PHASE = 5  # schedule/run subjects activate with Phase 5 (development plan §6.7)


@dataclass(frozen=True)
class Subject:
    subject_type: str
    subject_id: str


def validate_subject(session: Session, workspace_uuid: uuid.UUID, subject: Subject) -> None:
    """Exactly one target identifier; only subjects whose phase has arrived may be used."""
    if subject.subject_type not in SUBJECT_TYPES:
        raise ApprovalError("SUBJECT_TYPE_UNKNOWN", subject.subject_type, status=400)
    if not subject.subject_id:
        raise ApprovalError("SUBJECT_ID_REQUIRED", subject.subject_type, status=400)
    activates = SUBJECT_ACTIVATION[subject.subject_type]
    if activates > CURRENT_PHASE:
        raise ApprovalError(
            "SUBJECT_TYPE_NOT_ACTIVE",
            f"{subject.subject_type} subjects activate in Phase {activates}",
        )
    tables = {
        "task": ("tasks_projection", "task_id"),
        "schedule": ("schedules", "schedule_id"),
        "run": ("schedule_runs", "run_id"),
    }
    if subject.subject_type in tables:
        table, column = tables[subject.subject_type]
        row = session.execute(
            text(f"SELECT workspace_id FROM {table} WHERE {column} = :t"),  # noqa: S608 - fixed map
            {"t": subject.subject_id},
        ).first()
        if row is None:
            raise ApprovalError("SUBJECT_NOT_FOUND", subject.subject_id, status=404)
        if uuid.UUID(str(row[0])) != workspace_uuid:
            raise ApprovalError("WORKSPACE_MISMATCH", subject.subject_id, status=404)


@dataclass(frozen=True)
class Grant:
    approval_id: str
    workspace_uuid: uuid.UUID
    subject: Subject
    action: str
    risk: str
    status: ApprovalStatus
    requested_by: uuid.UUID
    implementing_agent_account: uuid.UUID | None
    channel_uuid: uuid.UUID | None
    valid_from: dt.datetime
    expires_at: dt.datetime
    max_uses: int | None
    quorum_required: int
    aggregate_seq: int
    requires_human_approval: bool = False

    def usable_at(self, now: dt.datetime) -> bool:
        return self.status in CONSUMABLE and self.valid_from <= now < self.expires_at
