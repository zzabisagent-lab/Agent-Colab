"""Task state machine (spec §8.2, development plan §6.8). Pure functions, no I/O.

The state of a Task is folded from its Event stream (task aggregate) merged with the results of
the VerificationRuns started on it (``verification_run`` aggregate, envelope ``task_id``). Only
the transitions in ``TRANSITIONS`` exist; everything else is a stable error with zero side
effects. ``COMPLETED`` and ``CANCELLED`` are terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    OPEN = "OPEN"
    DELEGATED = "DELEGATED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    IMPLEMENTED = "IMPLEMENTED"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    COMPLETED = "COMPLETED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


TERMINAL: frozenset[TaskStatus] = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED})

# (current status, event type) -> next status. Exactly spec §8.2.
TRANSITIONS: dict[tuple[TaskStatus, str], TaskStatus] = {
    (TaskStatus.OPEN, "TASK_DELEGATED"): TaskStatus.DELEGATED,
    (TaskStatus.DELEGATED, "TASK_REASSIGNED"): TaskStatus.DELEGATED,
    (TaskStatus.DELEGATED, "TASK_ACCEPTED"): TaskStatus.ACCEPTED,
    (TaskStatus.ACCEPTED, "TASK_STARTED"): TaskStatus.RUNNING,
    (TaskStatus.RUNNING, "TASK_WAITING"): TaskStatus.WAITING,
    (TaskStatus.WAITING, "TASK_STARTED"): TaskStatus.RUNNING,
    (TaskStatus.RUNNING, "TASK_PROGRESS_REPORTED"): TaskStatus.RUNNING,
    (TaskStatus.RUNNING, "IMPLEMENTATION_SUBMITTED"): TaskStatus.IMPLEMENTED,
    (TaskStatus.IMPLEMENTED, "TASK_VERIFICATION_STARTED"): TaskStatus.VERIFYING,
    (TaskStatus.VERIFYING, "VERIFICATION_PASSED"): TaskStatus.VERIFIED,
    (TaskStatus.VERIFYING, "VERIFICATION_FAILED"): TaskStatus.RUNNING,
    (TaskStatus.VERIFYING, "VERIFICATION_BLOCKED"): TaskStatus.WAITING,
    (TaskStatus.VERIFIED, "TASK_COMPLETED"): TaskStatus.COMPLETED,
    (TaskStatus.OPEN, "TASK_CANCELLED"): TaskStatus.CANCELLED,
    (TaskStatus.DELEGATED, "TASK_CANCELLED"): TaskStatus.CANCELLED,
    (TaskStatus.ACCEPTED, "TASK_CANCELLED"): TaskStatus.CANCELLED,
    (TaskStatus.RUNNING, "TASK_CANCEL_REQUESTED"): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.WAITING, "TASK_CANCEL_REQUESTED"): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.IMPLEMENTED, "TASK_CANCEL_REQUESTED"): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.VERIFYING, "TASK_CANCEL_REQUESTED"): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.CANCEL_REQUESTED, "TASK_CANCELLED"): TaskStatus.CANCELLED,
    # §7D.3 re-routing (Phase 3, P3-14): the assignee is lost (reject, accept timeout, offline,
    # revocation, budget overrun) before or during execution → one reassignment (the new
    # assignee accepts again) or WAITING when no candidate exists.
    (TaskStatus.DELEGATED, "TASK_WAITING"): TaskStatus.WAITING,
    (TaskStatus.ACCEPTED, "TASK_WAITING"): TaskStatus.WAITING,
    (TaskStatus.IMPLEMENTED, "TASK_WAITING"): TaskStatus.WAITING,
    (TaskStatus.ACCEPTED, "TASK_REASSIGNED"): TaskStatus.DELEGATED,
    (TaskStatus.RUNNING, "TASK_REASSIGNED"): TaskStatus.DELEGATED,
    (TaskStatus.WAITING, "TASK_REASSIGNED"): TaskStatus.DELEGATED,
}
# Events recorded on the task aggregate that annotate the Task without changing its status
# (P3-09: the parent's join condition became satisfied).
ANNOTATION_EVENTS: frozenset[str] = frozenset({"TASK_JOIN_SATISFIED"})

CREATION_EVENTS: frozenset[str] = frozenset({"TASK_CREATED", "SUBTASK_CREATED"})
VERIFICATION_RESULT_EVENTS: frozenset[str] = frozenset(
    {"VERIFICATION_PASSED", "VERIFICATION_FAILED", "VERIFICATION_BLOCKED"}
)
TASK_EVENT_TYPES: frozenset[str] = (
    CREATION_EVENTS | {e for (_, e) in TRANSITIONS} - VERIFICATION_RESULT_EVENTS
)


class TaskTransitionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def next_status(current: TaskStatus, event_type: str) -> TaskStatus:
    """Return the status after ``event_type`` or raise a stable error (pure)."""
    if current in TERMINAL:
        raise TaskTransitionError("TASK_TERMINAL", f"{current} is terminal; {event_type} rejected")
    target = TRANSITIONS.get((current, event_type))
    if target is None:
        raise TaskTransitionError(
            "TASK_TRANSITION_INVALID", f"{event_type} is not allowed in {current}"
        )
    return target


@dataclass
class TaskState:
    """Folded state of one Task (everything the projection and the handlers need)."""

    task_id: str
    exists: bool = False
    status: TaskStatus = TaskStatus.OPEN
    workspace_id: str | None = None
    root_task_id: str | None = None
    parent_task_id: str | None = None
    channel_id: str | None = None
    title: str = ""
    domain: str = ""
    risk: str = "LOW"
    assignee_account_id: str | None = None
    delegated_by: str | None = None
    delegation_depth: int = 0
    assignment_revision: int = 0
    policy_snapshot_hash: str | None = None
    criteria_revision: int = 0
    latest_progress: str | None = None
    active_verification_id: str | None = None
    verification_status: str | None = None  # PENDING | PASSED | FAILED | BLOCKED
    last_event_id: str | None = None
    last_aggregate_seq: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    join_policy: dict[str, Any] = field(default_factory=dict)
    join_satisfied: bool = False
    join_satisfied_children: list[str] = field(default_factory=list)


def apply_event(state: TaskState, event: dict[str, Any]) -> TaskState:
    """Fold one Event into ``state``. Events that do not concern the Task are ignored."""
    etype = event["type"]
    payload = event.get("payload", {})
    if etype in CREATION_EVENTS:
        if event.get("aggregate_id") != state.task_id:
            return state
        state.exists = True
        state.status = TaskStatus.OPEN
        state.workspace_id = event.get("workspace_id")
        state.root_task_id = payload.get("root_task_id", state.task_id)
        state.parent_task_id = payload.get("parent_task_id")
        state.channel_id = payload.get("channel_id") or event.get("channel_id")
        state.title = payload.get("title", "")
        state.domain = payload.get("domain", "")
        state.risk = payload.get("risk", "LOW")
        state.delegation_depth = int(payload.get("depth", 0))
        state.join_policy = dict(payload.get("join_policy", {}))
        state.created_at = event.get("occurred_at")
        state.updated_at = event.get("occurred_at")
        state.last_event_id = event.get("event_id")
        state.last_aggregate_seq = int(event.get("aggregate_seq", 0))
        return state
    if not state.exists:
        return state
    if etype in VERIFICATION_RESULT_EVENTS:
        if event.get("task_id") != state.task_id:
            return state
        if payload.get("verification_id") != state.active_verification_id:
            return state  # stale or foreign verification: ignored in the fold
        if (state.status, etype) not in TRANSITIONS:
            return state
        state.status = TRANSITIONS[(state.status, etype)]
        state.verification_status = etype.removeprefix("VERIFICATION_")
        state.updated_at = event.get("occurred_at")
        return state
    if event.get("aggregate_id") != state.task_id or event.get("aggregate_type") != "task":
        return state
    if etype in ANNOTATION_EVENTS:
        state.last_event_id = event.get("event_id")
        state.last_aggregate_seq = int(event.get("aggregate_seq", state.last_aggregate_seq))
        state.updated_at = event.get("occurred_at")
        if etype == "TASK_JOIN_SATISFIED":
            state.join_satisfied = True
            state.join_satisfied_children = list(payload.get("satisfied_children", []))
        return state
    state.status = next_status(state.status, etype)
    state.last_event_id = event.get("event_id")
    state.last_aggregate_seq = int(event.get("aggregate_seq", state.last_aggregate_seq))
    state.updated_at = event.get("occurred_at")
    if etype in ("TASK_DELEGATED", "TASK_REASSIGNED"):
        state.assignee_account_id = payload.get("assignee_account_id")
        state.delegated_by = event.get("actor_account_id")
        state.assignment_revision = int(
            payload.get("assignment_revision", state.assignment_revision)
        )
        if payload.get("policy_snapshot_hash"):
            state.policy_snapshot_hash = payload["policy_snapshot_hash"]
    elif etype == "TASK_PROGRESS_REPORTED":
        state.latest_progress = payload.get("summary")
    elif etype == "IMPLEMENTATION_SUBMITTED":
        state.criteria_revision = int(payload.get("criteria_revision", state.criteria_revision))
        state.verification_status = None
    elif etype == "TASK_VERIFICATION_STARTED":
        state.active_verification_id = payload.get("verification_id")
        state.verification_status = "PENDING"
    return state


def fold(task_id: str, events: list[dict[str, Any]]) -> TaskState:
    """Fold a recorded_seq-ordered list of Events (task stream + verification results)."""
    state = TaskState(task_id=task_id)
    for ev in sorted(events, key=lambda e: int(e.get("recorded_seq", 0))):
        apply_event(state, ev)
    return state


# ---------------------------------------------------------------- completion prerequisites
CompletionCheck = Callable[[TaskState, Any], str | None]
"""Returns an error code or None. ``Any`` is the DB session for checks that need it (P1-10)."""

COMPLETION_CHECKS: list[CompletionCheck] = []


def _verification_passed(state: TaskState, _session: Any) -> str | None:
    if state.status is not TaskStatus.VERIFIED or state.verification_status != "PASSED":
        return "VERIFICATION_REQUIRED"
    return None


def completion_prerequisites(state: TaskState, session: Any = None) -> list[str]:
    """Codes of unmet prerequisites; empty means the Task may complete (spec §8.2, §21.1)."""
    codes = [c for c in (_verification_passed(state, session),) if c]
    codes += [c for c in (check(state, session) for check in COMPLETION_CHECKS) if c]
    return codes


def register_completion_check(check: CompletionCheck) -> None:
    """Later phases add prerequisites (e.g. a FINALIZED Document, P1-10) without edits here."""
    if check not in COMPLETION_CHECKS:
        COMPLETION_CHECKS.append(check)
