"""Notification delivery providers (P2-17; development plan §7G, §7.5, spec §8.7).

The P1-13 outbox drain calls ``Provider.send(destination, payload)``; these providers turn the
rows into Mattermost DMs / thread mentions / channel posts, SMTP mail, or no-ops:

- ``mattermost:dm:<account uuid>`` → direct message to the Account's Mattermost user (resolved
  through its active ExternalIdentityLink on the provider instance; unlinked → error recorded on
  the outbox row, never a crash of the drain);
- ``mattermost:thread:<account uuid>`` → reply under the subject's Task root post (from
  ``thread_bindings``) mentioning the recipient;
- ``mattermost:approval_channel|ops_channel|channel:<channel uuid>`` → post in that channel; an
  enabled Telegram Bridge may relay it per its content policy (``TelegramRelayGate``);
- ``smtp:<address>`` → mail, only when SMTP is configured;
- ``work_item:<account uuid>`` → no-op here (work items are delivered by the inbox, Phase 3).

Exactly-once argument: the drain marks the outbox row ``sent`` in the same transaction in which
it called the provider. A crash between the provider call and the commit retries the row, so a DM
MAY be re-sent once; the provider keeps an in-memory guard of recently delivered keys and every
message carries its notification id so a duplicate is detectable by the recipient and by audits.
Providers never read or send for recipients whose preference is currently ``muted``.
"""

from __future__ import annotations

import datetime as dt
import json
import smtplib
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from server.channels.mattermost.client import MattermostClient
from server.channels.mattermost.provider import (
    ProviderInstance,
    binding_for_subject,
    client_for,
    load_instance,
)
from server.channels.outbox import Delivery, enqueue_delivery
from server.domain.clock import Clock, SystemClock


class NotificationDeliveryError(RuntimeError):
    """Stable, provider-level failure recorded on the outbox row by the drain."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class Provider(Protocol):
    def send(self, destination: str, payload: dict[str, Any]) -> None: ...


# ------------------------------------------------------------------------------ rendering

EVENT_TEXT: dict[str, str] = {
    "APPROVAL_REQUESTED": "Approval requested: {action} ({risk}) for {subject_id} — {approval_id}",
    "VERIFIER_ASSIGNED": "You were assigned as verifier: {verification_id} for {target_id}",
    "TASK_WAITING": "Task {task_id} is WAITING: {reason_code}",
    "BUDGET_EXCEEDED": "Budget exceeded for {scope_type} {scope_id}",
    "AGENT_MARKED_OFFLINE": "Agent {agent_id} marked offline ({missed_heartbeats} missed)",
    "BREAK_GLASS_STARTED": "Break-glass session started: {session_id} ({scope})",
    "BREAK_GLASS_ENDED": "Break-glass session ended: {session_id}",
    "HARD_DELETE_REQUESTED": "Hard delete requested: {request_id} ({target_type} {target_id})",
    "HARD_DELETE_APPROVED": "Hard delete approved: {request_id}",
    "HARD_DELETE_EXECUTED": "Hard delete executed: {request_id}",
    "DEPENDENCY_FAILURE_DETECTED": "Dependency failure: {dependency} ({error_code})",
    "RUN_SUCCEEDED": "Schedule run {run_id} succeeded",
    "RUN_FAILED": "Schedule run {run_id} failed ({error_code})",
    "RUN_SKIPPED": "Schedule run {run_id} skipped ({error_code})",
    "RUN_TIMED_OUT": "Schedule run {run_id} timed out",
}


def render_text(payload: dict[str, Any]) -> str:
    """Human text for one outbox payload (digest payloads list their items)."""
    if payload.get("digest"):
        items = payload.get("items", [])
        lines = [f"Notification digest ({len(items)} items):"]
        lines += [f"- {render_text(item)}" for item in items]
        return "\n".join(lines)
    event_type = str(payload.get("event_type", ""))
    body = payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {}
    template = EVENT_TEXT.get(event_type, event_type or "Notification")
    try:
        message = template.format(**{k: v for k, v in body.items() if isinstance(v, str | int)})
    except (KeyError, IndexError):
        message = template
    reminder = payload.get("reminder")
    if reminder:
        message = f"Reminder ({reminder}): {message}"
    if payload.get("re_notify"):
        message = f"Re-notification: {message}"
    notification_id = payload.get("notification_id")
    if notification_id:
        message = f"{message} `[{notification_id}]`"
    return message


def guard_key(destination: str, payload: dict[str, Any]) -> str:
    """Key of a delivery for the duplicate guard: outbox dedupe semantics without the row id."""
    if payload.get("digest"):
        first = payload.get("items", [{}])[0]
        return f"{destination}|digest|{payload.get('recipient_account_id')}|{first.get('event_id')}"
    return (
        "|".join(
            str(payload.get(k, ""))
            for k in ("notification_id", "event_id", "rule_id", "channel", "reminder", "re_notify")
        )
        + f"|{destination}"
    )


# ------------------------------------------------------------------------------ Mattermost

InstanceResolver = Callable[[Session, str], ProviderInstance | None]
ClientResolver = Callable[[ProviderInstance], MattermostClient]


@dataclass
class DeliveryLog:
    """What a provider did, for tests and the dashboard (never message bodies with secrets)."""

    dms: list[tuple[str, str]] = field(default_factory=list)  # (account uuid, user id)
    thread_posts: list[tuple[str, str]] = field(default_factory=list)  # (root post id, post id)
    channel_posts: list[tuple[str, str]] = field(default_factory=list)  # (ext channel, post)
    skipped_muted: int = 0
    duplicates_guarded: int = 0
    relayed: int = 0


class MattermostNotificationProvider:
    """P1-13 ``Provider`` for the ``mattermost:*`` destinations."""

    def __init__(
        self,
        session_factory: Any,
        client_resolver: ClientResolver | None = None,
        *,
        relay_gate: TelegramRelayGate | None = None,
        clock: Clock | None = None,
        guard_size: int = 10_000,
    ) -> None:
        self._factory = session_factory
        self._client_for = client_resolver or client_for
        self._relay = relay_gate
        self._clock = clock or SystemClock()
        self._recent: dict[str, str] = {}
        self._guard_size = guard_size
        self.log = DeliveryLog()

    # -- helpers ---------------------------------------------------------------------------
    def _guarded(self, key: str) -> bool:
        if key in self._recent:
            self.log.duplicates_guarded += 1
            return True
        return False

    def _remember(self, key: str, post_id: str) -> None:
        if len(self._recent) >= self._guard_size:
            self._recent.pop(next(iter(self._recent)))
        self._recent[key] = post_id

    @staticmethod
    def _muted(session: Session, account_uuid: str) -> bool:
        row = session.execute(
            text("SELECT muted FROM notification_preferences WHERE account_id = :a"),
            {"a": uuid.UUID(account_uuid)},
        ).first()
        return bool(row[0]) if row else False

    @staticmethod
    def _link(session: Session, account_uuid: str) -> tuple[ProviderInstance, str] | None:
        """The recipient's active Mattermost identity: (provider instance, external user id)."""
        row = session.execute(
            text(
                "SELECT p.provider_instance_id, l.external_user_id FROM external_identity_links l "
                "JOIN provider_instances p ON p.id = l.provider_instance_id "
                "WHERE l.account_id = :a AND l.status = 'active' AND p.provider = 'mattermost' "
                "AND p.status = 'active' ORDER BY p.created_at LIMIT 1"
            ),
            {"a": uuid.UUID(account_uuid)},
        ).first()
        if row is None:
            return None
        instance = load_instance(session, str(row[0]))
        if instance is None:
            return None
        return instance, str(row[1])

    @staticmethod
    def _channel(session: Session, channel_uuid: str) -> tuple[ProviderInstance, str, str] | None:
        row = session.execute(
            text(
                "SELECT p.provider_instance_id, c.external_channel_id, c.workspace_id "
                "FROM channels c JOIN provider_instances p ON p.id = c.provider_instance_id "
                "WHERE c.id = :c AND c.external_channel_id IS NOT NULL "
                "AND p.provider = 'mattermost'"
            ),
            {"c": uuid.UUID(channel_uuid)},
        ).first()
        if row is None:
            return None
        instance = load_instance(session, str(row[0]))
        if instance is None:
            return None
        return instance, str(row[1]), str(row[2])

    @staticmethod
    def _subject_task(payload: dict[str, Any]) -> str | None:
        body = payload.get("payload", {}) if isinstance(payload.get("payload"), dict) else {}
        for key in ("task_id", "subject_id", "target_id"):
            value = body.get(key)
            if isinstance(value, str) and value.startswith("task-"):
                return value
        subject = str(payload.get("subject", ""))
        if subject.startswith("task:"):
            return subject.split(":", 1)[1]
        return None

    # -- API -------------------------------------------------------------------------------
    def send(self, destination: str, payload: dict[str, Any]) -> None:
        parts = destination.split(":")
        if len(parts) < 3 or parts[0] != "mattermost":
            raise NotificationDeliveryError("NOTIFICATION_DESTINATION_INVALID", destination)
        kind, target = parts[1], ":".join(parts[2:])
        key = guard_key(destination, payload)
        if self._guarded(key):
            return
        session: Session = self._factory()
        try:
            with session.begin():
                if kind in ("dm", "thread"):
                    post_id = self._send_recipient(session, kind, target, payload)
                elif kind in ("approval_channel", "ops_channel", "channel"):
                    post_id = self._send_channel_post(session, kind, target, payload)
                else:
                    raise NotificationDeliveryError("NOTIFICATION_DESTINATION_INVALID", destination)
        finally:
            session.close()
        if post_id is not None:
            self._remember(key, post_id)

    def _send_recipient(
        self, session: Session, kind: str, account_uuid: str, payload: dict[str, Any]
    ) -> str | None:
        if self._muted(session, account_uuid):
            self.log.skipped_muted += 1
            return None
        link = self._link(session, account_uuid)
        if link is None:
            raise NotificationDeliveryError("NOTIFICATION_RECIPIENT_UNREACHABLE", account_uuid)
        instance, user_id = link
        client = self._client_for(instance)
        message = render_text(payload)
        if kind == "dm":
            post = client.direct_message(user_id, message)
            self.log.dms.append((account_uuid, user_id))
            return post.id
        task_id = self._subject_task(payload)
        binding = binding_for_subject(session, instance.id, "task", task_id) if task_id else None
        if binding is None:
            raise NotificationDeliveryError("NOTIFICATION_THREAD_UNBOUND", task_id or "?")
        username = str(client.get_user(user_id).get("username", user_id))
        post = client.create_post(
            binding.external_channel_id, f"@{username} {message}", root_id=binding.root_post_id
        )
        self.log.thread_posts.append((binding.root_post_id, post.id))
        return post.id

    def _send_channel_post(
        self, session: Session, kind: str, channel_uuid: str, payload: dict[str, Any]
    ) -> str | None:
        target = self._channel(session, channel_uuid)
        if target is None:
            raise NotificationDeliveryError("NOTIFICATION_CHANNEL_UNREACHABLE", channel_uuid)
        instance, external_channel_id, workspace_id = target
        message = render_text(payload)
        post = self._client_for(instance).create_post(external_channel_id, message)
        self.log.channel_posts.append((external_channel_id, post.id))
        if self._relay is not None:
            content_kind = "approval_notice" if kind == "approval_channel" else "system_event"
            relayed = self._relay.relay(
                session,
                workspace_id=workspace_id,
                channel_uuid=channel_uuid,
                content_kind=content_kind,
                message=message,
                dedupe_seed=f"{payload.get('event_id')}|{kind}",
                source_event_id=str(payload.get("event_id")) if payload.get("event_id") else None,
                provider_instance_id=instance.provider_instance_id,
                external_channel_id=external_channel_id,
                now=self._clock.now(),
            )
            self.log.relayed += relayed
        return post.id


# ------------------------------------------------------------------------------ Telegram relay


@dataclass(frozen=True)
class BridgeTarget:
    bridge_id: str
    chat_id: str
    thread_id: str | None
    direction: str
    status: str
    content_policy: dict[str, bool]


def relay_allowed(bridge: BridgeTarget, content_kind: str) -> bool:
    """Pure decision: enabled, MM→TG direction, and the content kind permitted by policy."""
    if bridge.status != "enabled":
        return False
    if bridge.direction not in ("mattermost_to_telegram", "bidirectional"):
        return False
    return bool(bridge.content_policy.get(content_kind, False))


BridgeLookup = Callable[[Session, str], Iterable[BridgeTarget]]


def bridges_for_channel(session: Session, channel_uuid: str) -> list[BridgeTarget]:
    """Enabled/disabled Bridges of a channel from ``telegram_bridges`` (empty if absent)."""
    try:
        rows = session.execute(
            text(
                "SELECT bridge_id, telegram_chat_id, telegram_thread_id, direction, status, "
                "content_policy FROM telegram_bridges WHERE channel_id = :c ORDER BY bridge_id"
            ),
            {"c": uuid.UUID(channel_uuid)},
        ).all()
    except ProgrammingError:
        session.rollback()
        return []
    out: list[BridgeTarget] = []
    for bridge_id, chat, thread, direction, status, policy in rows:
        policy_dict = policy if isinstance(policy, dict) else json.loads(policy or "{}")
        out.append(
            BridgeTarget(
                str(bridge_id),
                str(chat),
                None if thread is None else str(thread),
                str(direction),
                str(status),
                {k: bool(v) for k, v in policy_dict.items()},
            )
        )
    return out


class TelegramRelayGate:
    """Decides and enqueues Telegram relays of channel notifications per Bridge policy (§10.2)."""

    def __init__(self, lookup: BridgeLookup = bridges_for_channel) -> None:
        self._lookup = lookup

    def targets(self, session: Session, channel_uuid: str, content_kind: str) -> list[BridgeTarget]:
        return [b for b in self._lookup(session, channel_uuid) if relay_allowed(b, content_kind)]

    def relay(
        self,
        session: Session,
        *,
        workspace_id: str,
        channel_uuid: str,
        content_kind: str,
        message: str,
        dedupe_seed: str,
        source_event_id: str | None,
        provider_instance_id: str,
        external_channel_id: str,
        now: dt.datetime,
    ) -> int:
        count = 0
        for bridge in self.targets(session, channel_uuid, content_kind):
            destination = f"telegram:{bridge.chat_id}" + (
                f":{bridge.thread_id}" if bridge.thread_id else ""
            )
            delivery = Delivery(
                "telegram.send",
                destination,
                {"text": f"[Agent-Colab] {message}", "content_kind": content_kind},
                f"relay:{bridge.bridge_id}:{dedupe_seed}",
                subject_type="notification",
                subject_id=dedupe_seed,
                role="reply",
            )
            if enqueue_delivery(
                session,
                workspace_id=workspace_id,
                source_event_id=source_event_id,
                delivery=delivery,
                provider_instance_id=provider_instance_id,
                external_channel_id=external_channel_id,
                now=now,
            ):
                count += 1
        return count


# ------------------------------------------------------------------------------ SMTP


class SmtpTransport(Protocol):
    def send_message(self, msg: EmailMessage) -> Any: ...

    def quit(self) -> Any: ...


TransportFactory = Callable[[str, int], SmtpTransport]


def _smtp_factory(host: str, port: int) -> SmtpTransport:
    return smtplib.SMTP(host, port, timeout=10)


class SmtpNotificationProvider:
    """``smtp:<address>`` deliveries; disabled (raises) unless a host is configured."""

    def __init__(
        self,
        host: str | None,
        port: int = 587,
        sender: str = "agent-colab@localhost",
        transport: TransportFactory = _smtp_factory,
    ) -> None:
        self.host, self.port, self.sender = host, port, sender
        self._transport = transport
        self.sent: list[tuple[str, str]] = []

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def send(self, destination: str, payload: dict[str, Any]) -> None:
        if not destination.startswith("smtp:"):
            raise NotificationDeliveryError("NOTIFICATION_DESTINATION_INVALID", destination)
        if not self.enabled:
            raise NotificationDeliveryError("NOTIFICATION_CHANNEL_DISABLED", "smtp")
        address = destination.split(":", 1)[1]
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = address
        msg["Subject"] = f"[Agent-Colab] {payload.get('event_type', 'Notification')}"
        msg.set_content(render_text(payload))
        transport = self._transport(str(self.host), self.port)
        try:
            transport.send_message(msg)
        finally:
            transport.quit()
        self.sent.append((address, str(msg["Subject"])))


class NoopProvider:
    """Destinations delivered elsewhere (``work_item:`` → the Phase 3 inbox)."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def send(self, destination: str, payload: dict[str, Any]) -> None:
        self.seen.append(destination)


class CompositeProvider:
    """Route by destination prefix; unknown prefixes are a stable error (row retried/dead)."""

    def __init__(self, providers: dict[str, Provider]) -> None:
        self._providers = providers

    def send(self, destination: str, payload: dict[str, Any]) -> None:
        prefix = destination.split(":", 1)[0]
        provider = self._providers.get(prefix)
        if provider is None:
            raise NotificationDeliveryError("NOTIFICATION_DESTINATION_INVALID", destination)
        provider.send(destination, payload)
