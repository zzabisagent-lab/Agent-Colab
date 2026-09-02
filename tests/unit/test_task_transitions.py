"""V-P1-27: table-driven Task transition table; V-P1-09: terminal states immutable; V-P1-14 hook."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from server.domain.task import (
    TERMINAL,
    TRANSITIONS,
    TaskState,
    TaskStatus,
    TaskTransitionError,
    apply_event,
    completion_prerequisites,
    fold,
    next_status,
    register_completion_check,
)


@pytest.fixture(autouse=True)
def _without_document_gate() -> Iterator[None]:
    """These tests exercise the Task transition machine; the FINALIZED-document completion gate
    (P1-10) is covered by tests/integration/test_document_lifecycle.py and tests/e2e."""
    from server.domain.task import COMPLETION_CHECKS

    removed = [
        c for c in COMPLETION_CHECKS if getattr(c, "__name__", "") == "finalized_document_check"
    ]
    for check in removed:
        COMPLETION_CHECKS.remove(check)
    yield
    COMPLETION_CHECKS.extend(removed)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tasks" / "transitions.yaml"
TABLE = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
ALLOWED = {(c["from"], c["event"]): c["to"] for c in TABLE["allowed"]}
ALL_PAIRS = [(s.value, e) for s in TaskStatus for e in TABLE["events"]]


def test_fixture_table_equals_the_code_table() -> None:
    assert {(k[0].value, k[1]): v.value for k, v in TRANSITIONS.items()} == ALLOWED


@pytest.mark.parametrize(("status", "event"), ALL_PAIRS, ids=[f"{s}-{e}" for s, e in ALL_PAIRS])
def test_every_status_event_pair(status: str, event: str) -> None:
    current = TaskStatus(status)
    if (status, event) in ALLOWED:
        assert next_status(current, event) == TaskStatus(ALLOWED[(status, event)])
        return
    with pytest.raises(TaskTransitionError) as exc:
        next_status(current, event)
    expected = "TASK_TERMINAL" if current in TERMINAL else "TASK_TRANSITION_INVALID"
    assert exc.value.code == expected


def _events(task_id: str, types: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {
            "event_id": "evt-0",
            "type": "TASK_CREATED",
            "aggregate_type": "task",
            "aggregate_id": task_id,
            "aggregate_seq": 1,
            "recorded_seq": 1,
            "workspace_id": "ws",
            "task_id": task_id,
            "actor_account_id": "acct-h",
            "occurred_at": "2026-01-01T00:00:00.000Z",
            "payload": {
                "task_id": task_id,
                "root_task_id": task_id,
                "channel_id": "c",
                "title": "t",
                "domain": "d",
                "risk": "LOW",
            },
        }
    ]
    seq = 1
    vid = 0
    for i, t in enumerate(types, start=2):
        ev: dict[str, Any] = {
            "event_id": f"evt-{i}",
            "type": t,
            "recorded_seq": i,
            "workspace_id": "ws",
            "task_id": task_id,
            "actor_account_id": "acct-a",
            "occurred_at": f"2026-01-01T00:00:{i:02d}.000Z",
        }
        if t.startswith("VERIFICATION_"):
            ev.update(
                {
                    "aggregate_type": "verification_run",
                    "aggregate_id": f"vr-{vid}",
                    "aggregate_seq": 2,
                    "payload": {
                        "verification_id": f"vr-{vid}",
                        "revision": 1,
                        "evidence_refs": [],
                        "finding_ids": [],
                        "reason_code": "ENV",
                    },
                }
            )
        else:
            seq += 1
            payload: dict[str, Any] = {"task_id": task_id}
            if t == "TASK_VERIFICATION_STARTED":
                vid += 1
                payload["verification_id"] = f"vr-{vid}"
            if t in ("TASK_DELEGATED", "TASK_REASSIGNED"):
                payload.update(
                    {
                        "assignee_account_id": "acct-a",
                        "assignment_revision": 1,
                        "policy_snapshot_hash": "0" * 64,
                        "reason_code": "x",
                    }
                )
            if t == "TASK_PROGRESS_REPORTED":
                payload["summary"] = "half"
            if t == "IMPLEMENTATION_SUBMITTED":
                payload.update({"evidence_refs": ["art-1"], "criteria_revision": 1})
            if t == "TASK_COMPLETED":
                payload.update({"verification_id": f"vr-{vid}", "document_id": "doc-1"})
            ev.update(
                {
                    "aggregate_type": "task",
                    "aggregate_id": task_id,
                    "aggregate_seq": seq,
                    "payload": payload,
                }
            )
        events.append(ev)
    return events


@pytest.mark.parametrize("flow", TABLE["flows"], ids=[f["name"] for f in TABLE["flows"]])
def test_flows_fold_to_expected_final_status(flow: dict[str, Any]) -> None:
    state = fold("task-1", _events("task-1", flow["events"]))
    assert state.status == TaskStatus(flow["final"])
    assert state.exists and state.last_aggregate_seq >= 1


def test_terminal_states_reject_every_event_without_changing_state() -> None:
    for terminal in TERMINAL:
        for event in TABLE["events"]:
            state = TaskState(task_id="t", exists=True, status=terminal)
            if event.startswith("VERIFICATION_"):
                # verification results never move a terminal Task; the fold ignores them
                apply_event(
                    state,
                    {
                        "type": event,
                        "aggregate_type": "verification_run",
                        "aggregate_id": "vr",
                        "aggregate_seq": 1,
                        "task_id": "t",
                        "payload": {"verification_id": "vr"},
                    },
                )
                assert state.status == terminal
                continue
            with pytest.raises(TaskTransitionError) as exc:
                apply_event(
                    state,
                    {
                        "type": event,
                        "aggregate_type": "task",
                        "aggregate_id": "t",
                        "aggregate_seq": 9,
                        "task_id": "t",
                        "payload": {"task_id": "t", "verification_id": "x"},
                    },
                )
            assert exc.value.code == "TASK_TERMINAL" and state.status == terminal


def test_stale_or_foreign_verification_results_are_ignored_in_the_fold() -> None:
    events = _events(
        "task-2",
        [
            "TASK_DELEGATED",
            "TASK_ACCEPTED",
            "TASK_STARTED",
            "IMPLEMENTATION_SUBMITTED",
            "TASK_VERIFICATION_STARTED",
        ],
    )
    stale = {
        "event_id": "evt-x",
        "type": "VERIFICATION_PASSED",
        "aggregate_type": "verification_run",
        "aggregate_id": "vr-9",
        "aggregate_seq": 1,
        "recorded_seq": 99,
        "workspace_id": "ws",
        "task_id": "task-2",
        "actor_account_id": "v",
        "occurred_at": "2026-01-01T00:01:00.000Z",
        "payload": {"verification_id": "vr-9", "revision": 1, "evidence_refs": []},
    }
    other_task = dict(
        stale,
        task_id="task-other",
        payload={"verification_id": "vr-1", "revision": 1, "evidence_refs": []},
    )
    state = fold("task-2", [*events, stale, other_task])
    assert state.status is TaskStatus.VERIFYING and state.verification_status == "PENDING"


def test_completion_prerequisites_and_hook() -> None:
    state = fold(
        "task-3",
        _events(
            "task-3",
            [
                "TASK_DELEGATED",
                "TASK_ACCEPTED",
                "TASK_STARTED",
                "IMPLEMENTATION_SUBMITTED",
                "TASK_VERIFICATION_STARTED",
            ],
        ),
    )
    assert completion_prerequisites(state) == ["VERIFICATION_REQUIRED"]
    passed = fold(
        "task-3",
        _events(
            "task-3",
            [
                "TASK_DELEGATED",
                "TASK_ACCEPTED",
                "TASK_STARTED",
                "IMPLEMENTATION_SUBMITTED",
                "TASK_VERIFICATION_STARTED",
                "VERIFICATION_PASSED",
            ],
        ),
    )
    assert completion_prerequisites(passed) == []

    def needs_document(s: TaskState, _session: Any) -> str | None:
        return "COMPLETION_PREREQUISITE_MISSING"

    register_completion_check(needs_document)
    try:
        assert completion_prerequisites(passed) == ["COMPLETION_PREREQUISITE_MISSING"]
    finally:
        from server.domain.task import COMPLETION_CHECKS

        COMPLETION_CHECKS.remove(needs_document)
