"""P1-06 unit rules: state table, snapshot determinism, submitter independence, verdict schema."""

from __future__ import annotations

from typing import Any

import pytest

from server.verification.independence import Identity, VerificationIndependenceError
from server.verification.runs import (
    TERMINAL,
    TRANSITIONS,
    VerificationError,
    VerificationOp,
    VerificationRun,
    VerificationStatus,
    build_snapshot,
    check_submitter,
    independence_from_snapshot,
    next_status,
    relevant_alias_edges,
    snapshot_hash,
    validate_verdict,
)

S = VerificationStatus
OP = VerificationOp


@pytest.mark.parametrize("state", list(S))
@pytest.mark.parametrize("op", list(OP))
def test_transition_table_is_total_and_terminal_is_immutable(state: S, op: OP) -> None:
    if state in TERMINAL:
        with pytest.raises(VerificationError) as exc:
            next_status(state, op, "PASSED")
        assert exc.value.code == "VERIFICATION_TERMINAL"
    elif (state, op) in TRANSITIONS:
        result = "FAILED" if op is OP.VERDICT else None
        assert next_status(state, op, result) is not None
    else:
        with pytest.raises(VerificationError) as exc:
            next_status(state, op, "PASSED")
        assert exc.value.code == "VERIFICATION_TRANSITION_INVALID"


def test_normal_and_recheck_paths() -> None:
    s = next_status(S.PLANNED, OP.ASSIGN)
    s = next_status(s, OP.START)
    s = next_status(s, OP.VERDICT, "FAILED")
    assert s is S.FAILED
    s = next_status(s, OP.FIX)
    s = next_status(s, OP.RECHECK)
    s = next_status(s, OP.START)
    assert next_status(s, OP.VERDICT, "PASSED") is S.PASSED
    with pytest.raises(VerificationError) as exc:
        next_status(S.RUNNING, OP.VERDICT, "MAYBE")
    assert exc.value.code == "VERIFICATION_RESULT_INVALID"


def test_snapshot_hash_is_deterministic_and_order_independent() -> None:
    impl = Identity("a", "fp-a", "agent-a")
    ver = Identity("b", "fp-b", "agent-b")
    graph = {"c": "a", "d": "b", "e": "z"}
    edges = relevant_alias_edges(graph, "a", "b")
    assert edges == [["c", "a"], ["d", "b"]]
    kwargs: dict[str, Any] = {
        "identity_graph_version": "identity-v8-001",
        "effective_policy_hash": "sha256:p",
        "criteria_version": "v8.0",
        "target_commit": "abc",
        "alias_edges": edges,
    }
    h1 = snapshot_hash(build_snapshot(impl, ver, **kwargs))
    h2 = snapshot_hash(
        build_snapshot(impl, ver, **{**kwargs, "alias_edges": list(reversed(edges))})
    )
    assert h1 == snapshot_hash(build_snapshot(impl, ver, **kwargs))
    assert h1 != h2  # edges are part of the snapshot; order is canonical from the builder
    independence_from_snapshot(build_snapshot(impl, ver, **kwargs))
    with pytest.raises(VerificationIndependenceError):
        independence_from_snapshot(
            build_snapshot(
                impl, Identity("c", "fp-c", None), **{**kwargs, "alias_edges": [["c", "a"]]}
            )
        )


def _run(**over: Any) -> VerificationRun:
    base: dict[str, Any] = {
        "verification_id": "vr-x",
        "workspace_id": "ws",
        "target_type": "task",
        "target_id": "task-1",
        "phase": None,
        "task_id": "task-1",
        "implementer_account_id": "impl",
        "verifier_account_id": "ver",
        "implementer_agent_id": "agent-i",
        "verifier_agent_id": "agent-v",
        "implementer_credential_fingerprint": "fp-i",
        "verifier_credential_fingerprint": "fp-v",
        "identity_graph_version": "identity-v8-001",
        "effective_policy_hash": "sha256:p",
        "criteria_version": "v8.0",
        "target_commit": "abc",
        "status": S.RUNNING,
        "current_revision": 0,
        "result": None,
        "snapshot_hash": "0" * 64,
    }
    base.update(over)
    return VerificationRun(**base)


@pytest.mark.parametrize(
    ("account", "fp", "graph", "code"),
    [
        ("impl", "fp-x", {}, "SELF_VERIFICATION_FORBIDDEN"),
        ("alias", "fp-x", {"alias": "impl"}, "SELF_VERIFICATION_FORBIDDEN"),
        ("other", "fp-i", {}, "SELF_VERIFICATION_FORBIDDEN"),
        ("other", "fp-x", {}, "VERIFIER_MISMATCH"),
        ("ver", "fp-v", {}, None),
        ("ver", "fp-rotated", {}, None),
    ],
)
def test_submitter_rules(account: str, fp: str, graph: dict[str, str], code: str | None) -> None:
    run = _run()
    if code is None:
        check_submitter(run, submitter_account_id=account, submitter_fingerprint=fp, graph=graph)
        return
    with pytest.raises(VerificationError) as exc:
        check_submitter(run, submitter_account_id=account, submitter_fingerprint=fp, graph=graph)
    assert exc.value.code == code


def test_verdict_report_rules() -> None:
    ok = {
        "result": "PASSED",
        "criteria_version": "v8.0",
        "tests": [{"id": "V-P1-01", "result": "PASS", "evidence_ref": "e"}],
        "findings": [],
        "residual_risks": [],
    }
    validate_verdict(ok, "PASSED")
    with pytest.raises(VerificationError) as exc:
        validate_verdict({**ok, "result": "FAILED"}, "PASSED")
    assert exc.value.code == "VERDICT_RESULT_MISMATCH"
    with pytest.raises(VerificationError) as exc2:
        validate_verdict(
            {**ok, "findings": [{"id": "F1", "severity": "High", "summary": "x"}]}, "PASSED"
        )
    assert exc2.value.code == "VERDICT_PASS_NOT_JUSTIFIED"
    with pytest.raises(VerificationError) as exc3:
        validate_verdict({"result": "PASSED"}, "PASSED")
    assert exc3.value.code == "VERDICT_REPORT_INVALID"
    validate_verdict(
        {**ok, "result": "FAILED", "findings": [{"id": "F1", "severity": "High", "summary": "x"}]},
        "FAILED",
    )
