"""Work item state machine and timing model (development plan §7B.1, §7B.4, §21.1).

Pure functions: no clock, no I/O. Every rejection carries a stable error code so REST, MCP, and
fixtures report identical failures (V-P0-17, V-P1-29).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum

from server.domain import defaults


class WorkItemState(StrEnum):
    QUEUED = "QUEUED"
    DELIVERED = "DELIVERED"
    ACKED = "ACKED"
    IN_PROGRESS = "IN_PROGRESS"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset(
    {
        WorkItemState.RESULT_RECEIVED,
        WorkItemState.REJECTED,
        WorkItemState.EXPIRED,
        WorkItemState.CANCELLED,
    }
)

# (from_state, transition) -> to_state. `redeliver` keeps DELIVERED but increments delivery_count.
TRANSITIONS: dict[tuple[WorkItemState, str], WorkItemState] = {
    (WorkItemState.QUEUED, "deliver"): WorkItemState.DELIVERED,
    (WorkItemState.QUEUED, "cancel"): WorkItemState.CANCELLED,
    (WorkItemState.QUEUED, "expire"): WorkItemState.EXPIRED,
    (WorkItemState.DELIVERED, "redeliver"): WorkItemState.DELIVERED,
    (WorkItemState.DELIVERED, "ack"): WorkItemState.ACKED,
    (WorkItemState.DELIVERED, "reject"): WorkItemState.REJECTED,
    (WorkItemState.DELIVERED, "expire"): WorkItemState.EXPIRED,
    (WorkItemState.DELIVERED, "cancel"): WorkItemState.CANCELLED,
    (WorkItemState.ACKED, "start"): WorkItemState.IN_PROGRESS,
    (WorkItemState.ACKED, "reject"): WorkItemState.REJECTED,
    (WorkItemState.ACKED, "expire"): WorkItemState.EXPIRED,
    (WorkItemState.ACKED, "cancel"): WorkItemState.CANCELLED,
    (WorkItemState.ACKED, "result"): WorkItemState.RESULT_RECEIVED,
    (WorkItemState.IN_PROGRESS, "result"): WorkItemState.RESULT_RECEIVED,
    (WorkItemState.IN_PROGRESS, "reject"): WorkItemState.REJECTED,
    (WorkItemState.IN_PROGRESS, "expire"): WorkItemState.EXPIRED,
    (WorkItemState.IN_PROGRESS, "cancel"): WorkItemState.CANCELLED,
}

# Event type recorded for each transition (spec §9.3 + documented extensions).
TRANSITION_EVENTS: dict[str, str] = {
    "queue": "WORK_ITEM_QUEUED",
    "deliver": "WORK_ITEM_DELIVERED",
    "redeliver": "WORK_ITEM_DELIVERED",
    "ack": "WORK_ITEM_ACKED",
    "start": "WORK_ITEM_STARTED",
    "result": "WORK_ITEM_RESULT_RECEIVED",
    "reject": "WORK_ITEM_REJECTED",
    "expire": "WORK_ITEM_EXPIRED",
    "cancel": "WORK_ITEM_CANCELLED",
}

REJECTION_CODES = frozenset({"CAPABILITY_UNSUPPORTED", "CAPACITY", "POLICY", "OTHER"})

# Kinds that additionally carry the §7D.3 accept timeout (task_accept within 120 s).
ASSIGNMENT_KINDS = frozenset({"task_assignment", "subtask_assignment"})


class WorkItemError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def transition(state: WorkItemState, action: str) -> WorkItemState:
    """Apply ``action`` or raise ``WORK_ITEM_TRANSITION_INVALID``; terminal states are immutable."""
    if state in TERMINAL_STATES:
        raise WorkItemError("WORK_ITEM_TRANSITION_INVALID", f"{state} is terminal ({action})")
    try:
        return TRANSITIONS[(state, action)]
    except KeyError:
        raise WorkItemError("WORK_ITEM_TRANSITION_INVALID", f"{action} from {state}") from None


class NextAction(StrEnum):
    NONE = "NONE"
    REDELIVER = "REDELIVER"
    EXPIRE = "EXPIRE"
    REROUTE = "REROUTE"
    WAITING = "WAITING"


@dataclass(frozen=True)
class TimingDecision:
    action: NextAction
    reason: str
    due_at: dt.datetime | None = None


def next_action(
    state: WorkItemState,
    delivered_at: dt.datetime | None,
    acked_at: dt.datetime | None,
    now: dt.datetime,
    delivery_count: int,
    *,
    kind: str = "invoke",
    accepted_at: dt.datetime | None = None,
    reroute_count: int = 0,
    deadline: dt.datetime | None = None,
) -> TimingDecision:
    """Pure timing model.

    - DELIVERED without ACK for 60 s: REDELIVER while ``delivery_count`` (deliveries so far)
      is ≤ 3, i.e. at most 3 redeliveries after the first delivery; afterwards EXPIRE.
    - Assignment kinds ACKED but not accepted within 120 s of the first ack: REROUTE once,
      then WAITING (development plan §7D.3).
    - Any non-terminal item past its deadline: EXPIRE.
    """
    if state in TERMINAL_STATES:
        return TimingDecision(NextAction.NONE, "terminal")
    if deadline is not None and now >= deadline:
        return TimingDecision(NextAction.EXPIRE, "DEADLINE_EXCEEDED")
    ack_timeout = dt.timedelta(seconds=defaults.WORK_ITEM_ACK_TIMEOUT_S)
    if state is WorkItemState.DELIVERED:
        if delivered_at is None:
            raise WorkItemError("WORK_ITEM_TIMING_INVALID", "DELIVERED without delivered_at")
        due = delivered_at + ack_timeout
        if now < due:
            return TimingDecision(NextAction.NONE, "awaiting ack", due)
        if delivery_count <= defaults.WORK_ITEM_MAX_REDELIVERIES:
            return TimingDecision(
                NextAction.REDELIVER, f"ACK_TIMEOUT redelivery {delivery_count} of 3", due
            )
        return TimingDecision(NextAction.EXPIRE, "ACK_TIMEOUT_EXHAUSTED", due)
    if state in (WorkItemState.ACKED, WorkItemState.IN_PROGRESS) and kind in ASSIGNMENT_KINDS:
        if accepted_at is not None:
            return TimingDecision(NextAction.NONE, "accepted")
        if acked_at is None:
            raise WorkItemError("WORK_ITEM_TIMING_INVALID", "ACKED without acked_at")
        due = acked_at + dt.timedelta(seconds=defaults.TASK_ASSIGNMENT_ACCEPT_TIMEOUT_S)
        if now < due:
            return TimingDecision(NextAction.NONE, "awaiting accept", due)
        if reroute_count < defaults.TASK_ASSIGNMENT_REROUTES:
            return TimingDecision(NextAction.REROUTE, "ACCEPT_TIMEOUT", due)
        return TimingDecision(NextAction.WAITING, "ACCEPT_TIMEOUT_NO_CANDIDATE", due)
    return TimingDecision(NextAction.NONE, "no timer")


@dataclass(frozen=True)
class ResultReceipt:
    work_item_id: str
    accepted: bool
    code: str  # RESULT_ACCEPTED | DUPLICATE_RESULT_IGNORED
    first_result_ref: str


@dataclass
class ResultLedger:
    """Exactly-once result acceptance per work_item_id (§7B.1). Duplicates are ignored + audited."""

    _results: dict[str, str] = field(default_factory=dict)
    audit: list[dict[str, str]] = field(default_factory=list)

    def accept(self, work_item_id: str, result_ref: str, *, reporter: str) -> ResultReceipt:
        existing = self._results.get(work_item_id)
        if existing is not None:
            self.audit.append(
                {
                    "code": "DUPLICATE_RESULT_IGNORED",
                    "work_item_id": work_item_id,
                    "reporter": reporter,
                    "ignored_result_ref": result_ref,
                    "first_result_ref": existing,
                }
            )
            return ResultReceipt(work_item_id, False, "DUPLICATE_RESULT_IGNORED", existing)
        self._results[work_item_id] = result_ref
        return ResultReceipt(work_item_id, True, "RESULT_ACCEPTED", result_ref)

    def result_of(self, work_item_id: str) -> str | None:
        return self._results.get(work_item_id)
