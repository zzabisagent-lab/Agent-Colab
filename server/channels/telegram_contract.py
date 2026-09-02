"""Telegram Bridge thread-mapping contract (P0-13, fixed for P2-04/P2-05/P2-06).

Rules are derived from spec §10, development plan §6.5, and the P0-13 spike observations recorded
in ``evidence/phase-0/spikes/telegram/summary.json``; ``tests/unit/test_telegram_contract.py``
cross-checks the constants below against that summary so the contract can never contradict the
observed Bot API behaviour (V-P0-19).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

# --- Bot API facts confirmed by the spike -------------------------------------------------------
GENERAL_TOPIC_THREAD_ID: int | None = None
"""Messages in the General topic carry no ``message_thread_id``; ``sendMessage`` with
``message_thread_id=1`` is rejected (``Bad Request: message thread not found``)."""

TOPIC_THREAD_ID_IS_SERVICE_MESSAGE_ID = True
"""A forum topic's ``message_thread_id`` equals the ``message_id`` of its ``forum_topic_created``
service message; replying to that id is allowed and keeps the thread."""

REPLY_TARGET_THREAD_FROM_PARAMETER = True
"""The destination topic is the ``message_thread_id`` parameter, not the replied message's topic."""

EDIT_OWN_MESSAGES_ONLY = True
EDIT_WINDOW_HOURS = 48  # Bot API documented limit for editing own messages (not spike-testable)

# Rate budget per chat derived from the spike burst (16 accepted of 40 in 24 s, 429 retry_after
# 31-33 s after ~16 messages in ~22 s) and Telegram's documented limits (~1 msg/s per chat,
# 20 msg/min per group): a token bucket of 20 per 60 s with a sustained rate of 1 msg/s.
RATE_BUCKET_CAPACITY = 20
RATE_BUCKET_WINDOW_S = 60
RATE_SUSTAINED_PER_S = 1.0
RATE_MAX_CONCURRENT_PER_CHAT = 1
RETRY_AFTER_HONOURED = True
RETRY_AFTER_CAP_S = 120
MAX_HOPS = 1


class Platform(StrEnum):
    MATTERMOST = "mattermost"
    TELEGRAM = "telegram"


class Direction(StrEnum):
    MM_TO_TG = "mattermost_to_telegram"
    TG_TO_MM = "telegram_to_mattermost"
    BIDIRECTIONAL = "bidirectional"


class BridgeError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def mapping_key(bridge_id: str, platform: Platform | str, message_id: str | int) -> str:
    """Unique dedupe key of a relayed message: ``(bridge_id, source_platform, source_id)``."""
    raw = f"{bridge_id}|{Platform(platform)}|{message_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def display_prefix(sender_name: str, source: Platform | str) -> str:
    """Identity display of a relayed message (spec §10.2): original sender and source."""
    via = "Telegram" if Platform(source) is Platform.TELEGRAM else "Mattermost"
    return f"[{sender_name} via {via}]"


@dataclass(frozen=True)
class Mapping:
    bridge_id: str
    source_platform: Platform
    source_message_id: str
    origin_platform: Platform
    origin_message_id: str
    hop_count: int
    mm_channel_id: str
    mm_post_id: str
    mm_root_id: str | None
    tg_chat_id: str
    tg_message_id: int
    tg_thread_id: int | None
    tg_reply_to_message_id: int | None


@dataclass(frozen=True)
class BridgeConfig:
    bridge_id: str
    mm_channel_id: str
    tg_chat_id: str
    direction: Direction
    tg_thread_mode: str = "topic_per_root"  # topic_per_root | general | fixed_topic
    tg_fixed_thread_id: int | None = None
    enabled: bool = True


@dataclass(frozen=True)
class InboundEvent:
    """A message observed on one platform that may need relaying to the other."""

    platform: Platform
    message_id: str
    sender_name: str
    hop_count: int = 0
    origin_platform: Platform | None = None
    origin_message_id: str | None = None
    # mattermost
    mm_channel_id: str | None = None
    mm_root_id: str | None = None
    # telegram
    tg_chat_id: str | None = None
    tg_thread_id: int | None = None
    tg_reply_to_message_id: int | None = None
    is_bridge_bot: bool = False


@dataclass(frozen=True)
class Target:
    platform: Platform
    # telegram target
    tg_chat_id: str | None = None
    tg_thread_id: int | None = None
    tg_reply_to_message_id: int | None = None
    create_topic: bool = False
    # mattermost target
    mm_channel_id: str | None = None
    mm_root_id: str | None = None
    display_prefix: str = ""
    origin_platform: Platform | None = None
    origin_message_id: str | None = None
    hop_count: int = 1


def check_loop(event: InboundEvent) -> None:
    """Echo/loop prevention: relayed messages (hop ≥ 1), bot-authored posts, and origin marker."""
    if event.is_bridge_bot:
        raise BridgeError("BRIDGE_LOOP_DETECTED", "message authored by the bridge bot itself")
    if event.hop_count >= MAX_HOPS:
        raise BridgeError("BRIDGE_LOOP_DETECTED", f"hop_count {event.hop_count} >= {MAX_HOPS}")
    if event.origin_platform is not None and event.origin_platform != event.platform:
        raise BridgeError("BRIDGE_LOOP_DETECTED", "origin marker points to the other platform")


def check_direction(config: BridgeConfig, event: InboundEvent) -> None:
    if not config.enabled:
        raise BridgeError("BRIDGE_DIRECTION_DENIED", "bridge disabled")
    wanted = Direction.MM_TO_TG if event.platform is Platform.MATTERMOST else Direction.TG_TO_MM
    if config.direction not in (Direction.BIDIRECTIONAL, wanted):
        raise BridgeError("BRIDGE_DIRECTION_DENIED", f"{config.direction} forbids {wanted}")


def check_duplicate(
    existing: dict[str, Mapping], config: BridgeConfig, event: InboundEvent
) -> None:
    if mapping_key(config.bridge_id, event.platform, event.message_id) in existing:
        raise BridgeError("BRIDGE_DUPLICATE_SOURCE", f"{event.platform}:{event.message_id}")


def _find_by_mm_post(existing: dict[str, Mapping], bridge_id: str, post_id: str) -> Mapping | None:
    for m in existing.values():
        if m.bridge_id == bridge_id and m.mm_post_id == post_id:
            return m
    return None


def _find_by_tg_thread(
    existing: dict[str, Mapping], bridge_id: str, thread_id: int | None
) -> Mapping | None:
    for m in existing.values():
        if m.bridge_id == bridge_id and m.tg_thread_id == thread_id and m.mm_root_id is None:
            return m
    return None


def _find_by_tg_message(
    existing: dict[str, Mapping], bridge_id: str, message_id: int
) -> Mapping | None:
    for m in existing.values():
        if m.bridge_id == bridge_id and m.tg_message_id == message_id:
            return m
    return None


def resolve_target(
    existing: dict[str, Mapping], config: BridgeConfig, event: InboundEvent
) -> Target:
    """Decide where a message goes on the other platform (pure; stable BridgeError codes).

    Mattermost → Telegram: a root post opens (or reuses) a forum topic (``topic_per_root``), or goes
    to the General topic / a fixed topic per policy; a thread reply goes into the topic of its root
    with ``reply_parameters`` pointing at the mapped Telegram message of the replied post.
    Telegram → Mattermost: a message in a mapped topic becomes a thread reply under the mapped root
    post; a message in an unmapped topic (or General) becomes a new root post; a Telegram reply to a
    mapped message becomes a reply in that message's thread.
    """
    check_loop(event)
    check_direction(config, event)
    check_duplicate(existing, config, event)
    prefix = display_prefix(event.sender_name, event.platform)
    origin_platform = event.origin_platform or event.platform
    origin_id = event.origin_message_id or event.message_id
    if event.platform is Platform.MATTERMOST:
        if event.mm_channel_id != config.mm_channel_id:
            raise BridgeError("BRIDGE_TARGET_UNMAPPED", "post is not in the bridged channel")
        if event.mm_root_id is None:  # root post
            if config.tg_thread_mode == "general":
                thread: int | None = GENERAL_TOPIC_THREAD_ID
                create = False
            elif config.tg_thread_mode == "fixed_topic":
                thread, create = config.tg_fixed_thread_id, False
            else:
                thread, create = (
                    None,
                    True,
                )  # topic created at send time; thread id = service msg id
            return Target(
                Platform.TELEGRAM,
                tg_chat_id=config.tg_chat_id,
                tg_thread_id=thread,
                create_topic=create,
                display_prefix=prefix,
                origin_platform=origin_platform,
                origin_message_id=origin_id,
            )
        root = _find_by_mm_post(existing, config.bridge_id, event.mm_root_id)
        if root is None:
            raise BridgeError("BRIDGE_TARGET_UNMAPPED", f"root post {event.mm_root_id} not mapped")
        return Target(
            Platform.TELEGRAM,
            tg_chat_id=config.tg_chat_id,
            tg_thread_id=root.tg_thread_id,
            tg_reply_to_message_id=root.tg_message_id,
            display_prefix=prefix,
            origin_platform=origin_platform,
            origin_message_id=origin_id,
        )
    # telegram → mattermost
    if event.tg_chat_id != config.tg_chat_id:
        raise BridgeError("BRIDGE_TARGET_UNMAPPED", "message is not in the bridged chat")
    if event.tg_reply_to_message_id is not None:
        replied = _find_by_tg_message(existing, config.bridge_id, event.tg_reply_to_message_id)
        if replied is not None:
            return Target(
                Platform.MATTERMOST,
                mm_channel_id=config.mm_channel_id,
                mm_root_id=replied.mm_root_id or replied.mm_post_id,
                display_prefix=prefix,
                origin_platform=origin_platform,
                origin_message_id=origin_id,
            )
    topic_root = _find_by_tg_thread(existing, config.bridge_id, event.tg_thread_id)
    if topic_root is not None and event.tg_thread_id is not None:
        return Target(
            Platform.MATTERMOST,
            mm_channel_id=config.mm_channel_id,
            mm_root_id=topic_root.mm_post_id,
            display_prefix=prefix,
            origin_platform=origin_platform,
            origin_message_id=origin_id,
        )
    # unmapped topic or General topic → new root post
    return Target(
        Platform.MATTERMOST,
        mm_channel_id=config.mm_channel_id,
        mm_root_id=None,
        display_prefix=prefix,
        origin_platform=origin_platform,
        origin_message_id=origin_id,
    )


def send_params_for(target: Target) -> dict[str, object]:
    """Bot API ``sendMessage`` parameters for a Telegram target; General topic omits the thread."""
    params: dict[str, object] = {"chat_id": target.tg_chat_id}
    if target.tg_thread_id is not None:
        params["message_thread_id"] = target.tg_thread_id
    if target.tg_reply_to_message_id is not None:
        params["reply_parameters"] = {"message_id": target.tg_reply_to_message_id}
    return params


def retry_delay(retry_after: int | None, attempt: int) -> float:
    """Honour ``retry_after`` (capped); otherwise exponential backoff 1/2/4… seconds."""
    if retry_after is not None:
        return float(min(max(retry_after, 1), RETRY_AFTER_CAP_S))
    return float(min(2 ** max(attempt - 1, 0), RETRY_AFTER_CAP_S))
