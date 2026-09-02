"""Generate deterministic Event fixtures (V-P0-05, V-P0-13).

Valid fixtures are complete aggregate streams (task, approval, schedule, schedule_run, agent) with
correct hash chains; invalid fixtures carry the stable error code the contract must return.
``python -m tools.gen_event_fixtures`` writes ``tests/fixtures/events/{valid,invalid}``; ``--check``
fails on drift.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from server.events.hashing import compute_content_hash
from tools.baseline import ROOT

FIXTURES = ROOT / "tests" / "fixtures" / "events"
WORKSPACE = "0f1e2d3c-4b5a-4a6b-8c7d-9e8f7a6b5c4d"
ACTOR_HUMAN = "11111111-1111-4111-8111-111111111111"
ACTOR_AGENT = "22222222-2222-4222-8222-222222222222"
ACTOR_SERVICE = "33333333-3333-4333-8333-333333333333"
CHANNEL = "44444444-4444-4444-8444-444444444444"


def _event_id(seed: str) -> str:
    return "evt-" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def _ts(step: int) -> str:
    return f"2026-01-15T10:{step // 60:02d}:{step % 60:02d}.000Z"


def stream(
    aggregate_type: str,
    aggregate_id: str,
    steps: list[tuple[str, str, dict[str, Any], str]],
    task_id: str | None = None,
    channel_id: str | None = CHANNEL,
) -> list[dict[str, Any]]:
    """Build a hash-chained stream: steps are (type, actor, payload, operation)."""
    events: list[dict[str, Any]] = []
    previous: str | None = None
    for seq, (etype, actor, payload, operation) in enumerate(steps, start=1):
        event: dict[str, Any] = {
            "event_id": _event_id(f"{aggregate_id}:{seq}"),
            "schema_version": 1,
            "workspace_id": WORKSPACE,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "aggregate_seq": seq,
            "channel_id": channel_id,
            "task_id": task_id,
            "type": etype,
            "actor_account_id": actor,
            "caused_by": events[-1]["event_id"] if events else None,
            "correlation_id": f"corr-{aggregate_id}",
            "idempotency_scope": f"{aggregate_type}:{operation}",
            "idempotency_key": f"{aggregate_id}-{operation}-{seq}",
            "policy_version": "policy-v1",
            "payload": payload,
            "sensitive_payload_ciphertext": None,
            "sensitive_payload_key_ref": None,
            "previous_hash": previous,
            "occurred_at": _ts(seq),
            "recorded_at": _ts(seq),
        }
        event["content_hash"] = compute_content_hash(event)
        previous = event["content_hash"]
        events.append(event)
    return events


def build_valid() -> dict[str, list[dict[str, Any]]]:
    task = "task-0001"
    fixtures: dict[str, list[dict[str, Any]]] = {}
    fixtures["task-stream"] = stream(
        "task",
        task,
        [
            (
                "TASK_CREATED",
                ACTOR_HUMAN,
                {
                    "task_id": task,
                    "root_task_id": task,
                    "channel_id": CHANNEL,
                    "title": "Write the report",
                    "domain": "research",
                    "risk": "LOW",
                },
                "create",
            ),
            (
                "TASK_DELEGATED",
                ACTOR_HUMAN,
                {
                    "task_id": task,
                    "assignee_account_id": ACTOR_AGENT,
                    "assignment_revision": 1,
                    "policy_snapshot_hash": "a" * 64,
                },
                "delegate",
            ),
            (
                "TASK_ACCEPTED",
                ACTOR_AGENT,
                {"task_id": task, "assignee_account_id": ACTOR_AGENT},
                "accept",
            ),
            ("TASK_STARTED", ACTOR_AGENT, {"task_id": task}, "start"),
            (
                "TASK_PROGRESS_REPORTED",
                ACTOR_AGENT,
                {"task_id": task, "summary": "50% done"},
                "progress",
            ),
            (
                "IMPLEMENTATION_SUBMITTED",
                ACTOR_AGENT,
                {"task_id": task, "evidence_refs": ["art-0001"], "criteria_revision": 1},
                "submit",
            ),
            (
                "TASK_VERIFICATION_STARTED",
                ACTOR_SERVICE,
                {"task_id": task, "verification_id": "vr-0001"},
                "verify",
            ),
            (
                "TASK_COMPLETED",
                ACTOR_SERVICE,
                {"task_id": task, "verification_id": "vr-0001", "document_id": "doc-0001"},
                "complete",
            ),
        ],
        task_id=task,
    )
    apr = "apr-0001"
    fixtures["approval-stream"] = stream(
        "approval",
        apr,
        [
            (
                "APPROVAL_REQUESTED",
                ACTOR_AGENT,
                {
                    "approval_id": apr,
                    "subject_type": "task",
                    "subject_id": task,
                    "action": "external_send",
                    "risk": "HIGH",
                    "expires_at": "2026-01-16T10:00:00.000Z",
                },
                "request",
            ),
            (
                "APPROVAL_GRANTED",
                ACTOR_HUMAN,
                {"approval_id": apr, "decided_by": ACTOR_HUMAN, "quorum_count": 1},
                "decide",
            ),
            (
                "APPROVAL_CONSUMED",
                ACTOR_SERVICE,
                {"approval_id": apr, "consumption_key": f"{task}:external_send:1", "used_count": 1},
                "consume",
            ),
        ],
        task_id=task,
    )
    sch = "sch-0001"
    fixtures["schedule-stream"] = stream(
        "schedule",
        sch,
        [
            (
                "SCHEDULE_CREATED",
                ACTOR_HUMAN,
                {
                    "schedule_id": sch,
                    "schedule_version_id": "schv-0001",
                    "version": 1,
                    "snapshot_hash": "b" * 64,
                },
                "create",
            ),
            ("SCHEDULE_ENABLED", ACTOR_HUMAN, {"schedule_id": sch}, "enable"),
            (
                "SCHEDULE_UPDATED",
                ACTOR_HUMAN,
                {
                    "schedule_id": sch,
                    "schedule_version_id": "schv-0002",
                    "version": 2,
                    "snapshot_hash": "c" * 64,
                },
                "update",
            ),
            ("SCHEDULE_PAUSED", ACTOR_HUMAN, {"schedule_id": sch}, "pause"),
        ],
        task_id=None,
    )
    run = "run-0001"
    fixtures["schedule-run-stream"] = stream(
        "schedule_run",
        run,
        [
            (
                "RUN_DUE",
                ACTOR_SERVICE,
                {
                    "run_id": run,
                    "schedule_id": sch,
                    "schedule_version_id": "schv-0001",
                    "run_kind": "SCHEDULED",
                    "scheduled_for": "2026-01-15T10:05:00.000Z",
                },
                "materialize",
            ),
            (
                "RUN_CLAIMED",
                ACTOR_SERVICE,
                {
                    "run_id": run,
                    "claimed_by": "runner-a",
                    "lease_expires_at": "2026-01-15T10:06:00.000Z",
                },
                "claim",
            ),
            (
                "RUN_STARTED",
                ACTOR_SERVICE,
                {"run_id": run, "attempt_no": 1, "task_id": "task-0002"},
                "start",
            ),
            ("RUN_SUCCEEDED", ACTOR_SERVICE, {"run_id": run, "attempt_no": 1}, "finish"),
        ],
        task_id="task-0002",
    )
    agent = "agent-0001"
    fixtures["agent-stream"] = stream(
        "agent",
        agent,
        [
            (
                "AGENT_REGISTERED",
                ACTOR_HUMAN,
                {
                    "agent_id": agent,
                    "account_id": ACTOR_AGENT,
                    "adapter_type": "mcp",
                    "display_name": "Research Agent",
                },
                "register",
            ),
            ("AGENT_ACTIVATED", ACTOR_HUMAN, {"agent_id": agent}, "activate"),
            (
                "AGENT_HEARTBEAT_RECORDED",
                ACTOR_AGENT,
                {"agent_id": agent, "capacity": 3},
                "heartbeat",
            ),
            (
                "AGENT_MARKED_OFFLINE",
                ACTOR_SERVICE,
                {"agent_id": agent, "missed_heartbeats": 3},
                "offline",
            ),
            (
                "AGENT_SUSPENDED",
                ACTOR_HUMAN,
                {"agent_id": agent, "reason_code": "POLICY"},
                "suspend",
            ),
        ],
        task_id=None,
        channel_id=None,
    )
    # sensitive envelope example: ciphertext + key ref, payload non-sensitive only
    sec = stream(
        "secret_grant",
        "grant-0001",
        [
            (
                "SECRET_GRANT_CREATED",
                ACTOR_HUMAN,
                {
                    "grant_id": "grant-0001",
                    "secret_id": "sec-0001",
                    "grantee_agent_id": agent,
                    "task_id": task,
                    "action_scope": "http.get",
                    "expires_at": "2026-01-15T10:30:00.000Z",
                },
                "grant",
            )
        ],
        task_id=task,
    )
    sec[0]["sensitive_payload_ciphertext"] = base64.b64encode(b"\x01\x02ciphertext-bytes").decode()
    sec[0]["sensitive_payload_key_ref"] = "dek://workspace/0f1e/grant-0001"
    sec[0]["content_hash"] = compute_content_hash(sec[0])
    fixtures["secret-grant-with-ciphertext"] = sec
    return fixtures


def build_invalid(valid: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    base = copy.deepcopy(valid["task-stream"][0])
    second = copy.deepcopy(valid["task-stream"][1])
    cases: dict[str, dict[str, Any]] = {}

    def case(name: str, code: str, mutate: Any) -> None:
        ev = copy.deepcopy(base)
        mutate(ev)
        cases[name] = {"expected_code": code, "event": ev}

    case("missing-required-field", "SCHEMA_INVALID", lambda e: e.pop("correlation_id"))
    case("bad-event-id", "SCHEMA_INVALID", lambda e: e.__setitem__("event_id", "not-an-id"))
    case("seq-zero", "SCHEMA_INVALID", lambda e: e.__setitem__("aggregate_seq", 0))
    case("unknown-field", "SCHEMA_INVALID", lambda e: e.__setitem__("extra", 1))
    case(
        "ciphertext-without-key-ref",
        "SCHEMA_INVALID",
        lambda e: e.__setitem__("sensitive_payload_ciphertext", "AQI="),
    )
    case(
        "key-ref-without-ciphertext",
        "SCHEMA_INVALID",
        lambda e: e.__setitem__("sensitive_payload_key_ref", "dek://x"),
    )
    case(
        "previous-hash-on-first",
        "SCHEMA_INVALID",
        lambda e: e.__setitem__("previous_hash", "0" * 64),
    )
    case(
        "timestamp-not-utc-ms",
        "SCHEMA_INVALID",
        lambda e: e.__setitem__("occurred_at", "2026-01-15T10:00:01+09:00"),
    )
    case("unknown-type", "UNKNOWN_EVENT_TYPE", lambda e: e.__setitem__("type", "TASK_EXPLODED"))
    case(
        "aggregate-type-mismatch",
        "AGGREGATE_TYPE_MISMATCH",
        lambda e: e.__setitem__("aggregate_type", "approval"),
    )
    case(
        "aggregate-id-prefix",
        "AGGREGATE_ID_INVALID",
        lambda e: e.__setitem__("aggregate_id", "apr-0001"),
    )
    case(
        "idempotency-scope-other-aggregate",
        "IDEMPOTENCY_SCOPE_INVALID",
        lambda e: e.__setitem__("idempotency_scope", "approval:create"),
    )
    case("payload-missing-required", "PAYLOAD_INVALID", lambda e: e["payload"].pop("title"))
    case("payload-wrong-type", "PAYLOAD_INVALID", lambda e: e["payload"].__setitem__("title", 42))
    case(
        "hash-tampered-payload",
        "HASH_MISMATCH",
        lambda e: e["payload"].__setitem__("title", "Tampered"),
    )
    case(
        "hash-tampered-metadata",
        "HASH_MISMATCH",
        lambda e: e.__setitem__("actor_account_id", ACTOR_AGENT),
    )
    case("hash-wrong-value", "HASH_MISMATCH", lambda e: e.__setitem__("content_hash", "f" * 64))

    ev2 = copy.deepcopy(second)
    ev2["previous_hash"] = "e" * 64
    cases["chain-broken-previous-hash"] = {"expected_code": "HASH_MISMATCH", "event": ev2}
    ev3 = copy.deepcopy(second)
    ev3["previous_hash"] = None
    cases["chain-missing-previous-hash"] = {"expected_code": "SCHEMA_INVALID", "event": ev3}
    return cases


def render() -> dict[Path, str]:
    valid = build_valid()
    out: dict[Path, str] = {}
    for name, events in valid.items():
        out[FIXTURES / "valid" / f"{name}.json"] = (
            json.dumps(events, indent=2, ensure_ascii=False) + "\n"
        )
    for name, case in build_invalid(valid).items():
        out[FIXTURES / "invalid" / f"{name}.json"] = (
            json.dumps(case, indent=2, ensure_ascii=False) + "\n"
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ns = ap.parse_args(argv)
    expected = render()
    drift = [p for p, c in expected.items() if not p.exists() or p.read_text(encoding="utf-8") != c]
    existing = set((FIXTURES / "valid").glob("*.json")) | set((FIXTURES / "invalid").glob("*.json"))
    stale = sorted(existing - set(expected))
    if ns.check:
        for p in drift + stale:
            print(f"FIXTURE DRIFT: {p.relative_to(ROOT)}")
        print(
            f"gen_event_fixtures: {len(expected)} fixtures, {len(drift)} drifted, {len(stale)} stale"
        )
        return 1 if (drift or stale) else 0
    for p, c in expected.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c, encoding="utf-8")
    for p in stale:
        p.unlink()
    print(f"gen_event_fixtures: wrote {len(expected)} fixtures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
