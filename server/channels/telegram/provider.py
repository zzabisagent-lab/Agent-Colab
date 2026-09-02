"""Telegram provider instance identity and the outbox delivery provider (P2-04).

Provider instance id = ``tg:<bot id>`` (spec §10.2: the Telegram provider instance is the bot).
``TelegramNotificationProvider`` implements the P1-13 outbox ``Provider`` for destinations
``telegram:<chat_id>`` and ``telegram:<chat_id>:<message_thread_id>``; deliveries are idempotent
per dedupe key (a repeated send with the same key returns the first message id without a second
Bot API call).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from server.channels.telegram.client import HttpTelegramClient, SentMessage, TelegramClient

_DEST = re.compile(r"^telegram:(-?\d+)(?::(\d+))?$")


def provider_instance_id(bot_id: str) -> str:
    return f"tg:{bot_id}"


def parse_destination(destination: str) -> tuple[str, int | None]:
    m = _DEST.match(destination)
    if not m:
        raise ValueError(f"unsupported telegram destination: {destination}")
    return m.group(1), int(m.group(2)) if m.group(2) else None


def client_from_env(clock: Any = None) -> HttpTelegramClient | None:
    """Build the HTTP client from ``TELEGRAM_BOT_TOKEN`` (Secret reference resolved from the
    environment in Phase 2; the Secret Broker takes over in Phase 4). None when unset."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    return HttpTelegramClient(token, clock=clock)


@dataclass
class TelegramNotificationProvider:
    client: TelegramClient
    delivered: dict[str, int] = field(default_factory=dict)  # dedupe key -> message id
    _bot_id: str | None = None

    @property
    def instance_id(self) -> str:
        if self._bot_id is None:
            self._bot_id = str(self.client.get_me()["id"])
        return provider_instance_id(self._bot_id)

    def send(self, destination: str, payload: dict[str, Any]) -> None:
        """Outbox contract: raise on failure; idempotent per ``payload['dedupe_key']``."""
        self.deliver(destination, payload)

    def deliver(self, destination: str, payload: dict[str, Any]) -> SentMessage | int:
        dedupe_key = str(payload.get("dedupe_key") or payload.get("outbox_id") or "")
        if dedupe_key and dedupe_key in self.delivered:
            return self.delivered[dedupe_key]
        chat_id, thread = parse_destination(destination)
        text = str(payload.get("text") or payload.get("body") or "")
        if not text:
            raise ValueError("telegram payload requires text")
        sent = self.client.send_message(
            chat_id,
            text,
            message_thread_id=thread if thread is not None else payload.get("message_thread_id"),
            reply_to_message_id=payload.get("reply_to_message_id"),
        )
        if dedupe_key:
            self.delivered[dedupe_key] = sent.message_id
        return sent
