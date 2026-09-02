"""P1-12 unit tests: timeout decisions (pure), delivery envelope, result reference."""

from __future__ import annotations

import datetime as dt

import pytest

from server.domain import defaults
from server.work.inbox import WorkItem, result_ref_for
from server.work.state import NextAction, WorkItemState, next_action

T0 = dt.datetime(2026, 4, 1, 12, 0, tzinfo=dt.UTC)


def _item(**over: object) -> WorkItem:
    base: dict[str, object] = {
        "work_item_id": "wi-" + "a" * 24,
        "workspace_id": "ws",
        "kind": "invoke",
        "agent_id": "agent-1",
        "task_id": "task-1",
        "brainstorm_id": None,
        "correlation_id": "corr",
        "deadline": T0 + dt.timedelta(hours=1),
        "payload": {"x": 1},
        "secret_handles": [],
        "expected_result_schema": "colab.work-result.v1",
        "idempotency_key": "k",
        "status": WorkItemState.QUEUED,
        "delivery_count": 0,
        "delivered_at": None,
        "acked_at": None,
        "accepted_at": None,
        "finished_at": None,
        "created_at": T0,
        "updated_at": T0,
    }
    base.update(over)
    return WorkItem(**base)  # type: ignore[arg-type]


def test_delivery_envelope_matches_7b1() -> None:
    env = _item(status=WorkItemState.DELIVERED, delivery_count=2).to_delivery()
    assert env["payload_ref"] == "colab://work/wi-" + "a" * 24 + "/payload"
    assert env["schema_id"] == "colab.work-item.v1" and env["delivery_no"] == 2
    assert env["deadline"].endswith("Z") and "payload" not in env


@pytest.mark.parametrize(
    ("count", "elapsed", "expected", "reason"),
    [
        (1, 59, NextAction.NONE, "awaiting ack"),
        (1, 60, NextAction.REDELIVER, "ACK_TIMEOUT redelivery 1 of 3"),
        (2, 60, NextAction.REDELIVER, "ACK_TIMEOUT redelivery 2 of 3"),
        (3, 60, NextAction.REDELIVER, "ACK_TIMEOUT redelivery 3 of 3"),
        (4, 60, NextAction.EXPIRE, "ACK_TIMEOUT_EXHAUSTED"),
    ],
)
def test_exactly_three_redeliveries_then_expire(
    count: int, elapsed: int, expected: NextAction, reason: str
) -> None:
    d = next_action(WorkItemState.DELIVERED, T0, None, T0 + dt.timedelta(seconds=elapsed), count)
    assert (d.action, d.reason) == (expected, reason)
    assert defaults.WORK_ITEM_MAX_REDELIVERIES == 3 and defaults.WORK_ITEM_ACK_TIMEOUT_S == 60


def test_deadline_beats_every_other_timer() -> None:
    d = next_action(
        WorkItemState.ACKED,
        T0,
        T0,
        T0 + dt.timedelta(hours=2),
        1,
        deadline=T0 + dt.timedelta(hours=1),
    )
    assert d.action is NextAction.EXPIRE and d.reason == "DEADLINE_EXCEEDED"


def test_assignment_accept_timeout_reroutes_once_then_waits() -> None:
    late = T0 + dt.timedelta(seconds=defaults.TASK_ASSIGNMENT_ACCEPT_TIMEOUT_S)
    first = next_action(WorkItemState.ACKED, T0, T0, late, 1, kind="task_assignment")
    second = next_action(
        WorkItemState.ACKED, T0, T0, late, 1, kind="task_assignment", reroute_count=1
    )
    assert first.action is NextAction.REROUTE and second.action is NextAction.WAITING
    accepted = next_action(
        WorkItemState.IN_PROGRESS, T0, T0, late, 1, kind="task_assignment", accepted_at=T0
    )
    assert accepted.action is NextAction.NONE


def test_result_ref_is_content_addressed_and_order_independent() -> None:
    a, da = result_ref_for("wi-1", {"b": 1, "a": [1, 2]})
    b, db = result_ref_for("wi-1", {"a": [1, 2], "b": 1})
    c, _ = result_ref_for("wi-1", {"a": [2, 1], "b": 1})
    assert a == b and da == db and a != c and a.startswith("colab://work/wi-1/result/")
