"""Bridge rules (P2-05/P2-06): direction matrix, content filters, loop markers, redaction,
prefix origin detection, dead-letter/replay bookkeeping — pure parts with fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from server.channels import telegram_contract as tc
from server.channels.bridge_admin import BridgeAdminError, validate_config
from server.channels.telegram.bridge import (
    ORIGIN_PROP,
    Bridge,
    BridgeRow,
    MattermostBridgeProvider,
    TelegramBridgeProvider,
    _prefix_origin,
    origin_marker,
    parse_origin_prop,
)
from server.channels.telegram.client import FakeTelegramClient
from server.channels.telegram.redaction import redact

FIXTURES = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "fixtures" / "bridge" / "rules.yaml").read_text()
)


def _row(**over: Any) -> BridgeRow:
    base: dict[str, Any] = {
        "bridge_id": "bridge-unit",
        "workspace_id": "ws",
        "channel_id": "ch",
        "channel_ext_id": "ext",
        "mm_provider_instance_id": "mm:x",
        "provider_instance_id": "tg:1",
        "telegram_chat_id": "-100",
        "telegram_thread_id": None,
        "thread_mode": "topic_per_root",
        "direction": tc.Direction.BIDIRECTIONAL,
        "content_policy": {},
        "redaction_policy": {},
        "identity_display": {},
        "allow_commands": False,
        "status": "enabled",
    }
    base.update(over)
    return BridgeRow(**base)


@pytest.mark.parametrize(
    "case", FIXTURES["direction_matrix"], ids=lambda c: f"{c['direction']}/{c['platform']}"
)
def test_direction_matrix(case: dict[str, Any]) -> None:
    cfg = _row(direction=tc.Direction(case["direction"])).config()
    event = tc.InboundEvent(platform=tc.Platform(case["platform"]), message_id="1", sender_name="a")
    if case["allowed"]:
        tc.check_direction(cfg, event)
    else:
        with pytest.raises(tc.BridgeError) as exc:
            tc.check_direction(cfg, event)
        assert exc.value.code == "BRIDGE_DIRECTION_DENIED"


@pytest.mark.parametrize("case", FIXTURES["content_filters"], ids=lambda c: c["kind"])
def test_content_filters(case: dict[str, Any]) -> None:
    assert (
        Bridge._content_allowed(_row(content_policy=case["policy"]), case["kind"])
        is case["allowed"]
    )


@pytest.mark.parametrize("case", FIXTURES["loop_cases"], ids=lambda c: c["name"])
def test_loop_cases(case: dict[str, Any]) -> None:
    event = tc.InboundEvent(
        platform=tc.Platform.MATTERMOST,
        message_id="p1",
        sender_name="a",
        hop_count=case["hop"],
        origin_platform=tc.Platform(case["origin"]) if case["origin"] else None,
        is_bridge_bot=case["is_bridge_bot"],
    )
    if case["blocked"]:
        with pytest.raises(tc.BridgeError) as exc:
            tc.check_loop(event)
        assert exc.value.code == "BRIDGE_LOOP_DETECTED"
    else:
        tc.check_loop(event)


@pytest.mark.parametrize("case", FIXTURES["redaction_cases"], ids=lambda c: c["text"][:20])
def test_redaction_reports_kinds_never_values(case: dict[str, Any]) -> None:
    result = redact(case["text"])
    assert list(result.findings) == case["findings"]
    for secret in (
        "CANARY-NOT-A-SECRET",
        "hunter2secret",
        "SuperSecret123",
        "AAHfiKQyQrTd2a0FEnFYMIyTd0YrZqE3cP8",
        "MIIE",
    ):
        assert secret not in result.text or (
            secret == "MIIE" and "private_key" not in result.findings
        )


@pytest.mark.parametrize("case", FIXTURES["prefix_cases"], ids=lambda c: c["text"][:18])
def test_prefix_origin(case: dict[str, Any]) -> None:
    got = _prefix_origin(case["text"])
    assert (got.value if got else None) == case["origin"]


def test_origin_marker_props_round_trip() -> None:
    marker = origin_marker(tc.Platform.TELEGRAM, "555", 1)
    assert marker == "colab-bridge:telegram:555:hop1"
    props = {ORIGIN_PROP: {"origin": "telegram:555", "hop": 1, "bridge_id": "b"}}
    assert parse_origin_prop(props) == (tc.Platform.TELEGRAM, "555", 1)
    assert parse_origin_prop({}) == (None, None, 0)
    assert parse_origin_prop({ORIGIN_PROP: {"origin": "mars:1", "hop": 3}}) == (None, None, 3)


def test_thread_mapping_round_trips_through_contract() -> None:
    cfg = _row().config()
    existing: dict[str, tc.Mapping] = {}
    root = tc.InboundEvent(
        platform=tc.Platform.MATTERMOST, message_id="root1", sender_name="a", mm_channel_id="ext"
    )
    target = tc.resolve_target(existing, cfg, root)
    assert (
        target.platform is tc.Platform.TELEGRAM
        and target.create_topic
        and target.tg_thread_id is None
    )
    existing[tc.mapping_key("bridge-unit", "mattermost", "root1")] = tc.Mapping(
        "bridge-unit",
        tc.Platform.MATTERMOST,
        "root1",
        tc.Platform.MATTERMOST,
        "root1",
        1,
        "ext",
        "root1",
        None,
        "-100",
        900,
        77,
        None,
    )
    reply = tc.InboundEvent(
        platform=tc.Platform.MATTERMOST,
        message_id="r2",
        sender_name="a",
        mm_channel_id="ext",
        mm_root_id="root1",
    )
    t2 = tc.resolve_target(existing, cfg, reply)
    assert (t2.tg_thread_id, t2.tg_reply_to_message_id) == (77, 900)
    back = tc.InboundEvent(
        platform=tc.Platform.TELEGRAM,
        message_id="901",
        sender_name="t",
        tg_chat_id="-100",
        tg_thread_id=77,
    )
    t3 = tc.resolve_target(existing, cfg, back)
    assert t3.platform is tc.Platform.MATTERMOST and t3.mm_root_id == "root1"
    tg_reply = tc.InboundEvent(
        platform=tc.Platform.TELEGRAM,
        message_id="902",
        sender_name="t",
        tg_chat_id="-100",
        tg_thread_id=77,
        tg_reply_to_message_id=900,
    )
    assert tc.resolve_target(existing, cfg, tg_reply).mm_root_id == "root1"


def test_providers_are_idempotent_per_dedupe_key() -> None:
    client = FakeTelegramClient()
    provider = TelegramBridgeProvider(client)
    payload = {
        "dedupe_key": "k1",
        "text": "hi",
        "message_thread_id": None,
        "create_topic": "MM root",
    }
    first = provider.deliver("telegram:-100", payload)
    second = provider.deliver("telegram:-100", payload)
    assert first == second and first.count(":") == 1 and first.split(":")[0]  # "<thread>:<message>"
    assert len([c for c in client.calls if c[0] == "sendMessage"]) == 1
    assert len([c for c in client.calls if c[0] == "createForumTopic"]) == 1

    class _MM:
        def __init__(self) -> None:
            self.posts: list[dict[str, Any]] = []

        def create_post(
            self,
            channel_id: str,
            message: str,
            root_id: str | None = None,
            props: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.posts.append(
                {"id": f"p{len(self.posts) + 1}", "channel_id": channel_id, "root_id": root_id}
            )
            return self.posts[-1]

    mm = _MM()
    mp = MattermostBridgeProvider(mm)
    assert (
        mp.deliver("mattermost:ext", {"dedupe_key": "k2", "message": "x", "root_id": None}) == "p1"
    )
    assert (
        mp.deliver("mattermost:ext", {"dedupe_key": "k2", "message": "x", "root_id": None}) == "p1"
    )
    assert len(mm.posts) == 1


def test_config_validation() -> None:
    validate_config(
        {
            "channel_id": "c",
            "provider_instance_id": "tg:1",
            "telegram_chat_id": "-100",
            "direction": "bidirectional",
        }
    )
    with pytest.raises(BridgeAdminError) as exc:
        validate_config(
            {
                "channel_id": "c",
                "provider_instance_id": "tg:1",
                "telegram_chat_id": "-100",
                "direction": "mm_to_tg",
            }
        )
    assert exc.value.code == "BRIDGE_CONFIG_INVALID"
    with pytest.raises(BridgeAdminError):
        validate_config(
            {
                "channel_id": "c",
                "provider_instance_id": "tg:1",
                "telegram_chat_id": "-100",
                "direction": "bidirectional",
                "thread_mode": "fixed_topic",
            }
        )
