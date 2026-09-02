"""Per-channel Telegram Bridge (P2-05) with dedupe, loop prevention, retry and dead letters
(P2-06). Spec §10, development plan §3.1 (Telegram Bridge), §6.5, §7G.

Flow for an observed message (either platform):

1. load the *enabled* Bridges of that channel / chat (per-channel isolation);
2. contract checks in order: loop (origin marker, hop count, bot author) → direction policy →
   duplicate source (``message_mappings`` unique) → thread target (``telegram_contract``);
3. content filters and redaction: only the redacted text is persisted or forwarded, findings are
   audited as kinds;
4. one ``message_mappings`` row (pending) and one ``delivery_outbox`` row are written in the
   caller's transaction; the outbox drain delivers through a provider wrapper whose returned
   destination id completes the mapping (``record_delivered``);
5. failures follow the outbox backoff; rows that exhaust their attempts move to
   ``bridge_dead_letters`` with a ``BRIDGE_DELIVERY_FAILED`` Event and can be replayed exactly once.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.channels import telegram_contract as tc
from server.channels.outbox import ChannelProvider, Delivery, drain_channels, enqueue_delivery
from server.channels.telegram.client import TelegramClient
from server.channels.telegram.intake import InboundMessage
from server.channels.telegram.redaction import redact
from server.domain.clock import Clock
from server.events.store import AppendRequest, EventStore
from server.observability.audit import append_audit

ORIGIN_PROP = "agent_colab_bridge"
_PREFIX_RE = re.compile(r"^\[(?P<sender>[^\]]+) via (?P<source>Telegram|Mattermost)\]\s")
BRIDGE_MAX_ATTEMPTS = 8  # backoff 1/5/25/125/625 s: a 10-minute outage never dead-letters


class BridgeError(tc.BridgeError):
    """Stable Bridge errors (codes from the P0-13 contract plus admin codes)."""


@dataclass(frozen=True)
class AttachmentMeta:
    name: str
    mime: str
    size: int
    url: str | None = None


@dataclass(frozen=True)
class MattermostPostView:
    """A Mattermost post as observed by the gateway (WebSocket ``posted``)."""

    provider_instance_id: str  # public id of the Mattermost provider instance
    channel_ext_id: str
    post_id: str
    root_id: str | None
    user_id: str
    user_label: str
    message: str
    attachments: tuple[AttachmentMeta, ...] = ()
    props: dict[str, Any] = field(default_factory=dict)
    kind: str = "text"  # text | system_event | approval_notice | mention
    user_is_bot: bool = False


@dataclass(frozen=True)
class BridgeRow:
    bridge_id: str
    workspace_id: str
    channel_id: str
    channel_ext_id: str
    mm_provider_instance_id: str
    provider_instance_id: str
    telegram_chat_id: str
    telegram_thread_id: int | None
    thread_mode: str
    direction: tc.Direction
    content_policy: dict[str, Any]
    redaction_policy: dict[str, Any]
    identity_display: dict[str, Any]
    allow_commands: bool
    status: str

    def config(self) -> tc.BridgeConfig:
        return tc.BridgeConfig(
            bridge_id=self.bridge_id,
            mm_channel_id=self.channel_ext_id,
            tg_chat_id=self.telegram_chat_id,
            direction=self.direction,
            tg_thread_mode=self.thread_mode,
            tg_fixed_thread_id=self.telegram_thread_id,
            enabled=self.status == "enabled",
        )


@dataclass
class BridgeMetrics:
    delivered: int = 0
    enqueued: int = 0
    duplicates_prevented: int = 0
    loops_blocked: int = 0
    direction_denied: int = 0
    content_filtered: int = 0
    redacted: int = 0
    dead_lettered: int = 0
    replayed: int = 0
    skipped_disabled: int = 0

    def snapshot(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class RelayOutcome:
    bridge_id: str
    accepted: bool
    code: str  # ENQUEUED | BRIDGE_LOOP_DETECTED | BRIDGE_DUPLICATE_SOURCE | BRIDGE_DIRECTION_DENIED | ...
    dedupe_key: str | None = None
    target: tc.Target | None = None


# ----------------------------------------------------------------------------- helpers
def _row(r: Any) -> BridgeRow:
    return BridgeRow(
        bridge_id=str(r["bridge_id"]),
        workspace_id=str(r["workspace_id"]),
        channel_id=str(r["channel_id"]),
        channel_ext_id=str(r["external_channel_id"] or ""),
        mm_provider_instance_id=str(r["mm_provider_instance_id"] or ""),
        provider_instance_id=str(r["provider_instance_id"]),
        telegram_chat_id=str(r["telegram_chat_id"]),
        telegram_thread_id=int(r["telegram_thread_id"]) if r["telegram_thread_id"] else None,
        thread_mode=str(r["thread_mode"]),
        direction=tc.Direction(str(r["direction"])),
        content_policy=dict(r["content_policy"] or {}),
        redaction_policy=dict(r["redaction_policy"] or {}),
        identity_display=dict(r["identity_display"] or {}),
        allow_commands=bool(r["allow_commands"]),
        status=str(r["status"]),
    )


_SELECT = (
    "SELECT b.bridge_id, b.workspace_id, b.channel_id, c.external_channel_id, "
    "p.provider_instance_id AS mm_provider_instance_id, b.provider_instance_id, "
    "b.telegram_chat_id, b.telegram_thread_id, b.thread_mode, b.direction, b.content_policy, "
    "b.redaction_policy, b.identity_display, b.allow_commands, b.status "
    "FROM telegram_bridges b JOIN channels c ON c.id = b.channel_id "
    "LEFT JOIN provider_instances p ON p.id = c.provider_instance_id "
)


def bridges_for_mattermost_channel(
    session: Session, mm_provider_instance_id: str, channel_ext_id: str, *, enabled_only: bool = True
) -> list[BridgeRow]:
    rows = session.execute(
        text(
            _SELECT + "WHERE c.external_channel_id = :ext AND p.provider_instance_id = :pi "
            + ("AND b.status = 'enabled' " if enabled_only else "")
            + "ORDER BY b.bridge_id"
        ),
        {"ext": channel_ext_id, "pi": mm_provider_instance_id},
    ).mappings()
    return [_row(r) for r in rows]


def bridges_for_telegram_chat(
    session: Session, provider_instance_id: str, chat_id: str, *, enabled_only: bool = True
) -> list[BridgeRow]:
    rows = session.execute(
        text(
            _SELECT + "WHERE b.provider_instance_id = :pi AND b.telegram_chat_id = :chat "
            + ("AND b.status = 'enabled' " if enabled_only else "")
            + "ORDER BY b.bridge_id"
        ),
        {"pi": provider_instance_id, "chat": chat_id},
    ).mappings()
    return [_row(r) for r in rows]


def load_bridge(session: Session, bridge_id: str) -> BridgeRow | None:
    row = session.execute(text(_SELECT + "WHERE b.bridge_id = :b"), {"b": bridge_id}).mappings().first()
    return _row(row) if row else None


def completed_mappings(session: Session, bridge_id: str) -> dict[str, tc.Mapping]:
    """Mappings whose destination is known; the contract resolves threads against these."""
    rows = session.execute(
        text(
            "SELECT bridge_id, source_platform, source_message_id, origin_platform, origin_message_id, "
            "hop_count, mm_channel_id, mm_post_id, mm_root_post_id, tg_chat_id, tg_message_id, "
            "tg_thread_id, tg_reply_to_message_id, dedupe_key FROM message_mappings "
            "WHERE bridge_id = :b AND delivery_status = 'sent' AND mm_post_id IS NOT NULL "
            "AND tg_message_id IS NOT NULL"
        ),
        {"b": bridge_id},
    ).mappings()
    out: dict[str, tc.Mapping] = {}
    for r in rows:
        out[str(r["dedupe_key"])] = tc.Mapping(
            bridge_id=str(r["bridge_id"]),
            source_platform=tc.Platform(str(r["source_platform"])),
            source_message_id=str(r["source_message_id"]),
            origin_platform=tc.Platform(str(r["origin_platform"])),
            origin_message_id=str(r["origin_message_id"]),
            hop_count=int(r["hop_count"]),
            mm_channel_id=str(r["mm_channel_id"]),
            mm_post_id=str(r["mm_post_id"]),
            mm_root_id=str(r["mm_root_post_id"]) if r["mm_root_post_id"] else None,
            tg_chat_id=str(r["tg_chat_id"]),
            tg_message_id=int(r["tg_message_id"]),
            tg_thread_id=int(r["tg_thread_id"]) if r["tg_thread_id"] is not None else None,
            tg_reply_to_message_id=(
                int(r["tg_reply_to_message_id"]) if r["tg_reply_to_message_id"] is not None else None
            ),
        )
    return out


def origin_marker(platform: tc.Platform | str, message_id: str, hop: int) -> str:
    return f"colab-bridge:{tc.Platform(platform)}:{message_id}:hop{hop}"


def parse_origin_prop(props: dict[str, Any]) -> tuple[tc.Platform | None, str | None, int]:
    marker = props.get(ORIGIN_PROP)
    if not isinstance(marker, dict):
        return None, None, 0
    origin = str(marker.get("origin", ""))
    hop = int(marker.get("hop", 0) or 0)
    if ":" in origin:
        platform, _, mid = origin.partition(":")
        try:
            return tc.Platform(platform), mid or None, hop
        except ValueError:
            return None, None, hop
    return None, None, hop


def _prefix_origin(message: str) -> tc.Platform | None:
    """A relayed text always starts with ``[sender via <Source>]``; that prefix is an origin mark."""
    m = _PREFIX_RE.match(message)
    if not m:
        return None
    return tc.Platform.TELEGRAM if m.group("source") == "Telegram" else tc.Platform.MATTERMOST


# ----------------------------------------------------------------------------- bridge service
@dataclass
class Bridge:
    """Bridge service. One instance per process; ``metrics`` feeds the dashboard (P4-02)."""

    store: EventStore | None = None
    service_account_uuid: str | None = None  # actor for BRIDGE_DELIVERY_FAILED Events
    mm_bot_user_ids: set[str] = field(default_factory=set)
    tg_bot_user_ids: set[str] = field(default_factory=set)
    metrics: BridgeMetrics = field(default_factory=BridgeMetrics)

    # ---- inbound: Mattermost -> Telegram --------------------------------------------------
    def on_mattermost_post(
        self, session: Session, clock: Clock, post: MattermostPostView
    ) -> list[RelayOutcome]:
        outcomes: list[RelayOutcome] = []
        bridges = bridges_for_mattermost_channel(
            session, post.provider_instance_id, post.channel_ext_id, enabled_only=False
        )
        origin_platform, origin_id, hop = parse_origin_prop(post.props)
        if origin_platform is None:
            origin_platform = _prefix_origin(post.message)
            if origin_platform is not None:
                hop = max(hop, 1)
        event = tc.InboundEvent(
            platform=tc.Platform.MATTERMOST,
            message_id=post.post_id,
            sender_name=post.user_label,
            hop_count=hop,
            origin_platform=origin_platform,
            origin_message_id=origin_id,
            mm_channel_id=post.channel_ext_id,
            mm_root_id=post.root_id,
            is_bridge_bot=post.user_is_bot or post.user_id in self.mm_bot_user_ids,
        )
        for bridge in bridges:
            outcomes.append(
                self._relay(session, clock, bridge, event, post.message, post.kind, post.attachments)
            )
        return outcomes

    # ---- inbound: Telegram -> Mattermost --------------------------------------------------
    def on_telegram_message(
        self, session: Session, clock: Clock, msg: InboundMessage
    ) -> list[RelayOutcome]:
        outcomes: list[RelayOutcome] = []
        bridges = bridges_for_telegram_chat(
            session, msg.provider_instance_id, msg.chat_id, enabled_only=False
        )
        bot_id = msg.provider_instance_id.split(":", 1)[-1]
        origin_platform = _prefix_origin(msg.text)
        hop = 1 if origin_platform is not None else 0
        event = tc.InboundEvent(
            platform=tc.Platform.TELEGRAM,
            message_id=str(msg.message_id),
            sender_name=msg.from_display_name or (msg.from_user_id or "unknown"),
            hop_count=hop,
            origin_platform=origin_platform,
            tg_chat_id=msg.chat_id,
            tg_thread_id=msg.message_thread_id,
            tg_reply_to_message_id=msg.reply_to_message_id,
            is_bridge_bot=msg.from_is_bot
            and (msg.from_user_id in (bot_id, *self.tg_bot_user_ids)),
        )
        kind = "system_event" if msg.forum_topic_created is not None else "text"
        attachments = tuple(
            AttachmentMeta(a.file_name or a.kind, a.mime_type or "", a.file_size or 0)
            for a in msg.attachments
        )
        for bridge in bridges:
            # fixed-topic bridges only relay their own topic
            if bridge.thread_mode == "fixed_topic" and msg.message_thread_id != bridge.telegram_thread_id:
                continue
            outcomes.append(self._relay(session, clock, bridge, event, msg.text, kind, attachments))
        return outcomes

    # ---- core ------------------------------------------------------------------------------
    def _relay(
        self,
        session: Session,
        clock: Clock,
        bridge: BridgeRow,
        event: tc.InboundEvent,
        body: str,
        kind: str,
        attachments: tuple[AttachmentMeta, ...],
    ) -> RelayOutcome:
        if bridge.status != "enabled":
            self.metrics.skipped_disabled += 1
            return RelayOutcome(bridge.bridge_id, False, "BRIDGE_DISABLED")
        existing = completed_mappings(session, bridge.bridge_id)
        try:
            tc.check_loop(event)
        except tc.BridgeError as exc:
            self.metrics.loops_blocked += 1
            self._audit(session, clock, bridge, "bridge.loop_blocked", event, exc.code, exc.detail)
            return RelayOutcome(bridge.bridge_id, False, exc.code)
        try:
            tc.check_direction(bridge.config(), event)
        except tc.BridgeError as exc:
            self.metrics.direction_denied += 1
            self._audit(session, clock, bridge, "bridge.direction_denied", event, exc.code, exc.detail)
            return RelayOutcome(bridge.bridge_id, False, exc.code)
        dedupe_key = tc.mapping_key(bridge.bridge_id, event.platform, event.message_id)
        if dedupe_key in existing or self._mapping_exists(session, bridge.bridge_id, event):
            self.metrics.duplicates_prevented += 1
            self._audit(
                session, clock, bridge, "bridge.duplicate_source", event, "BRIDGE_DUPLICATE_SOURCE", ""
            )
            return RelayOutcome(bridge.bridge_id, False, "BRIDGE_DUPLICATE_SOURCE", dedupe_key)
        if not self._content_allowed(bridge, kind):
            self.metrics.content_filtered += 1
            self._audit(session, clock, bridge, "bridge.content_filtered", event, "CONTENT_FILTERED", kind)
            return RelayOutcome(bridge.bridge_id, False, "BRIDGE_CONTENT_FILTERED", dedupe_key)
        try:
            target = tc.resolve_target(existing, bridge.config(), event)
        except tc.BridgeError as exc:
            self._audit(session, clock, bridge, "bridge.target_unmapped", event, exc.code, exc.detail)
            return RelayOutcome(bridge.bridge_id, False, exc.code, dedupe_key)
        redaction = redact(body)
        if redaction.redacted:
            self.metrics.redacted += 1
            self._audit(
                session, clock, bridge, "bridge.redacted", event, "REDACTED",
                ",".join(redaction.findings),
            )
        allowed_attachments = tuple(a for a in attachments if self._content_allowed(bridge, "attachment"))
        text_out = self._compose(bridge, target, redaction.text, allowed_attachments)
        marker = origin_marker(target.origin_platform or event.platform, target.origin_message_id or event.message_id, 1)
        now = clock.now()
        self._insert_mapping(session, bridge, event, target, dedupe_key, marker, redaction.findings, now)
        delivery = self._delivery(bridge, event, target, text_out, dedupe_key, marker)
        enqueue_delivery(
            session,
            workspace_id=bridge.workspace_id,
            source_event_id=None,
            delivery=delivery,
            provider_instance_id=bridge.provider_instance_id,
            external_channel_id=bridge.channel_ext_id,
            now=now,
        )
        self.metrics.enqueued += 1
        return RelayOutcome(bridge.bridge_id, True, "ENQUEUED", dedupe_key, target)

    @staticmethod
    def _content_allowed(bridge: BridgeRow, kind: str) -> bool:
        policy = bridge.content_policy
        if kind == "mention":
            return bool(policy.get("mention", True)) and bool(policy.get("text", True))
        return bool(policy.get(kind, kind == "text"))

    @staticmethod
    def _compose(
        bridge: BridgeRow, target: tc.Target, body: str, attachments: tuple[AttachmentMeta, ...]
    ) -> str:
        show_sender = bool(bridge.identity_display.get("show_sender", True))
        prefix = f"{target.display_prefix} " if show_sender and target.display_prefix else ""
        text_out = f"{prefix}{body}".strip()
        if attachments:
            names = ", ".join(f"{a.name} ({a.mime}, {a.size} B)" for a in attachments)
            text_out = f"{text_out}\n[attachments: {names}]"
        return text_out

    @staticmethod
    def _delivery(
        bridge: BridgeRow,
        event: tc.InboundEvent,
        target: tc.Target,
        text_out: str,
        dedupe_key: str,
        marker: str,
    ) -> Delivery:
        if target.platform is tc.Platform.TELEGRAM:
            params = tc.send_params_for(target)
            dest = f"telegram:{target.tg_chat_id}"
            if target.tg_thread_id is not None:
                dest += f":{target.tg_thread_id}"
            payload: dict[str, Any] = {
                "text": text_out,
                "message_thread_id": params.get("message_thread_id"),
                "reply_to_message_id": target.tg_reply_to_message_id,
                "create_topic": (f"MM {event.message_id[:8]}" if target.create_topic else None),
                "bridge_id": bridge.bridge_id,
                "origin_marker": marker,
            }
            return Delivery("telegram.send", dest, payload, dedupe_key)
        return Delivery(
            "mattermost.post",
            f"mattermost:{target.mm_channel_id}",
            {
                "message": text_out,
                "root_id": target.mm_root_id,
                "props": {ORIGIN_PROP: {"origin": marker.split(":", 1)[1].rsplit(":hop", 1)[0], "hop": 1, "bridge_id": bridge.bridge_id}},
                "bridge_id": bridge.bridge_id,
                "origin_marker": marker,
            },
            dedupe_key,
        )

    @staticmethod
    def _mapping_exists(session: Session, bridge_id: str, event: tc.InboundEvent) -> bool:
        row = session.execute(
            text(
                "SELECT 1 FROM message_mappings WHERE bridge_id = :b AND source_platform = :p "
                "AND source_message_id = :m"
            ),
            {"b": bridge_id, "p": str(event.platform), "m": event.message_id},
        ).first()
        return row is not None

    @staticmethod
    def _insert_mapping(
        session: Session,
        bridge: BridgeRow,
        event: tc.InboundEvent,
        target: tc.Target,
        dedupe_key: str,
        marker: str,
        findings: tuple[str, ...],
        now: dt.datetime,
    ) -> None:
        mm_side = event.platform is tc.Platform.MATTERMOST
        session.execute(
            text(
                "INSERT INTO message_mappings (workspace_id, bridge_id, source_platform, "
                "source_message_id, destination_platform, mm_channel_id, mm_post_id, "
                "mm_root_post_id, tg_chat_id, tg_message_id, tg_thread_id, tg_reply_to_message_id, "
                "origin_platform, origin_message_id, origin_marker, hop_count, redaction_status, "
                "delivery_status, dedupe_key, created_at) VALUES (:ws, :b, :sp, :sid, :dp, :mmc, "
                ":mmp, :mmr, :tgc, :tgm, :tgt, :tgr, :op, :oid, :marker, 1, :red, 'pending', :key, "
                ":now)"
            ),
            {
                "ws": uuid.UUID(bridge.workspace_id),
                "b": bridge.bridge_id,
                "sp": str(event.platform),
                "sid": event.message_id,
                "dp": str(target.platform),
                "mmc": bridge.channel_ext_id,
                "mmp": event.message_id if mm_side else None,
                "mmr": event.mm_root_id if mm_side else target.mm_root_id,
                "tgc": bridge.telegram_chat_id,
                "tgm": None if mm_side else int(event.message_id),
                "tgt": target.tg_thread_id if mm_side else event.tg_thread_id,
                "tgr": target.tg_reply_to_message_id if mm_side else event.tg_reply_to_message_id,
                "op": str(target.origin_platform or event.platform),
                "oid": target.origin_message_id or event.message_id,
                "marker": marker,
                "red": "redacted:" + ",".join(findings) if findings else "clean",
                "key": dedupe_key,
                "now": now,
            },
        )

    def _audit(
        self,
        session: Session,
        clock: Clock,
        bridge: BridgeRow,
        action: str,
        event: tc.InboundEvent,
        code: str,
        detail: str,
    ) -> None:
        append_audit(
            session,
            action=action,
            target_type="bridge",
            target_id=bridge.bridge_id,
            result="DENY" if code != "REDACTED" else "REDACTED",
            actor_label="bridge",
            correlation_id=f"bridge-{event.platform}-{event.message_id}",
            workspace_id=uuid.UUID(bridge.workspace_id),
            error_code=None if code == "REDACTED" else code,
            metadata={"platform": str(event.platform), "detail": detail[:200]},
            clock=clock,
        )

    # ---- delivery completion ----------------------------------------------------------------
    def record_delivered(
        self, session: Session, dedupe_key: str, destination_message_id: str, now: dt.datetime
    ) -> bool:
        """Complete a mapping with the destination id returned by the provider (idempotent)."""
        row = session.execute(
            text("SELECT destination_platform, delivery_status FROM message_mappings WHERE dedupe_key = :k"),
            {"k": dedupe_key},
        ).first()
        if row is None:
            return False
        if row[1] == "sent":
            return True
        if row[0] == "telegram":
            thread, _, mid = destination_message_id.rpartition(":")
            session.execute(
                text(
                    "UPDATE message_mappings SET destination_message_id = :d, tg_message_id = :m, "
                    "tg_thread_id = COALESCE(:t, tg_thread_id), delivery_status = 'sent', "
                    "delivered_at = :now WHERE dedupe_key = :k"
                ),
                {"d": mid, "m": int(mid), "t": int(thread) if thread else None, "now": now, "k": dedupe_key},
            )
        else:
            session.execute(
                text(
                    "UPDATE message_mappings SET destination_message_id = :d, mm_post_id = :d, "
                    "delivery_status = 'sent', delivered_at = :now WHERE dedupe_key = :k"
                ),
                {"d": destination_message_id, "now": now, "k": dedupe_key},
            )
        self.metrics.delivered += 1
        return True

    def deliver(
        self,
        session: Session,
        providers: dict[str, ChannelProvider],
        clock: Clock,
        workspace_id: str,
        *,
        batch: int = 100,
        max_attempts: int = BRIDGE_MAX_ATTEMPTS,
    ) -> dict[str, int]:
        """Drain the outbox for Bridge kinds and complete mappings from the providers' results."""
        result = drain_channels(
            session, providers, clock, workspace_id, batch=batch, max_attempts=max_attempts,
            kinds_prefix=("telegram.", "mattermost."),
        )
        now = clock.now()
        for provider in providers.values():
            delivered = getattr(provider, "delivered", None)
            if isinstance(delivered, dict):
                for key, dest_id in delivered.items():
                    self.record_delivered(session, str(key), str(dest_id), now)
        failed_rows = session.execute(
            text(
                "SELECT o.outbox_id, o.dedupe_key, o.last_error, m.bridge_id, o.payload FROM delivery_outbox o "
                "JOIN message_mappings m ON m.dedupe_key = o.dedupe_key WHERE o.status = 'dead' "
                "AND m.delivery_status <> 'dead' AND o.workspace_id = :ws"
            ),
            {"ws": uuid.UUID(workspace_id)},
        ).all()
        for outbox_id, dedupe_key, last_error, bridge_id, payload in failed_rows:
            self._dead_letter(session, clock, workspace_id, bridge_id, outbox_id, dedupe_key, last_error, payload)
        return {"sent": result.sent, "failed": result.failed, "dead": result.dead, **self.metrics.snapshot()}

    def _dead_letter(
        self,
        session: Session,
        clock: Clock,
        workspace_id: str,
        bridge_id: str,
        outbox_id: str,
        dedupe_key: str,
        last_error: str | None,
        payload: Any,
    ) -> None:
        event_id: str | None = None
        if self.store is not None and self.service_account_uuid is not None:
            res = self.store.append(
                AppendRequest(
                    workspace_id=workspace_id,
                    aggregate_type="bridge",
                    aggregate_id=f"bridge-{bridge_id}" if not bridge_id.startswith("bridge-") else bridge_id,
                    type="BRIDGE_DELIVERY_FAILED",
                    actor_account_id=self.service_account_uuid,
                    correlation_id=f"bridge-dead-{dedupe_key[:16]}",
                    idempotency_scope="bridge:delivery_failed",
                    idempotency_key=dedupe_key,
                    payload={"bridge_id": bridge_id, "outbox_id": outbox_id, "error_code": "DELIVERY_DEAD"},
                )
            )
            event_id = res.event_id
        data = payload if isinstance(payload, dict) else json.loads(payload)
        session.execute(
            text(
                "INSERT INTO bridge_dead_letters (workspace_id, bridge_id, dedupe_key, outbox_id, reason, "
                "payload, event_id, created_at) VALUES (:ws, :b, :k, :o, :r, CAST(:p AS jsonb), :e, :now) "
                "ON CONFLICT (dedupe_key) DO NOTHING"
            ),
            {
                "ws": uuid.UUID(workspace_id), "b": bridge_id, "k": dedupe_key, "o": outbox_id,
                "r": (last_error or "delivery failed")[:200], "p": json.dumps(data), "e": event_id,
                "now": clock.now(),
            },
        )
        session.execute(
            text("UPDATE message_mappings SET delivery_status = 'dead' WHERE dedupe_key = :k"), {"k": dedupe_key}
        )
        self.metrics.dead_lettered += 1

    def replay_dead_letters(self, session: Session, clock: Clock, workspace_id: str, bridge_id: str | None = None) -> int:
        """Re-enqueue dead letters exactly once each (a second replay is a no-op)."""
        rows = session.execute(
            text(
                "SELECT dedupe_key FROM bridge_dead_letters WHERE workspace_id = :ws AND replayed_at IS NULL "
                + ("AND bridge_id = :b " if bridge_id else "")
                + "ORDER BY id FOR UPDATE SKIP LOCKED"
            ),
            {"ws": uuid.UUID(workspace_id), "b": bridge_id},
        ).all()
        now = clock.now()
        replayed = 0
        for (dedupe_key,) in rows:
            session.execute(
                text(
                    "UPDATE delivery_outbox SET status = 'pending', attempts = 0, next_attempt_at = :now, "
                    "last_error = NULL WHERE dedupe_key = :k AND status = 'dead'"
                ),
                {"now": now, "k": dedupe_key},
            )
            session.execute(
                text("UPDATE message_mappings SET delivery_status = 'pending' WHERE dedupe_key = :k"),
                {"k": dedupe_key},
            )
            session.execute(
                text("UPDATE bridge_dead_letters SET replayed_at = :now WHERE dedupe_key = :k"),
                {"now": now, "k": dedupe_key},
            )
            replayed += 1
        self.metrics.replayed += replayed
        return replayed


# ----------------------------------------------------------------------------- providers
@dataclass
class TelegramBridgeProvider:
    """``ChannelProvider`` for ``telegram.send`` Bridge deliveries: creates the forum topic when the
    target asks for it, sends the text, and returns ``"<thread_id>:<message_id>"``. Idempotent per
    dedupe key (a repeated delivery returns the first result without a second Bot API call)."""

    client: TelegramClient
    delivered: dict[str, str] = field(default_factory=dict)
    topics: dict[str, int] = field(default_factory=dict)  # dedupe key -> created thread id

    def deliver(self, destination: str, payload: dict[str, Any]) -> str:
        key = str(payload["dedupe_key"])
        if key in self.delivered:
            return self.delivered[key]
        chat_id = destination.split(":")[1]
        thread = payload.get("message_thread_id")
        topic_name = payload.get("create_topic")
        if topic_name and thread is None:
            if key not in self.topics:
                self.topics[key] = self.client.create_forum_topic(chat_id, str(topic_name)).message_thread_id
            thread = self.topics[key]
        sent = self.client.send_message(
            chat_id,
            str(payload["text"]),
            message_thread_id=int(thread) if thread is not None else None,
            reply_to_message_id=payload.get("reply_to_message_id"),
        )
        result = f"{thread if thread is not None else ''}:{sent.message_id}"
        self.delivered[key] = result
        return result


@dataclass
class MattermostBridgeProvider:
    """``ChannelProvider`` for ``mattermost.post`` deliveries over any client exposing
    ``create_post(channel_id, message, root_id=None, props=None)`` returning a post with ``id``."""

    client: Any
    delivered: dict[str, str] = field(default_factory=dict)

    def deliver(self, destination: str, payload: dict[str, Any]) -> str:
        key = str(payload["dedupe_key"])
        if key in self.delivered:
            return self.delivered[key]
        channel = destination.split(":", 1)[1]
        post = self.client.create_post(
            channel, str(payload["message"]), root_id=payload.get("root_id"), props=payload.get("props")
        )
        post_id = str(post["id"] if isinstance(post, dict) else getattr(post, "id", post))
        self.delivered[key] = post_id
        return post_id
