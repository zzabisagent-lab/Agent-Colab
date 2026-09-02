"""Pure planning tests for the notification engine (V-P1-31)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from server.notifications.rules import (
    NotificationRuleError,
    Preference,
    Reminders,
    dedupe_key,
    load_rules,
    plan_notifications,
    reminder_times,
    subject_of,
    window_bucket,
)
from server.notifications.rules import _parse_rule as parse_rule

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "notifications" / "cases.yaml"
DATA = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
RULES = [parse_rule(r) for r in DATA["rules"]]


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _event(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": "evt-x",
        "workspace_id": "ws",
        "payload": {},
        "channel_id": None,
        "actor_account_id": "a",
        **spec,
    }


@pytest.mark.parametrize("case", DATA["cases"], ids=[c["name"] for c in DATA["cases"]])
def test_planning_cases(case: dict[str, Any]) -> None:
    prefs = {k: Preference(**v) for k, v in case.get("preferences", {}).items()}
    planned = plan_notifications(
        RULES, _event(case["event"]), case["recipients"], prefs, _ts(case.get("now", DATA["now"]))
    )
    got = [(p.rule_id, p.recipient, p.status) for p in planned]
    assert got == [(e["rule_id"], e["recipient"], e["status"]) for e in case["expect"]]
    for p, e in zip(planned, case["expect"], strict=True):
        if "channels" in e:
            assert list(p.channels) == e["channels"]
        if "deliver_at" in e:
            assert p.deliver_at == _ts(e["deliver_at"])
    assert len({p.dedupe_key for p in planned}) == len(planned)


@pytest.mark.parametrize("case", DATA["dedupe"], ids=[c["name"] for c in DATA["dedupe"]])
def test_dedupe_keys(case: dict[str, Any]) -> None:
    rule = next(r for r in RULES if r.rule_id == case["rule"])
    keys = [
        dedupe_key(
            rule.rule_id,
            "acct-x",
            subject_of(e),
            window_bucket(_ts(e["occurred_at"]), rule.dedupe_window_seconds),
        )
        for e in case["events"]
    ]
    assert (keys[0] == keys[1]) is case["same_key"]


def test_reminder_times_follow_validity() -> None:
    start, end = _ts("2026-03-01T00:00:00+00:00"), _ts("2026-03-02T00:00:00+00:00")
    times = reminder_times(start, end, Reminders((0.5,), True))
    assert times == [("reminder:50", _ts("2026-03-01T12:00:00+00:00")), ("reminder:expiry", end)]


def test_default_rules_load_and_cover_7g() -> None:
    rules = load_rules()
    by_type = {r.event_type: r for r in rules}
    assert by_type["APPROVAL_REQUESTED"].reminders == Reminders((0.5,), True)
    assert by_type["VERIFIER_ASSIGNED"].re_notify_after_seconds == 600
    assert by_type["AGENT_MARKED_OFFLINE"].dedupe_window_seconds == 3600
    assert {
        "TASK_WAITING",
        "BUDGET_EXCEEDED",
        "BREAK_GLASS_STARTED",
        "HARD_DELETE_REQUESTED",
        "RUN_FAILED",
    } <= set(by_type)


def test_invalid_rules_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "rules.yaml"
    bad.write_text(
        "version: 1\nrules:\n  - {rule_id: ntf-x, event_type: X, recipient_selectors: [nobody], "
        "channels: [mattermost:dm], dedupe_window_seconds: 0}\n"
    )
    with pytest.raises(NotificationRuleError) as exc:
        load_rules(bad)
    assert exc.value.code == "NOTIFICATION_RULES_INVALID"
