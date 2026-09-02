"""P3-12/P3-04 (V-P3-23 unit part): bot adapter advertises unsupported secret handles, refuses
secret-carrying items, renders the structured work message, parses replies."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from server.agents.adapters.contract import AdapterError, DeliveryMode, WorkItemView
from server.agents.adapters.mattermost_bot import MattermostBotAdapter
from server.agents.adapters.secret_support import supports_secret_handles
from server.channels.work_messages import extract_json_block, render_work_message
from server.domain.clock import FixedClock

T0 = dt.datetime(2026, 5, 1, 9, 0, tzinfo=dt.UTC)


def _item(wid: str = "wi-00000000000000000000b001", handles: tuple[str, ...] = ()) -> WorkItemView:
    return WorkItemView(
        work_item_id=wid,
        kind="task_assignment",
        agent_id="agent-bot-1",
        task_id="task-bot-1",
        correlation_id="corr-bot-1",
        deadline=T0 + dt.timedelta(hours=1),
        payload_ref=f"colab://work/{wid}/payload",
        secret_handles=handles,
        expected_result_schema="colab.work-result.v1",
        idempotency_key="idem-bot-1",
        payload={"delivery_no": 1, "payload_size_bytes": 7},
    )


def _adapter(sink: Any = None) -> MattermostBotAdapter:
    return MattermostBotAdapter(
        {
            "agent_id": "agent-bot-1",
            "provider_instance_id": "mm:test:bot",
            "bot_user_id": "mm-bot-user",
            "bot_username": "research-bot",
            "capabilities": ["summarize"],
        },
        sink=sink,
        clock=FixedClock(T0),
    )


def test_probe_declares_unsupported_secret_handles_and_push_only() -> None:
    probe = _adapter().probe()
    assert probe.secret_handles == "unsupported" and "secret_handles" in probe.unsupported
    assert probe.delivery_modes == (DeliveryMode.PUSH,)
    assert probe.identity_hash == _adapter().probe().identity_hash  # CS-01
    assert supports_secret_handles("mattermost_bot") is False
    assert supports_secret_handles("webhook") and supports_secret_handles("mcp")


def test_deliver_renders_work_message_once_and_refuses_secret_items() -> None:
    sent: list[dict[str, Any]] = []

    def sink(_item: WorkItemView, message: dict[str, Any]) -> str:
        sent.append(message)
        return f"ref-{len(sent)}"

    adapter = _adapter(sink)
    r1 = adapter.deliver(_item())
    r2 = adapter.deliver(_item())  # idempotent per work item: one message (CS-02)
    assert r1.receipt_id == r2.receipt_id == "ref-1" and len(sent) == 1
    text = sent[0]["message"]
    assert text.startswith("@research-bot work item `wi-00000000000000000000b001`")
    assert "```json" in text and '"schema_id": "colab.work-item.v1"' in text
    rejected = adapter.deliver(_item("wi-00000000000000000000b002", ("sh-00000000cafe",)))
    assert rejected.rejection_code == "CAPABILITY_UNSUPPORTED" and len(sent) == 1
    with pytest.raises(AdapterError) as exc:
        adapter.invoke("summarize", {}, T0, ["sh-0000000a"], correlation_id="c")
    assert exc.value.code == "CAPABILITY_UNSUPPORTED"
    with pytest.raises(AdapterError) as unsupported:
        adapter.invoke("shell", {}, T0, [], correlation_id="c")
    assert unsupported.value.code == "CAPABILITY_UNSUPPORTED"


def test_reply_parsing() -> None:
    body = render_work_message({"work_item_id": "wi-1", "kind": "invoke"}, "b")
    assert extract_json_block(body) == {"work_item_id": "wi-1", "kind": "invoke"}
    assert extract_json_block("plain chat, no block") is None
    with pytest.raises(ValueError):
        extract_json_block("```json\n{not json\n```")
    with pytest.raises(ValueError):
        extract_json_block("```json\n[1, 2]\n```")
