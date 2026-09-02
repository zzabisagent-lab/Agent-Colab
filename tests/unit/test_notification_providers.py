"""P2-17 unit rules: destination routing, SMTP gating, DM duplicate guard, relay decisions."""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any

import pytest

from server.notifications.providers import (
    BridgeTarget,
    CompositeProvider,
    NoopProvider,
    NotificationDeliveryError,
    SmtpNotificationProvider,
    guard_key,
    relay_allowed,
    render_text,
)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def send(self, destination: str, payload: dict[str, Any]) -> None:
        self.calls.append((destination, payload))


def test_composite_routes_by_prefix_and_rejects_unknown() -> None:
    mm, noop = _Recorder(), NoopProvider()
    composite = CompositeProvider({"mattermost": mm, "work_item": noop})
    composite.send("mattermost:dm:acct", {"notification_id": "n1"})
    composite.send("work_item:acct", {"notification_id": "n2"})
    assert mm.calls[0][0] == "mattermost:dm:acct" and noop.seen == ["work_item:acct"]
    with pytest.raises(NotificationDeliveryError) as exc:
        composite.send("pager:123", {})
    assert exc.value.code == "NOTIFICATION_DESTINATION_INVALID"


class _FakeTransport:
    sent: list[EmailMessage] = []

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.quit_called = False

    def send_message(self, msg: EmailMessage) -> None:
        _FakeTransport.sent.append(msg)

    def quit(self) -> None:
        self.quit_called = True


def test_smtp_disabled_without_host_and_enabled_with_fake_transport() -> None:
    disabled = SmtpNotificationProvider(None, transport=_FakeTransport)
    with pytest.raises(NotificationDeliveryError) as exc:
        disabled.send("smtp:ops@example.test", {"event_type": "BREAK_GLASS_STARTED"})
    assert exc.value.code == "NOTIFICATION_CHANNEL_DISABLED"
    enabled = SmtpNotificationProvider("mail.local", 25, "colab@example.test", _FakeTransport)
    enabled.send(
        "smtp:ops@example.test",
        {
            "event_type": "BREAK_GLASS_STARTED",
            "notification_id": "ntf-1",
            "payload": {"session_id": "bg-1", "scope": "db"},
        },
    )
    assert enabled.sent == [("ops@example.test", "[Agent-Colab] BREAK_GLASS_STARTED")]
    body = _FakeTransport.sent[-1].get_content()
    assert "bg-1" in body and "[ntf-1]" in body
    with pytest.raises(NotificationDeliveryError):
        enabled.send("mattermost:dm:x", {})


def test_render_text_covers_events_reminders_and_digests() -> None:
    base = {
        "event_type": "APPROVAL_REQUESTED",
        "notification_id": "ntf-9",
        "payload": {
            "action": "external_send",
            "risk": "HIGH",
            "subject_id": "task-1",
            "approval_id": "apr-1",
        },
    }
    text_out = render_text(base)
    assert "external_send" in text_out and "apr-1" in text_out and "[ntf-9]" in text_out
    assert render_text({**base, "reminder": "50pct"}).startswith("Reminder (50pct)")
    digest = render_text({"digest": True, "items": [base, {**base, "notification_id": "ntf-10"}]})
    assert digest.startswith("Notification digest (2 items)") and "[ntf-10]" in digest
    assert render_text({"event_type": "UNKNOWN_X"}) == "UNKNOWN_X"


def test_guard_key_is_stable_per_delivery_and_distinct_per_reminder() -> None:
    p = {
        "notification_id": "ntf-1",
        "event_id": "evt-1",
        "rule_id": "r",
        "channel": "mattermost:dm",
    }
    k1 = guard_key("mattermost:dm:a", p)
    assert k1 == guard_key("mattermost:dm:a", dict(p))
    assert k1 != guard_key("mattermost:dm:a", {**p, "reminder": "expiry"})
    assert k1 != guard_key("mattermost:thread:a", p)
    d1 = guard_key(
        "mattermost:dm:a",
        {"digest": True, "recipient_account_id": "a", "items": [{"event_id": "evt-1"}]},
    )
    assert d1 != k1


@pytest.mark.parametrize(
    ("direction", "status", "policy", "kind", "expected"),
    [
        ("bidirectional", "enabled", {"approval_notice": True}, "approval_notice", True),
        ("mattermost_to_telegram", "enabled", {"approval_notice": True}, "approval_notice", True),
        ("telegram_to_mattermost", "enabled", {"approval_notice": True}, "approval_notice", False),
        ("bidirectional", "disabled", {"approval_notice": True}, "approval_notice", False),
        ("bidirectional", "enabled", {"approval_notice": False}, "approval_notice", False),
        ("bidirectional", "enabled", {"approval_notice": True}, "system_event", False),
        ("bidirectional", "enabled", {"system_event": True}, "system_event", True),
    ],
)
def test_relay_gate_decisions(
    direction: str, status: str, policy: dict[str, bool], kind: str, expected: bool
) -> None:
    bridge = BridgeTarget("br-1", "-100", None, direction, status, policy)
    assert relay_allowed(bridge, kind) is expected
