"""P3-06/P3-09 pure rules: eligibility ordering and tie-break, graph limits, join evaluation."""

from __future__ import annotations

import pytest

from server.agents.routing import Candidate, supports_secret_handles
from server.tasks.graph import (
    ChildView,
    GraphError,
    GraphLimits,
    ParentView,
    evaluate_join,
    limits_from_policy,
    validate_new_child,
)


def _cand(agent: str, score: int, load: int = 0) -> Candidate:
    return Candidate(agent, f"uuid-{agent}", f"acct-{agent}", "mcp", score, 2, load, score >= 2)


def test_ordering_score_desc_then_agent_id_asc() -> None:
    cands = [_cand("agent-c", 3), _cand("agent-a", 3), _cand("agent-b", 2), _cand("agent-0", 3)]
    cands.sort(key=lambda c: (-c.score, c.agent_id))
    assert [c.agent_id for c in cands] == ["agent-0", "agent-a", "agent-c", "agent-b"]


def test_secret_handles_support_by_adapter_type() -> None:
    assert supports_secret_handles("mcp") and supports_secret_handles("webhook")
    assert not supports_secret_handles("mattermost_bot")


def test_limits_from_policy_defaults_and_overrides() -> None:
    assert limits_from_policy(None) == GraphLimits()
    assert limits_from_policy({"delegation_depth": 2, "max_fan_out": 0}) == GraphLimits(
        delegation_depth=2
    )


@pytest.mark.parametrize(
    ("child", "kwargs", "code"),
    [
        ("task-p", {}, "TASK_GRAPH_CYCLE"),
        ("task-root", {}, "TASK_GRAPH_CYCLE"),
        ("task-c", {"actor_workspace_id": "ws-other"}, "TASK_WORKSPACE_MISMATCH"),
        ("task-c", {"parent_depth": 2}, "TASK_DEPTH_EXCEEDED"),
        ("task-c", {"sibling_count": 3}, "TASK_FANOUT_EXCEEDED"),
        ("task-c", {"active_sibling_count": 2}, "TASK_CONCURRENCY_EXCEEDED"),
    ],
)
def test_graph_rules_reject_with_stable_codes(
    child: str, kwargs: dict[str, object], code: str
) -> None:
    depth = int(kwargs.pop("parent_depth", 1))
    args: dict[str, object] = {
        "actor_workspace_id": "ws-1",
        "ancestors": ["task-root"],
        "sibling_count": 0,
        "active_sibling_count": 0,
        "limits": GraphLimits(delegation_depth=2, max_fan_out=3, concurrent_subtasks=2),
    }
    args.update(kwargs)
    with pytest.raises(GraphError) as exc:
        validate_new_child(ParentView("task-p", "ws-1", depth, "RUNNING"), child, **args)  # type: ignore[arg-type]
    assert exc.value.code == code


def test_graph_rules_accept_within_limits() -> None:
    validate_new_child(
        ParentView("task-p", "ws-1", 1, "RUNNING"),
        "task-c",
        actor_workspace_id="ws-1",
        ancestors=["task-root"],
        sibling_count=2,
        active_sibling_count=1,
        limits=GraphLimits(delegation_depth=2, max_fan_out=3, concurrent_subtasks=2),
    )


def _children(*states: str) -> list[ChildView]:
    return [ChildView(f"t{i}", s) for i, s in enumerate(states)]


def test_join_all_any_quorum() -> None:
    assert not evaluate_join({"mode": "ALL"}, []).satisfied
    assert not evaluate_join({"mode": "ALL"}, _children("VERIFIED", "RUNNING")).satisfied
    assert evaluate_join({"mode": "ALL"}, _children("VERIFIED", "COMPLETED")).satisfied
    # an unverified (IMPLEMENTED) required sub-Task can never satisfy ALL
    assert not evaluate_join({"mode": "ALL"}, _children("VERIFIED", "IMPLEMENTED")).satisfied
    assert evaluate_join({"mode": "ANY"}, _children("VERIFIED", "RUNNING", "OPEN")).satisfied
    assert not evaluate_join({"mode": "ANY"}, _children("RUNNING", "IMPLEMENTED")).satisfied
    q = evaluate_join({"mode": "QUORUM", "quorum": 2}, _children("VERIFIED", "VERIFIED", "OPEN"))
    assert q.satisfied and q.quorum == 2 and q.satisfied_children == ("t0", "t1")
    assert not evaluate_join(
        {"mode": "QUORUM", "quorum": 2}, _children("VERIFIED", "RUNNING", "OPEN")
    ).satisfied
    # a required child that is cancelled blocks ALL; QUORUM needs required children too
    assert not evaluate_join(
        {"mode": "ALL", "required": ["t1"]}, _children("VERIFIED", "CANCELLED")
    ).satisfied
    assert not evaluate_join(
        {"mode": "QUORUM", "quorum": 1, "required": ["t1"]}, _children("VERIFIED", "RUNNING")
    ).satisfied


def test_join_policy_validation() -> None:
    with pytest.raises(GraphError) as exc:
        evaluate_join({"mode": "QUORUM"}, _children("VERIFIED"))
    assert exc.value.code == "TASK_JOIN_POLICY_INVALID"
    with pytest.raises(GraphError):
        evaluate_join({"mode": "SOME"}, _children("VERIFIED"))
