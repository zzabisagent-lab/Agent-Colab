"""P2-08 Telegram command policy (V-P2-16 unit part): read/reply only by default, §7A.6
restricted grammar when enabled, policy-opened verbs, command detection and notice throttling."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from server.channels import policy as tg_policy
from server.channels.telegram.commands import (
    command_seed,
    normalize_command_text,
    reply_destination,
)
from server.channels.telegram.intake import InboundMessage

CASES = yaml.safe_load(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "telegram" / "commands-cases.yaml"
    ).read_text()
)
POLICY_CASES: list[dict[str, Any]] = CASES["policy_cases"]
NORMALIZE_CASES: list[dict[str, Any]] = CASES["normalize_cases"]


def test_fixture_has_enough_cases() -> None:
    assert len(POLICY_CASES) + len(NORMALIZE_CASES) >= 12


@pytest.mark.parametrize("case", POLICY_CASES, ids=[c["name"] for c in POLICY_CASES])
def test_policy_decisions(case: dict[str, Any]) -> None:
    content_policy = (
        {"telegram_commands": case["telegram_commands"]} if "telegram_commands" in case else {}
    )
    policy = tg_policy.TelegramCommandPolicy.from_bridge(case["allow_commands"], content_policy)
    decision = tg_policy.evaluate(policy, case["resource"], case["verb"])
    assert (decision.allowed, decision.code) == (
        case["expect"]["allowed"],
        case["expect"]["code"],
    )
    assert decision.denied is not decision.allowed


def test_default_policy_is_read_reply_only() -> None:
    policy = tg_policy.TelegramCommandPolicy()
    assert policy.allow_commands is False
    assert policy.allowed_verbs == {"task.show", "task.list", "approve.show", "doc.show"}
    assert tg_policy.TelegramCommandPolicy.from_bridge(False, None) == policy
    # None/empty content policy → the defaults, nothing more
    assert tg_policy.TelegramCommandPolicy.from_bridge(True, {}).allowed_verbs == (
        tg_policy.DEFAULT_ALLOWED_VERBS
    )


def test_every_default_verb_is_read_only() -> None:
    for key in tg_policy.DEFAULT_ALLOWED_VERBS:
        assert key.split(".")[1] in {"show", "list"}


@pytest.mark.parametrize("case", NORMALIZE_CASES, ids=[c["name"] for c in NORMALIZE_CASES])
def test_command_detection(case: dict[str, Any]) -> None:
    assert normalize_command_text(case["text"]) == case["expect"]
    assert normalize_command_text(None) is None


def _msg(thread: int | None = None) -> InboundMessage:
    return InboundMessage(
        provider_instance_id="tg:1001",
        update_id=5,
        chat_id="-100777",
        message_id=42,
        date=1_780_000_000,
        message_thread_id=thread,
        reply_to_message_id=None,
        from_user_id="9",
        from_is_bot=False,
        from_display_name="tg",
        text="/colab task list",
    )


def test_seed_and_destination_are_deterministic() -> None:
    assert command_seed(_msg()) == "tg:tg:1001:-100777:42"
    assert reply_destination(_msg()) == "telegram:-100777"
    assert reply_destination(_msg(thread=8)) == "telegram:-100777:8"


def test_notice_throttle_is_hourly_per_user_per_chat() -> None:
    t0 = dt.datetime(2026, 6, 1, 10, 5, tzinfo=dt.UTC)
    key = tg_policy.notice_dedupe_key("tg:1", "-1", "u1", t0)
    # same hour → same key (suppressed by the outbox dedupe), next hour → new key
    assert tg_policy.notice_dedupe_key("tg:1", "-1", "u1", t0 + dt.timedelta(minutes=54)) == key
    assert tg_policy.notice_dedupe_key("tg:1", "-1", "u1", t0 + dt.timedelta(hours=1)) != key
    # other user / other chat / other instance are independent
    assert tg_policy.notice_dedupe_key("tg:1", "-1", "u2", t0) != key
    assert tg_policy.notice_dedupe_key("tg:1", "-2", "u1", t0) != key
    assert tg_policy.notice_dedupe_key("tg:2", "-1", "u1", t0) != key
    assert tg_policy.notice_bucket(t0) == tg_policy.notice_bucket(t0 + dt.timedelta(minutes=54))
