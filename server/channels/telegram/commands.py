"""Telegram command gateway (P2-08, development plan §7A.6, spec §10.2).

Runs **before** the Bridge relay on every inbound Telegram message:

1. detect a ``/colab …`` (``/colab@<bot> …``, ``@colab …``) command; anything else is not handled;
2. resolve the Bridge of ``(provider instance, chat[, topic])`` → the bound Mattermost channel;
3. apply the Bridge's :mod:`server.channels.policy` — read/reply only by default: the user gets a
   read-only notice at most once per hour and **nothing executes**;
4. resolve the principal from the Telegram user's ExternalIdentityLink (provider instance = bot
   token id): unlinked users get link guidance, suspended/revoked links a stable
   ``EXTERNAL_IDENTITY_NOT_ACTIVE`` reply — zero Task/Event side effects either way;
5. parse the §7A.2 grammar, check the verb against the policy, and execute through the Command
   Router's mapping with the *explicit* principal (Account permissions apply on the command bus);
6. reply through the transactional channel outbox (``telegram.send`` to the same chat/topic).

Everything runs inside the caller's session/transaction, so a failure after the Event append
rolls back the reply with it (§10.2 outbox contract).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime
from server.api.errors import ApiError
from server.application import bus
from server.channels import commands as grammar
from server.channels import policy as tg_policy
from server.channels.mattermost import provider as prov
from server.channels.outbox import Delivery, enqueue_delivery
from server.channels.router import (
    _LANGUAGE,
    CommandResponse,
    Router,
    SlashRequest,
    ephemeral,
    language_for_channel,
    render,
)
from server.channels.telegram.bridge import BridgeRow, bridges_for_telegram_chat
from server.channels.telegram.intake import InboundMessage
from server.domain.clock import Clock, SystemClock
from server.identity.external_links import sql_service
from server.identity.principals import IdentityError, Principal

NOT_A_COMMAND = "NOT_A_COMMAND"
BRIDGE_NOT_FOUND = "BRIDGE_NOT_FOUND"
TELEGRAM_USER_NOT_LINKED = "TELEGRAM_USER_NOT_LINKED"
EXTERNAL_IDENTITY_NOT_ACTIVE = "EXTERNAL_IDENTITY_NOT_ACTIVE"
COMMAND_KIND = "telegram.send"

# English reference wording; localized copies live under the same keys in i18n bundles.
MESSAGES: dict[str, str] = {
    "telegram.read_only": (
        "This Telegram chat is read/reply only: Agent-Colab commands are not executed here. "
        "Use the linked Mattermost channel (or ask an administrator to enable commands on the "
        "Bridge)."
    ),
    "telegram.verb_not_allowed": (
        "`{command}` is not allowed from Telegram on this Bridge; allowed here: {allowed}."
    ),
    "telegram.not_linked": (
        "Your Telegram user is not linked to an Agent-Colab Account. Run `/colab link start` "
        "in the Mattermost channel and confirm the challenge code to link it."
    ),
    "telegram.identity_not_active": (
        "Your Agent-Colab link is not active ({state}); commands from Telegram are not executed."
    ),
}
_BOT_SUFFIX = re.compile(r"^(/colab)@[A-Za-z0-9_]+(?=\s|$)")


@dataclass(frozen=True)
class TelegramCommandResult:
    """Outcome of one inbound message.

    ``handled`` is True when the message was a Colab command addressed to this Bridge (the
    caller should *not* relay it as chat); ``response_text`` is the reply enqueued to Telegram
    (None when the read-only notice was suppressed by the hourly throttle); ``event_id`` is set
    only when a command produced a domain Event.
    """

    handled: bool
    response_text: str | None = None
    code: str = "OK"
    event_id: str | None = None
    resource_id: str | None = None
    bridge_id: str | None = None
    throttled: bool = False


class CommandExecutor(Protocol):
    """Extension point: run a parsed command with an explicit principal (Router mapping)."""

    def __call__(
        self,
        session: Session,
        bridge: BridgeRow,
        msg: InboundMessage,
        principal: Principal,
        parsed: grammar.ParsedCommand,
        command_text: str,
    ) -> CommandResponse: ...


def normalize_command_text(text_body: str | None) -> str | None:
    """``/colab@bot task show t-1`` → ``/colab task show t-1``; None when not a Colab command."""
    if not text_body:
        return None
    stripped = _BOT_SUFFIX.sub(r"\1", text_body.strip(), count=1)
    return stripped if grammar.strip_prefix(stripped) is not None else None


def resolve_verb(command_text: str) -> tuple[str, str] | None:
    """``(resource, verb)`` named by the command's first two tokens when the grammar knows them.

    Lets the Bridge policy refuse a verb *before* argument validation, so a refused verb never
    leaks grammar details and never touches the command bus.
    """
    tokens = grammar.strip_prefix(command_text) or []
    if len(tokens) < 2:
        return None
    pair = (tokens[0].lower(), tokens[1].lower())
    return pair if any((v.resource, v.verb) == pair for v in grammar.VERBS) else None


def command_seed(msg: InboundMessage) -> str:
    """Deterministic per-message seed: ``tg:<provider_instance>:<chat>:<message_id>``."""
    return f"tg:{msg.provider_instance_id}:{msg.chat_id}:{msg.message_id}"


def reply_destination(msg: InboundMessage) -> str:
    dest = f"telegram:{msg.chat_id}"
    return f"{dest}:{msg.message_thread_id}" if msg.message_thread_id is not None else dest


def select_bridge(session: Session, msg: InboundMessage) -> BridgeRow | None:
    """The enabled Bridge for the message's chat/topic (exact topic match preferred)."""
    rows = bridges_for_telegram_chat(
        session, msg.provider_instance_id, msg.chat_id, enabled_only=True
    )
    candidates = [
        b
        for b in rows
        if not (b.thread_mode == "fixed_topic" and b.telegram_thread_id != msg.message_thread_id)
    ]
    if not candidates:
        return None
    exact = [b for b in candidates if b.telegram_thread_id == msg.message_thread_id]
    return (exact or candidates)[0]


def message_text(key: str, **fields: Any) -> str:
    """Localized (current request language) wording with the English reference as fallback."""
    rendered = render(key, **fields)
    if rendered == key:
        template = MESSAGES.get(key, key)
        try:
            return template.format(**fields)
        except (KeyError, IndexError):
            return template
    return rendered


class TelegramCommandGateway:
    def __init__(
        self,
        runtime: Runtime,
        clock: Clock | None = None,
        *,
        router: Router | None = None,
        executor: CommandExecutor | None = None,
        principal_resolver: Callable[[Session, InboundMessage], Principal] | None = None,
    ) -> None:
        self._runtime = runtime
        self._clock = clock or SystemClock()
        self._router = router or Router(runtime, self._clock)
        self._executor: CommandExecutor = executor or self.execute_with_router
        self._resolve_principal = principal_resolver or self._default_principal

    # -- public entry -------------------------------------------------------------------------
    def handle(self, session: Session, msg: InboundMessage) -> TelegramCommandResult:
        command_text = normalize_command_text(msg.text)
        if command_text is None or msg.from_is_bot or not msg.from_user_id:
            return TelegramCommandResult(False, code=NOT_A_COMMAND)
        bridge = select_bridge(session, msg)
        if bridge is None:
            return TelegramCommandResult(False, code=BRIDGE_NOT_FOUND)
        _LANGUAGE.set(self._language(session, bridge))
        policy = tg_policy.TelegramCommandPolicy.from_bridge(
            bridge.allow_commands, bridge.content_policy
        )
        if not policy.allow_commands:
            return self._read_only_notice(session, bridge, msg)

        try:
            principal = self._resolve_principal(session, msg)
        except IdentityError as exc:
            return self._identity_reply(session, bridge, msg, exc)

        named = resolve_verb(command_text)
        if named is not None:
            early = tg_policy.evaluate(policy, *named)
            if early.denied:
                return self._verb_denied(session, bridge, msg, policy, early.code, named)

        thread_kind, thread_id = self._thread_context(session, bridge, msg)
        try:
            parsed = grammar.parse_command(
                command_text,
                grammar.CommandContext(
                    linked=True, thread_subject_kind=thread_kind, thread_subject_id=thread_id
                ),
            )
        except grammar.CommandError as exc:
            response = ephemeral(exc.message_key, exc.code, exc.detail, exc.example)
            return self._reply(session, bridge, msg, response, throttle=False)

        decision = tg_policy.evaluate(policy, parsed.resource, parsed.verb)
        if decision.denied:  # defence in depth: the pre-parse check already refused known verbs
            return self._verb_denied(
                session, bridge, msg, policy, decision.code, (parsed.resource, parsed.verb)
            )

        try:
            response = self._executor(session, bridge, msg, principal, parsed, command_text)
        except bus.CommandError as exc:
            response = ephemeral("command.error", exc.code, exc.detail)
        except ApiError as exc:
            response = ephemeral("command.error", exc.code, exc.detail)
        return self._reply(session, bridge, msg, response, throttle=False)

    # -- extension point: Router mapping with an explicit principal ---------------------------
    def execute_with_router(
        self,
        session: Session,
        bridge: BridgeRow,
        msg: InboundMessage,
        principal: Principal,
        parsed: grammar.ParsedCommand,
        command_text: str,
    ) -> CommandResponse:
        """Run ``parsed`` through the Command Router's ``<resource> <verb>`` mapping.

        The request is shaped as a slash command in the Bridge's Mattermost channel so the
        Router's handlers (read verbs and, when the policy opens them, write verbs) execute
        exactly as they do for Mattermost, with the Telegram user's Account as the principal.
        The idempotency key derives from ``tg:<provider_instance>:<chat>:<message_id>``.
        """
        inst = prov.load_instance(session, bridge.mm_provider_instance_id)
        if inst is None:
            raise bus.CommandError(
                "PROVIDER_INSTANCE_UNKNOWN", bridge.mm_provider_instance_id, status=404
            )
        root = self._mm_root_post(session, bridge, msg)
        req = SlashRequest(
            provider_instance_id=inst.provider_instance_id,
            team_id=inst.team_id,
            channel_id=bridge.channel_ext_id,
            user_id=f"telegram:{msg.from_user_id}",
            user_name=msg.from_display_name or str(msg.from_user_id),
            command="/colab",
            text=command_text.split(None, 1)[1] if " " in command_text.strip() else "",
            trigger_id=command_seed(msg),
            post_id=None,
            root_id=root,
        )
        return self._router._execute(session, inst, req, principal, parsed)

    # -- helpers ------------------------------------------------------------------------------
    def _default_principal(self, session: Session, msg: InboundMessage) -> Principal:
        service = sql_service(session, self._runtime.store_for(session), self._clock)
        return service.resolve_command_principal(msg.provider_instance_id, str(msg.from_user_id))

    def _language(self, session: Session, bridge: BridgeRow) -> str:
        inst = prov.load_instance(session, bridge.mm_provider_instance_id)
        if inst is None:
            return "en"
        return language_for_channel(session, inst.id, bridge.channel_ext_id)

    def _thread_context(
        self, session: Session, bridge: BridgeRow, msg: InboundMessage
    ) -> tuple[str | None, str | None]:
        root = self._mm_root_post(session, bridge, msg)
        if root is None:
            return None, None
        inst = prov.load_instance(session, bridge.mm_provider_instance_id)
        if inst is None:
            return None, None
        binding = prov.binding_for_post(session, inst.id, root)
        if binding is None:
            return None, None
        return binding.subject_type, binding.subject_id

    @staticmethod
    def _mm_root_post(session: Session, bridge: BridgeRow, msg: InboundMessage) -> str | None:
        """Mattermost root post of the Telegram topic (via message mappings), when any."""
        if msg.message_thread_id is None:
            return None
        row = session.execute(
            text(
                "SELECT COALESCE(mm_root_post_id, mm_post_id) FROM message_mappings "
                "WHERE bridge_id = :b AND tg_thread_id = :t AND mm_post_id IS NOT NULL "
                "ORDER BY id LIMIT 1"
            ),
            {"b": bridge.bridge_id, "t": int(msg.message_thread_id)},
        ).first()
        return None if row is None or row[0] is None else str(row[0])

    def _read_only_notice(
        self, session: Session, bridge: BridgeRow, msg: InboundMessage
    ) -> TelegramCommandResult:
        now = self._clock.now()
        key = tg_policy.notice_dedupe_key(
            msg.provider_instance_id, msg.chat_id, str(msg.from_user_id), now
        )
        outbox_id = enqueue_delivery(
            session,
            workspace_id=bridge.workspace_id,
            source_event_id=None,
            delivery=Delivery(
                kind=COMMAND_KIND,
                destination=reply_destination(msg),
                payload=self._payload(msg, message_text("telegram.read_only")),
                dedupe_key=key,
            ),
            provider_instance_id=bridge.provider_instance_id,
            external_channel_id=bridge.channel_ext_id,
            now=now,
        )
        throttled = outbox_id is None
        return TelegramCommandResult(
            True,
            None if throttled else message_text("telegram.read_only"),
            tg_policy.TELEGRAM_COMMANDS_DISABLED,
            bridge_id=bridge.bridge_id,
            throttled=throttled,
        )

    def _verb_denied(
        self,
        session: Session,
        bridge: BridgeRow,
        msg: InboundMessage,
        policy: tg_policy.TelegramCommandPolicy,
        code: str,
        named: tuple[str, str],
    ) -> TelegramCommandResult:
        response = CommandResponse(
            "ephemeral",
            message_text(
                "telegram.verb_not_allowed",
                command=" ".join(named),
                allowed=", ".join(sorted(policy.allowed_verbs)),
            ),
            code,
        )
        return self._reply(session, bridge, msg, response, throttle=False)

    def _identity_reply(
        self, session: Session, bridge: BridgeRow, msg: InboundMessage, exc: IdentityError
    ) -> TelegramCommandResult:
        if exc.detail == "no active link":
            response = CommandResponse(
                "ephemeral", message_text("telegram.not_linked"), TELEGRAM_USER_NOT_LINKED
            )
        else:
            response = CommandResponse(
                "ephemeral",
                message_text("telegram.identity_not_active", state=exc.detail),
                EXTERNAL_IDENTITY_NOT_ACTIVE,
            )
        return self._reply(session, bridge, msg, response, throttle=False)

    def _reply(
        self,
        session: Session,
        bridge: BridgeRow,
        msg: InboundMessage,
        response: CommandResponse,
        *,
        throttle: bool,
    ) -> TelegramCommandResult:
        now = self._clock.now()
        seed = command_seed(msg)
        key = f"tg-cmd:{hashlib.sha256(seed.encode()).hexdigest()[:40]}"
        enqueue_delivery(
            session,
            workspace_id=bridge.workspace_id,
            source_event_id=response.event_id,
            delivery=Delivery(
                kind=COMMAND_KIND,
                destination=reply_destination(msg),
                payload=self._payload(msg, response.text, code=response.code),
                dedupe_key=key,
            ),
            provider_instance_id=bridge.provider_instance_id,
            external_channel_id=bridge.channel_ext_id,
            now=now,
        )
        return TelegramCommandResult(
            True,
            response.text,
            response.code,
            response.event_id,
            response.resource_id,
            bridge.bridge_id,
            throttled=throttle,
        )

    @staticmethod
    def _payload(msg: InboundMessage, text_body: str, **extra: Any) -> dict[str, Any]:
        return {
            "text": text_body,
            "chat_id": msg.chat_id,
            "message_thread_id": msg.message_thread_id,
            "reply_to_message_id": msg.message_id,
            "source": "telegram_command",
            **extra,
        }
