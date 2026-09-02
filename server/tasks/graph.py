"""Pure task-graph rules (spec §4.2, development plan §16 P3-09). No I/O.

A Task graph is acyclic (``root_task_id``/``parent_task_id``); channel policy limits delegation
depth, fan-out and concurrent sub-Tasks; a parent joins its children with ``ALL``, ``ANY`` or
``QUORUM(n)``. Every rejection is a stable error code with zero side effects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

DEFAULT_DELEGATION_DEPTH = 4
DEFAULT_MAX_FAN_OUT = 8
DEFAULT_CONCURRENT_SUBTASKS = 8
JOIN_MODES = ("ALL", "ANY", "QUORUM")
DONE_STATUSES = frozenset({"VERIFIED", "COMPLETED"})


class GraphError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class GraphLimits:
    delegation_depth: int = DEFAULT_DELEGATION_DEPTH
    max_fan_out: int = DEFAULT_MAX_FAN_OUT
    concurrent_subtasks: int = DEFAULT_CONCURRENT_SUBTASKS


def limits_from_policy(limits: Mapping[str, Any] | None) -> GraphLimits:
    """Channel template ``limits`` → graph limits; absent or zero values keep the defaults."""
    limits = limits or {}

    def pick(key: str, default: int) -> int:
        value = limits.get(key)
        return int(value) if isinstance(value, int) and value > 0 else default

    return GraphLimits(
        delegation_depth=pick("delegation_depth", DEFAULT_DELEGATION_DEPTH),
        max_fan_out=pick("max_fan_out", DEFAULT_MAX_FAN_OUT),
        concurrent_subtasks=pick("concurrent_subtasks", DEFAULT_CONCURRENT_SUBTASKS),
    )


@dataclass(frozen=True)
class ParentView:
    task_id: str
    workspace_id: str
    depth: int  # delegation depth of the parent (root = 0)
    status: str


def validate_new_child(
    parent: ParentView,
    child_task_id: str,
    *,
    actor_workspace_id: str,
    ancestors: Sequence[str],
    sibling_count: int,
    active_sibling_count: int,
    limits: GraphLimits,
) -> None:
    """Reject cycles, cross-Workspace parents and limit overruns (V-P3-19)."""
    if child_task_id == parent.task_id or child_task_id in ancestors:
        raise GraphError("TASK_GRAPH_CYCLE", f"{child_task_id} is {parent.task_id} or an ancestor")
    if parent.workspace_id != actor_workspace_id:
        raise GraphError("TASK_WORKSPACE_MISMATCH", "parent belongs to another Workspace")
    if parent.status in ("COMPLETED", "CANCELLED"):
        raise GraphError("TASK_TERMINAL", f"parent {parent.task_id} is terminal")
    if parent.depth + 1 > limits.delegation_depth:
        raise GraphError(
            "TASK_DEPTH_EXCEEDED", f"depth {parent.depth + 1} > {limits.delegation_depth}"
        )
    if sibling_count + 1 > limits.max_fan_out:
        raise GraphError(
            "TASK_FANOUT_EXCEEDED", f"fan-out {sibling_count + 1} > {limits.max_fan_out}"
        )
    if active_sibling_count + 1 > limits.concurrent_subtasks:
        raise GraphError(
            "TASK_CONCURRENCY_EXCEEDED",
            f"concurrent sub-Tasks {active_sibling_count + 1} > {limits.concurrent_subtasks}",
        )


@dataclass(frozen=True)
class ChildView:
    task_id: str
    status: str
    verification_status: str | None = None

    @property
    def done(self) -> bool:
        """Verified by an independent Verifier (VERIFIED/COMPLETED imply VERIFICATION PASSED)."""
        return self.status in DONE_STATUSES

    @property
    def cancelled(self) -> bool:
        return self.status == "CANCELLED"


@dataclass(frozen=True)
class JoinResult:
    mode: str
    satisfied: bool
    satisfied_children: tuple[str, ...]
    missing: tuple[str, ...] = ()
    quorum: int | None = None
    detail: str = ""


def join_mode(policy: Mapping[str, Any] | None) -> tuple[str, int | None, tuple[str, ...]]:
    policy = policy or {}
    mode = str(policy.get("mode", policy.get("join", "ALL"))).upper()
    if mode not in JOIN_MODES:
        raise GraphError("TASK_JOIN_POLICY_INVALID", f"unknown join mode {mode}")
    quorum = policy.get("quorum")
    if mode == "QUORUM":
        if not isinstance(quorum, int) or quorum < 1:
            raise GraphError("TASK_JOIN_POLICY_INVALID", "QUORUM requires quorum >= 1")
    required = tuple(str(x) for x in policy.get("required", []))
    return mode, (int(quorum) if mode == "QUORUM" and quorum is not None else None), required


def evaluate_join(policy: Mapping[str, Any] | None, children: Sequence[ChildView]) -> JoinResult:
    """Decide whether the parent's join condition is met (V-P3-18).

    - ``ALL``: every non-cancelled child is done and every *required* child is done (a required
      child that is cancelled or unverified blocks the join; an unverified sub-Task never counts).
    - ``ANY``: at least one child is done.
    - ``QUORUM(n)``: at least ``n`` children are done and every required child is done.
    """
    mode, quorum, required = join_mode(policy)
    done = tuple(c.task_id for c in children if c.done)
    by_id = {c.task_id: c for c in children}
    missing_required = tuple(r for r in required if r not in done)
    if not children:
        return JoinResult(mode, False, (), detail="no sub-Tasks yet")
    if mode == "ALL":
        pending = tuple(c.task_id for c in children if not c.done and not c.cancelled)
        missing = tuple(dict.fromkeys(pending + missing_required))
        # a required child that was cancelled can never satisfy ALL
        missing += tuple(
            r for r in required if r in by_id and by_id[r].cancelled and r not in missing
        )
        return JoinResult(mode, not missing and bool(done), done, missing)
    if mode == "ANY":
        return JoinResult(mode, bool(done), done, () if done else tuple(by_id))
    assert quorum is not None
    satisfied = len(done) >= quorum and not missing_required
    missing = (
        missing_required
        if len(done) >= quorum
        else tuple(c.task_id for c in children if not c.done)
    )
    return JoinResult(mode, satisfied, done, missing, quorum=quorum)


@dataclass
class GraphSnapshot:
    """In-memory helper for unit tests and dry runs: parent → children."""

    children: dict[str, list[str]] = field(default_factory=dict)
    parent: dict[str, str] = field(default_factory=dict)

    def ancestors(self, task_id: str) -> list[str]:
        out: list[str] = []
        cur = self.parent.get(task_id)
        while cur is not None and cur not in out:
            out.append(cur)
            cur = self.parent.get(cur)
        return out
