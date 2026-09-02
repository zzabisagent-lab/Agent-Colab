"""Channel gateway wiring (Phase 2): connects the Mattermost WebSocket stream, the Telegram
intake, the Bridge, the Renderer outbox, and the notification providers to the running app.

Everything here is glue over the package APIs; the gateway never interprets free text as a
command (development plan §3.1). Providers are created from environment/Secret references:
``AGENT_COLAB_MATTERMOST_URL`` + ``AGENT_COLAB_MATTERMOST_BOT_TOKEN`` and ``TELEGRAM_BOT_TOKEN``.
Without them the gateway runs with no providers (deliveries stay pending in the outbox).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime
from server.channels.outbox import ChannelProvider
from server.channels.telegram.bridge import Bridge, MattermostPostView
from server.channels.telegram.intake import InboundMessage
from server.db.engine import session_scope
from server.domain.clock import Clock

log = logging.getLogger("agent_colab.gateway")

InboundHook = Callable[[Session, InboundMessage], bool]


@dataclass
class ChannelGateway:
    runtime: Runtime
    clock: Clock
    bridge: Bridge = field(default_factory=Bridge)
    providers: dict[str, ChannelProvider] = field(default_factory=dict)
    inbound_hooks: list[InboundHook] = field(default_factory=list)  # e.g. Telegram commands
    drain_interval_s: float = 1.0
    _task: asyncio.Task[None] | None = None
    drains: int = 0

    # ---- inbound ----------------------------------------------------------------------------
    def on_telegram_message(self, msg: InboundMessage) -> None:
        """Telegram webhook/polling handler: command gateways first, then the Bridge relay."""
        with session_scope(self.runtime.session_factory) as session:
            for hook in self.inbound_hooks:
                if hook(session, msg):
                    return
            self.bridge.on_telegram_message(session, self.clock, msg)

    def on_mattermost_event(self, event: dict[str, Any]) -> None:
        """Normalized WebSocket event (``posted``): relay through the Bridge; edits are ignored."""
        if event.get("event") != "posted" or not event.get("post"):
            return
        post = event["post"]
        with session_scope(self.runtime.session_factory) as session:
            instance_id = self._instance_for_channel(session, str(post.get("channel_id", "")))
            if instance_id is None:
                return
            props = post.get("props") or {}
            view = MattermostPostView(
                provider_instance_id=instance_id,
                channel_ext_id=str(post["channel_id"]),
                post_id=str(post["id"]),
                root_id=post.get("root_id") or None,
                user_id=str(post.get("user_id", "")),
                user_label=str(
                    props.get("override_username")
                    or event.get("data", {}).get("sender_name")
                    or post.get("user_id", "")
                ),
                message=str(post.get("message", "")),
                props=dict(props),
                user_is_bot=bool(props.get("from_bot") in ("true", True)),
            )
            self.bridge.on_mattermost_post(session, self.clock, view)

    def _instance_for_channel(self, session: Session, external_channel_id: str) -> str | None:
        row = session.execute(
            text(
                "SELECT p.provider_instance_id FROM channels c "
                "JOIN provider_instances p ON p.id = c.provider_instance_id "
                "WHERE c.external_channel_id = :e AND c.status = 'active'"
            ),
            {"e": external_channel_id},
        ).first()
        return None if row is None else str(row[0])

    # ---- outbound ---------------------------------------------------------------------------
    def drain_once(self) -> dict[str, int]:
        if not self.providers:
            return {}
        with session_scope(self.runtime.session_factory) as session:
            ws = self.runtime.resolve_workspace(session)
            result = self.bridge.deliver(session, self.providers, self.clock, ws)
        self.drains += 1
        return result

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.drain_once)
            except Exception:
                log.exception("gateway drain failed")
            await asyncio.sleep(self.drain_interval_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def providers_from_env() -> dict[str, ChannelProvider]:
    """Build outbox providers from configured credentials; missing credentials → no provider."""
    providers: dict[str, ChannelProvider] = {}
    mm_url = os.environ.get("AGENT_COLAB_MATTERMOST_URL")
    mm_token = os.environ.get("AGENT_COLAB_MATTERMOST_BOT_TOKEN")
    if mm_url and mm_token:
        from server.channels.mattermost.client import HttpMattermostClient
        from server.channels.telegram.bridge import MattermostBridgeProvider

        providers["mattermost"] = MattermostBridgeProvider(HttpMattermostClient(mm_url, mm_token))
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tg_token:
        from server.channels.telegram.bridge import TelegramBridgeProvider
        from server.channels.telegram.client import HttpTelegramClient

        providers["telegram"] = TelegramBridgeProvider(HttpTelegramClient(tg_token))
    return providers


def build_gateway(runtime: Runtime) -> ChannelGateway:
    return ChannelGateway(runtime=runtime, clock=runtime.clock, providers=providers_from_env())
