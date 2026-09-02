"""Work item contract: schemas, state machine, timing, exactly-once results, HMAC (V-P0-17)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from server.agents import webhook_signing as ws
from server.domain import defaults
from server.domain.clock import FixedClock
from server.work.schemas import AdapterSchemaError, validate
from server.work.state import (
    TERMINAL_STATES,
    TRANSITIONS,
    NextAction,
    ResultLedger,
    WorkItemError,
    WorkItemState,
    next_action,
    transition,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "work"
ITEMS_VALID = json.loads((FIXTURES / "work-item-valid.json").read_text(encoding="utf-8"))
ITEMS_INVALID = json.loads((FIXTURES / "work-item-invalid.json").read_text(encoding="utf-8"))
MIXED = json.loads((FIXTURES / "receipts-and-results.json").read_text(encoding="utf-8"))
T0 = dt.datetime(2026, 1, 15, 10, 0, tzinfo=dt.UTC)


# ---- schemas -------------------------------------------------------------------------------
@pytest.mark.parametrize("item", ITEMS_VALID, ids=[i["kind"] for i in ITEMS_VALID])
def test_valid_work_items(item: dict[str, Any]) -> None:
    validate("work_item", item)


@pytest.mark.parametrize("case", ITEMS_INVALID, ids=[c["name"] for c in ITEMS_INVALID])
def test_invalid_work_items(case: dict[str, Any]) -> None:
    with pytest.raises(AdapterSchemaError) as exc:
        validate("work_item", case["item"])
    assert exc.value.code == "WORK_ITEM_SCHEMA_INVALID"


@pytest.mark.parametrize(
    ("name", "key"),
    [("delivery_receipt", "receipts_valid"), ("work_result", "results_valid")],
)
def test_valid_receipts_and_results(name: str, key: str) -> None:
    for item in MIXED[key]:
        validate(name, item)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("delivery_receipt", "receipts_invalid"),
        ("work_result", "results_invalid"),
        ("probe_response", "probe_invalid"),
    ],
)
def test_invalid_receipts_results_probes(name: str, key: str) -> None:
    for case in MIXED[key]:
        with pytest.raises(AdapterSchemaError, match=f"{name.upper()}_SCHEMA_INVALID"):
            validate(name, case["item"])


def test_probe_and_heartbeat_valid() -> None:
    validate("probe_response", MIXED["probe_valid"])
    validate("heartbeat", MIXED["heartbeat_valid"])
    with pytest.raises(AdapterSchemaError):
        validate("heartbeat", {**MIXED["heartbeat_valid"], "usage_since_last": {}})


def test_display_identity_in_result_is_detectable_not_fatal() -> None:
    item = {**MIXED["results_valid"][0], "display_identity": {"username": "root"}}
    validate("work_result", item)  # server ignores + audits it (§7A.4); schema keeps it visible
    assert "display_identity" in item


# ---- state machine -------------------------------------------------------------------------
HAPPY_PATH = ["deliver", "ack", "start", "result"]


def test_happy_path_and_terminal_immutability() -> None:
    state = WorkItemState.QUEUED
    for action in HAPPY_PATH:
        state = transition(state, action)
    assert state is WorkItemState.RESULT_RECEIVED
    for action in ("deliver", "ack", "result", "cancel", "expire"):
        with pytest.raises(WorkItemError, match="WORK_ITEM_TRANSITION_INVALID"):
            transition(state, action)


@pytest.mark.parametrize(
    ("state", "action"),
    [
        (WorkItemState.QUEUED, "ack"),
        (WorkItemState.QUEUED, "result"),
        (WorkItemState.QUEUED, "start"),
        (WorkItemState.DELIVERED, "start"),
        (WorkItemState.DELIVERED, "result"),
        (WorkItemState.ACKED, "deliver"),
        (WorkItemState.IN_PROGRESS, "ack"),
        (WorkItemState.IN_PROGRESS, "deliver"),
        (WorkItemState.ACKED, "redeliver"),
    ],
)
def test_invalid_transitions(state: WorkItemState, action: str) -> None:
    with pytest.raises(WorkItemError) as exc:
        transition(state, action)
    assert exc.value.code == "WORK_ITEM_TRANSITION_INVALID"


def test_transition_table_is_closed_over_the_enum() -> None:
    for (src, _), dst in TRANSITIONS.items():
        assert src not in TERMINAL_STATES
        assert isinstance(dst, WorkItemState)
    reachable = {dst for dst in TRANSITIONS.values()} | {WorkItemState.QUEUED}
    assert reachable == set(WorkItemState)


# ---- timing model --------------------------------------------------------------------------
def test_ack_timeout_gives_exactly_three_redeliveries_then_expired() -> None:
    delivered_at = T0
    actions: list[NextAction] = []
    delivery_count = 1
    now = T0
    for _ in range(6):
        now = delivered_at + dt.timedelta(seconds=defaults.WORK_ITEM_ACK_TIMEOUT_S)
        decision = next_action(WorkItemState.DELIVERED, delivered_at, None, now, delivery_count)
        actions.append(decision.action)
        if decision.action is NextAction.REDELIVER:
            delivery_count += 1
            delivered_at = now
        else:
            break
    assert actions == [NextAction.REDELIVER] * 3 + [NextAction.EXPIRE]
    assert delivery_count == 4  # first delivery + 3 redeliveries


def test_ack_before_timeout_does_nothing() -> None:
    d = next_action(WorkItemState.DELIVERED, T0, None, T0 + dt.timedelta(seconds=59), 1)
    assert d.action is NextAction.NONE and d.due_at == T0 + dt.timedelta(seconds=60)


def test_accept_timeout_reroutes_once_then_waiting() -> None:
    acked = T0
    late = T0 + dt.timedelta(seconds=defaults.TASK_ASSIGNMENT_ACCEPT_TIMEOUT_S)
    first = next_action(WorkItemState.ACKED, T0, acked, late, 1, kind="task_assignment")
    assert first.action is NextAction.REROUTE
    second = next_action(
        WorkItemState.ACKED, T0, acked, late, 1, kind="task_assignment", reroute_count=1
    )
    assert second.action is NextAction.WAITING
    early = next_action(
        WorkItemState.ACKED, T0, acked, late - dt.timedelta(seconds=1), 1, kind="task_assignment"
    )
    assert early.action is NextAction.NONE
    accepted = next_action(
        WorkItemState.ACKED, T0, acked, late, 1, kind="task_assignment", accepted_at=acked
    )
    assert accepted.action is NextAction.NONE
    invoke = next_action(WorkItemState.ACKED, T0, acked, late, 1, kind="invoke")
    assert invoke.action is NextAction.NONE


def test_deadline_and_terminal_states() -> None:
    d = next_action(
        WorkItemState.ACKED,
        T0,
        T0,
        T0 + dt.timedelta(hours=2),
        1,
        deadline=T0 + dt.timedelta(hours=1),
    )
    assert d.action is NextAction.EXPIRE and d.reason == "DEADLINE_EXCEEDED"
    for st in TERMINAL_STATES:
        assert next_action(st, T0, T0, T0 + dt.timedelta(days=1), 9).action is NextAction.NONE
    with pytest.raises(WorkItemError, match="WORK_ITEM_TIMING_INVALID"):
        next_action(WorkItemState.DELIVERED, None, None, T0, 1)


# ---- exactly-once results ------------------------------------------------------------------
def test_results_accepted_exactly_once_and_duplicates_audited() -> None:
    ledger = ResultLedger()
    first = ledger.accept("wi-0123456789abcdef", "res-1", reporter="agent-0001")
    dup = ledger.accept("wi-0123456789abcdef", "res-2", reporter="agent-0001")
    other = ledger.accept("wi-00000000000000aa", "res-3", reporter="agent-0002")
    assert (first.accepted, first.code) == (True, "RESULT_ACCEPTED")
    assert (dup.accepted, dup.code, dup.first_result_ref) == (
        False,
        "DUPLICATE_RESULT_IGNORED",
        "res-1",
    )
    assert other.accepted
    assert ledger.result_of("wi-0123456789abcdef") == "res-1"
    assert len(ledger.audit) == 1 and ledger.audit[0]["ignored_result_ref"] == "res-2"


# ---- HMAC webhook --------------------------------------------------------------------------
KEY = b"test-signing-key-not-a-real-secret"
BODY = json.dumps(ITEMS_VALID[0]).encode()


def test_webhook_sign_verify_roundtrip_and_envelope_schema() -> None:
    clock = FixedClock(T0)
    headers = ws.sign(KEY, BODY, clock, key_ref="sec-webhook-agent-0001@v1", nonce="a" * 32)
    ws.verify(KEY, headers, BODY, clock, ws.InMemoryNonceStore())
    envelope = {
        "headers": {
            **headers,
            "X-Colab-Correlation-Id": "corr-task-0001",
            "Content-Type": "application/json",
        },
        "body": ITEMS_VALID[0],
        "expected_response": {"status": 202, "body": MIXED["receipts_valid"][0]},
        "timestamp_window_seconds": 300,
        "nonce_retention_hours": 24,
    }
    validate("webhook_envelope", envelope)
    assert (
        headers[ws.HEADER_SIGNATURE]
        != ws.sign(b"other", BODY, clock, key_ref="k")[ws.HEADER_SIGNATURE]
    )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda h, b: (dict(h, **{ws.HEADER_SIGNATURE: "0" * 64}), b), "WEBHOOK_SIGNATURE_INVALID"),
        (lambda h, b: (h, b + b" "), "WEBHOOK_SIGNATURE_INVALID"),
        (lambda h, b: (dict(h, **{ws.HEADER_NONCE: "b" * 32}), b), "WEBHOOK_SIGNATURE_INVALID"),
        (
            lambda h, b: ({k: v for k, v in h.items() if k != ws.HEADER_NONCE}, b),
            "WEBHOOK_HEADER_MISSING",
        ),
    ],
)
def test_webhook_tampering_rejected(mutate: Any, code: str) -> None:
    clock = FixedClock(T0)
    headers = ws.sign(KEY, BODY, clock, key_ref="sec-k@v1", nonce="a" * 32)
    h, b = mutate(headers, BODY)
    with pytest.raises(ws.WebhookError) as exc:
        ws.verify(KEY, h, b, clock, ws.InMemoryNonceStore())
    assert exc.value.code == code


def test_webhook_timestamp_window_five_minutes() -> None:
    signer = FixedClock(T0)
    headers = ws.sign(KEY, BODY, signer, key_ref="sec-k@v1", nonce="a" * 32)
    ws.verify(
        KEY, headers, BODY, FixedClock(T0 + dt.timedelta(seconds=300)), ws.InMemoryNonceStore()
    )
    with pytest.raises(ws.WebhookError, match="WEBHOOK_TIMESTAMP_EXPIRED"):
        ws.verify(
            KEY, headers, BODY, FixedClock(T0 + dt.timedelta(seconds=301)), ws.InMemoryNonceStore()
        )
    with pytest.raises(ws.WebhookError, match="WEBHOOK_TIMESTAMP_EXPIRED"):
        ws.verify(
            KEY, headers, BODY, FixedClock(T0 - dt.timedelta(seconds=301)), ws.InMemoryNonceStore()
        )


def test_webhook_nonce_reuse_rejected_within_24h_then_forgotten() -> None:
    clock = FixedClock(T0)
    store = ws.InMemoryNonceStore()
    headers = ws.sign(KEY, BODY, clock, key_ref="sec-k@v1", nonce="a" * 32)
    ws.verify(KEY, headers, BODY, clock, store)
    with pytest.raises(ws.WebhookError, match="WEBHOOK_NONCE_REUSED"):
        ws.verify(KEY, headers, BODY, clock, store)
    later = FixedClock(T0 + dt.timedelta(hours=24, seconds=1))
    assert store.remember("a" * 32, later.now()) is True  # retention window elapsed


def test_webhook_body_hash_claim_mismatch() -> None:
    clock = FixedClock(T0)
    headers = ws.sign(KEY, BODY, clock, key_ref="sec-k@v1", nonce="a" * 32)
    with pytest.raises(ws.WebhookError, match="WEBHOOK_BODY_HASH_MISMATCH"):
        ws.verify(KEY, headers, BODY, clock, ws.InMemoryNonceStore(), body_sha256_claim="f" * 64)
