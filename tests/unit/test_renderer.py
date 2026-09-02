"""P2-03/P2-11 renderer rules (§7A.3): card fields, buttons by permission, one reply per
transition, 10-second progress coalescing, >16k bodies linked as Artifacts."""

from __future__ import annotations

import datetime as dt

from server.channels.renderer import (
    TRANSITION_MESSAGES,
    BodyDecision,
    CardInput,
    ProgressCoalescer,
    body_for_post,
    render_task_card,
    render_transition,
)

T0 = dt.datetime(2026, 4, 1, tzinfo=dt.UTC)


def test_card_contains_every_required_field_and_permission_filtered_buttons() -> None:
    card = CardInput(
        task_id="task-1",
        title="Write report",
        status="RUNNING",
        risk="LOW",
        domain="research",
        assignee="@agent",
        verification_status="PENDING",
        pending_approvals=("apr-1",),
        latest_progress="half",
        links=("art-1", "doc-1"),
        subtasks=(("task-2", "RUNNING"),),
        join_policy="ALL",
        actor_permissions=frozenset({"task.submit", "approval.decide"}),
    )
    rendered = render_task_card(card)
    for needle in (
        "Write report",
        "RUNNING",
        "@agent",
        "PENDING",
        "apr-1",
        "half",
        "art-1",
        "task-2",
        "ALL",
    ):
        assert needle in rendered.text
    assert rendered.buttons == ("approve", "reject", "submit")  # no task.cancel permission
    assert rendered.props["agent_colab"]["subject_id"] == "task-1"
    none = render_task_card(CardInput("task-1", "t", "RUNNING", "LOW", "d"))
    assert none.buttons == ()


def test_every_task_transition_has_exactly_one_reply_and_unknown_types_none() -> None:
    for etype in (
        "TASK_CREATED",
        "TASK_DELEGATED",
        "TASK_ACCEPTED",
        "TASK_STARTED",
        "TASK_WAITING",
        "IMPLEMENTATION_SUBMITTED",
        "TASK_VERIFICATION_STARTED",
        "VERIFICATION_PASSED",
        "VERIFICATION_FAILED",
        "VERIFICATION_BLOCKED",
        "TASK_COMPLETED",
        "TASK_CANCEL_REQUESTED",
        "TASK_CANCELLED",
        "TASK_REASSIGNED",
        "SUBTASK_CREATED",
    ):
        assert etype in TRANSITION_MESSAGES
        assert (
            render_transition(
                etype,
                {
                    "title": "t",
                    "assignee": "a",
                    "reason_code": "r",
                    "summary": "s",
                    "criteria_revision": 1,
                    "verification_id": "v",
                    "revision": 1,
                    "document_id": "d",
                },
            )
            is not None
        )
    assert render_transition("AGENT_HEARTBEAT_RECORDED", {}) is None


def test_progress_coalesced_in_ten_second_windows() -> None:
    c = ProgressCoalescer()
    assert c.add("task-1", T0, "a") is None  # opens the window
    assert c.add("task-1", T0 + dt.timedelta(seconds=3), "b") is None
    assert c.add("task-1", T0 + dt.timedelta(seconds=9), "c") is None
    flushed = c.add("task-1", T0 + dt.timedelta(seconds=10), "d")  # window elapsed: flush a,b,c
    assert flushed is not None and "3 updates" in flushed and "a | b | c" in flushed
    assert c.add("task-2", T0 + dt.timedelta(seconds=10), "x") is None  # per-Task windows
    due = c.due(T0 + dt.timedelta(seconds=25))
    assert sorted(t for t, _ in due) == ["task-1", "task-2"]
    assert c.due(T0 + dt.timedelta(seconds=30)) == []


def test_long_bodies_are_linked_as_artifacts() -> None:
    short = body_for_post("x" * 16_000)
    assert short == BodyDecision("x" * 16_000, None)
    long = body_for_post("y" * 16_001, "art-9")
    assert long.artifact_body is not None and len(long.artifact_body) == 16_001
    assert "art-9" in long.post_text and len(long.post_text) < 1_000
