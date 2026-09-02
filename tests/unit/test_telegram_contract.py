"""Telegram Bridge thread-mapping contract (P0-13, V-P0-19).

Table-driven mapping cases plus a machine check that the contract constants agree with the spike
observations recorded in ``evidence/phase-0/spikes/telegram/summary.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from server.channels import telegram_contract as tc
from server.channels.telegram_contract import (
    BridgeConfig,
    BridgeError,
    Direction,
    InboundEvent,
    Mapping,
    Platform,
    display_prefix,
    mapping_key,
    resolve_target,
    retry_delay,
    send_params_for,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = yaml.safe_load((ROOT / "tests/fixtures/telegram/mapping-cases.yaml").read_text("utf-8"))
SUMMARY_PATH = ROOT / "evidence/phase-0/spikes/telegram/summary.json"
SCHEMA = json.loads(
    (ROOT / "schemas/api/telegram/bridge-mapping.v1.schema.json").read_text("utf-8")
)


def _bridge(name: str) -> BridgeConfig:
    spec = dict(FIXTURE["bridges"][name])
    spec["direction"] = Direction(spec["direction"])
    return BridgeConfig(**spec)


def _mapping(name: str) -> Mapping:
    spec = dict(FIXTURE["mappings"][name])
    spec["source_platform"] = Platform(spec["source_platform"])
    spec["origin_platform"] = Platform(spec["origin_platform"])
    return Mapping(**spec)


def _existing(names: list[str]) -> dict[str, Mapping]:
    out: dict[str, Mapping] = {}
    for n in names:
        m = _mapping(n)
        out[mapping_key(m.bridge_id, m.source_platform, m.source_message_id)] = m
    return out


def _event(spec: dict[str, Any]) -> InboundEvent:
    spec = dict(spec)
    spec["platform"] = Platform(spec["platform"])
    if "origin_platform" in spec:
        spec["origin_platform"] = Platform(spec["origin_platform"])
    return InboundEvent(**spec)


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]])
def test_mapping_cases(case: dict[str, Any]) -> None:
    bridge = _bridge(case["bridge"])
    existing = _existing(case.get("existing", []))
    event = _event(case["event"])
    if "error" in case:
        with pytest.raises(BridgeError) as exc:
            resolve_target(existing, bridge, event)
        assert exc.value.code == case["error"]
        return
    target = resolve_target(existing, bridge, event)
    expect = dict(case["expect"])
    has_thread = expect.pop("send_params_has_thread", None)
    for key, value in expect.items():
        actual = getattr(target, key)
        assert (actual.value if isinstance(actual, Platform) else actual) == value, key
    if has_thread is not None:
        params = send_params_for(target)
        assert ("message_thread_id" in params) is has_thread
        assert 1 not in (params.get("message_thread_id"),), "General topic must omit the thread"
    assert target.hop_count == 1


def test_mapping_key_is_unique_per_bridge_platform_and_message() -> None:
    a = mapping_key("bridge-1", Platform.TELEGRAM, 9)
    assert a == mapping_key("bridge-1", "telegram", "9")
    assert a != mapping_key("bridge-2", Platform.TELEGRAM, 9)
    assert a != mapping_key("bridge-1", Platform.MATTERMOST, 9)
    assert len(a) == 64


def test_display_prefix_shows_sender_and_source() -> None:
    assert display_prefix("alice", Platform.TELEGRAM) == "[alice via Telegram]"
    assert display_prefix("bob", "mattermost") == "[bob via Mattermost]"


def test_retry_delay_honours_retry_after_and_caps() -> None:
    assert retry_delay(33, 1) == 33.0
    assert retry_delay(0, 1) == 1.0
    assert retry_delay(999, 1) == float(tc.RETRY_AFTER_CAP_S)
    assert [retry_delay(None, n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]


def test_mapping_rows_validate_against_schema() -> None:
    validator = Draft202012Validator(SCHEMA)
    for name in FIXTURE["mappings"]:
        m = _mapping(name)
        row = {
            "bridge_id": m.bridge_id,
            "source_platform": m.source_platform.value,
            "source_message_id": m.source_message_id,
            "origin_platform": m.origin_platform.value,
            "origin_message_id": m.origin_message_id,
            "hop_count": m.hop_count,
            "mattermost": {
                "channel_id": m.mm_channel_id,
                "post_id": m.mm_post_id,
                "root_id": m.mm_root_id,
            },
            "telegram": {
                "chat_id": m.tg_chat_id,
                "message_id": m.tg_message_id,
                "message_thread_id": m.tg_thread_id,
                "reply_to_message_id": m.tg_reply_to_message_id,
            },
            "redaction_status": "clean",
            "delivery_status": "delivered",
        }
        validator.validate(row)
    bad = {"bridge_id": "x"}
    assert list(validator.iter_errors(bad))
    hop2 = json.loads(json.dumps(row))
    hop2["hop_count"] = 2
    assert list(validator.iter_errors(hop2)), "hop_count above 1 must be rejected"


def test_contract_matches_spike_observations() -> None:
    """V-P0-19: zero contradictions between the mapping rules and the spike results."""
    summary = json.loads(SUMMARY_PATH.read_text("utf-8"))
    obs = summary["observations"]
    steps = summary["steps"]
    assert (
        steps["getChat:chat-A"]["is_forum"] is True and steps["getChat:chat-B"]["is_forum"] is True
    )
    assert (
        obs["topic_thread_id_equals_first_message_id"] is tc.TOPIC_THREAD_ID_IS_SERVICE_MESSAGE_ID
    )
    assert obs["topic_creating_message_is_forum_topic_created"] is True
    assert obs["reply_in_topic_keeps_thread_id"] is True and obs["reply_to_message_present"] is True
    # General topic: no thread id on messages, and thread id 1 is rejected → contract uses None
    assert obs["general_topic_message_has_thread_id"] is False
    assert obs["general_topic_accepts_thread_id_1"] is False
    assert tc.GENERAL_TOPIC_THREAD_ID is None
    # cross-topic reply lands in the thread named by the parameter
    assert obs["cross_topic_reply"]["ok"] is True
    assert (
        obs["cross_topic_reply"]["lands_in_thread"]
        == steps["createForumTopic:chat-B"]["message_thread_id"]
    )
    assert tc.REPLY_TARGET_THREAD_FROM_PARAMETER is True
    # edits: own messages only
    assert obs["edit_own_message_possible"] is True and obs["edit_foreign_message_rejected"] is True
    assert tc.EDIT_OWN_MESSAGES_ONLY is True
    # rate limit: the burst hit 429 with retry_after; the bucket must not exceed what was accepted
    burst = obs["burst_40_messages"]
    assert burst["http_429"] > 0 and burst["max_retry_after_s"] > 0
    assert tc.RATE_BUCKET_CAPACITY >= burst["ok"] >= 1
    assert tc.RATE_BUCKET_CAPACITY / tc.RATE_BUCKET_WINDOW_S <= 1.0 >= tc.RATE_SUSTAINED_PER_S * 0.5
    assert tc.RETRY_AFTER_CAP_S >= burst["max_retry_after_s"]
    # cleanup rights needed by the Bridge (delete own messages, manage topics)
    assert obs["delete_own_messages_possible"] is True and obs["delete_topic_possible"] is True
    for chat in ("chat-A", "chat-B"):
        rights = obs[f"rights:{chat}"]
        assert rights["status"] == "administrator" and rights["can_manage_topics"] is True
