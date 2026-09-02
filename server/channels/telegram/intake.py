"""Telegram update intake and normalization (P2-04).

Validated updates (webhook secret / replay / staleness checked by the API layer, or read by the
polling loop) are normalized into ``InboundMessage`` per the P0-13 spike shapes and handed to a
handler. The intake never creates domain Events itself; the Bridge (P2-05) does.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from server.channels.telegram.client import TelegramClient

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "api" / "telegram"
UPDATE_SCHEMA = SCHEMA_PATH / "webhook-update.v1.schema.json"
GENERAL_TOPIC_THREAD_ID: int | None = None  # spec: General topic = thread id omitted (never 1)


class IntakeError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class InboundAttachment:
    kind: str  # document | photo | video | audio | voice | animation | sticker
    file_id: str
    file_name: str | None
    mime_type: str | None
    file_size: int | None


@dataclass(frozen=True)
class InboundMessage:
    provider_instance_id: str
    update_id: int
    chat_id: str
    message_id: int
    date: int
    message_thread_id: int | None
    reply_to_message_id: int | None
    from_user_id: str | None
    from_is_bot: bool
    from_display_name: str
    text: str
    attachments: tuple[InboundAttachment, ...] = ()
    is_topic_message: bool = False
    forum_topic_created: str | None = None  # topic name when this is the service message
    edited: bool = False
    raw_kind: str = "message"


InboundHandler = Callable[[InboundMessage], None]


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(UPDATE_SCHEMA.read_text(encoding="utf-8")))


def validate_update(update: dict[str, Any]) -> None:
    errors = sorted(_validator().iter_errors(update), key=lambda e: list(e.path))
    if errors:
        path = "/".join(str(p) for p in errors[0].path) or "<root>"
        raise IntakeError("TELEGRAM_UPDATE_INVALID", f"{path}: {errors[0].message}")


def _attachments(msg: dict[str, Any]) -> tuple[InboundAttachment, ...]:
    out: list[InboundAttachment] = []
    for kind in ("document", "video", "audio", "voice", "animation", "sticker"):
        obj = msg.get(kind)
        if isinstance(obj, dict):
            out.append(
                InboundAttachment(
                    kind,
                    str(obj.get("file_id")),
                    obj.get("file_name"),
                    obj.get("mime_type"),
                    obj.get("file_size"),
                )
            )
    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        best = max(photos, key=lambda p: int(p.get("file_size") or 0))
        out.append(
            InboundAttachment(
                "photo", str(best.get("file_id")), None, "image/jpeg", best.get("file_size")
            )
        )
    return tuple(out)


def normalize_update(provider_instance_id: str, update: dict[str, Any]) -> InboundMessage | None:
    """Return the normalized message, or None for update kinds the Bridge does not relay."""
    validate_update(update)
    kind = next((k for k in ("message", "edited_message") if k in update), None)
    if kind is None:
        return None
    msg = update[kind]
    sender = msg.get("from") or {}
    name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or (
        sender.get("username") or "unknown"
    )
    reply = msg.get("reply_to_message") or {}
    topic = msg.get("forum_topic_created")
    return InboundMessage(
        provider_instance_id=provider_instance_id,
        update_id=int(update["update_id"]),
        chat_id=str(msg["chat"]["id"]),
        message_id=int(msg["message_id"]),
        date=int(msg.get("date", 0)),
        message_thread_id=msg.get("message_thread_id"),
        reply_to_message_id=reply.get("message_id"),
        from_user_id=str(sender["id"]) if "id" in sender else None,
        from_is_bot=bool(sender.get("is_bot", False)),
        from_display_name=name,
        text=str(msg.get("text") or msg.get("caption") or ""),
        attachments=_attachments(msg),
        is_topic_message=bool(msg.get("is_topic_message", False)),
        forum_topic_created=str(topic["name"]) if isinstance(topic, dict) else None,
        edited=kind == "edited_message",
        raw_kind=kind,
    )


class OffsetStore(Protocol):
    def load(self, provider_instance_id: str) -> int | None: ...

    def save(self, provider_instance_id: str, offset: int) -> None: ...


@dataclass
class MemoryOffsetStore:
    offsets: dict[str, int] = field(default_factory=dict)

    def load(self, provider_instance_id: str) -> int | None:
        return self.offsets.get(provider_instance_id)

    def save(self, provider_instance_id: str, offset: int) -> None:
        self.offsets[provider_instance_id] = offset


def poll_updates(
    client: TelegramClient,
    provider_instance_id: str,
    handler: InboundHandler,
    offset_store: OffsetStore,
    *,
    timeout: int = 25,
    max_rounds: int | None = None,
) -> Iterator[int]:
    """Long-polling alternative to the webhook: yields each handled update_id.

    The offset (last update_id + 1) is persisted after each update so a restart never re-handles
    an update; ``max_rounds`` bounds the loop for tests/jobs.
    """
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        rounds += 1
        offset = offset_store.load(provider_instance_id)
        batch = sorted(client.get_updates(offset, timeout), key=lambda u: int(u["update_id"]))
        for update in batch:
            update_id = int(update["update_id"])
            inbound = normalize_update(provider_instance_id, update)
            if inbound is not None:
                handler(inbound)
            offset_store.save(provider_instance_id, update_id + 1)
            yield update_id
