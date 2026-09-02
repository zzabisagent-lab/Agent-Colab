"""Bridge administration (P2-05 admin half, spec §10.1/§10.2, §11.2): create/update/enable/
disable/test/status over ``telegram_bridges``. The one-target-one-channel rule is enforced by the
partial unique index and checked here for a stable error; an administrator exception is recorded
on the row and audited.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.channels import telegram_contract as tc
from server.channels.telegram.client import TelegramClient

SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "api"
    / "bridge"
    / "telegram-bridge.v1.schema.json"
)


class BridgeAdminError(ValueError):
    def __init__(self, code: str, detail: str, status: int = 409) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))


def validate_config(config: dict[str, Any]) -> None:
    errors = sorted(_validator().iter_errors(config), key=lambda e: list(e.path))
    if errors:
        path = "/".join(str(p) for p in errors[0].path) or "<root>"
        raise BridgeAdminError("BRIDGE_CONFIG_INVALID", f"{path}: {errors[0].message}", 400)
    mode = config.get("thread_mode", "topic_per_root")
    if (mode == "fixed_topic") != (config.get("telegram_thread_id") is not None):
        raise BridgeAdminError(
            "BRIDGE_CONFIG_INVALID", "fixed_topic requires telegram_thread_id (and only then)", 400
        )


@dataclass(frozen=True)
class BridgeView:
    bridge_id: str
    channel_id: str
    provider_instance_id: str
    telegram_chat_id: str
    telegram_thread_id: int | None
    thread_mode: str
    direction: str
    status: str
    admin_exception: bool
    allow_commands: bool


def _view(row: Any) -> BridgeView:
    return BridgeView(
        bridge_id=str(row["bridge_id"]),
        channel_id=str(row["channel_public_id"]),
        provider_instance_id=str(row["provider_instance_id"]),
        telegram_chat_id=str(row["telegram_chat_id"]),
        telegram_thread_id=int(row["telegram_thread_id"]) if row["telegram_thread_id"] else None,
        thread_mode=str(row["thread_mode"]),
        direction=str(row["direction"]),
        status=str(row["status"]),
        admin_exception=bool(row["admin_exception"]),
        allow_commands=bool(row["allow_commands"]),
    )


_VIEW_SQL = (
    "SELECT b.bridge_id, c.channel_id AS channel_public_id, b.provider_instance_id, "
    "b.telegram_chat_id, b.telegram_thread_id, b.thread_mode, b.direction, b.status, "
    "b.admin_exception, b.allow_commands FROM telegram_bridges b "
    "JOIN channels c ON c.id = b.channel_id "
)


def channel_uuid(session: Session, workspace_id: str, channel_public_id: str) -> uuid.UUID:
    row = session.execute(
        text(
            "SELECT id FROM channels WHERE channel_id = :c AND workspace_id = :ws "
            "AND status <> 'deleted'"
        ),
        {"c": channel_public_id, "ws": uuid.UUID(workspace_id)},
    ).first()
    if row is None:
        raise BridgeAdminError("NOT_FOUND", "channel not found", 404)
    return uuid.UUID(str(row[0]))


def target_owner(
    session: Session, provider_instance_id: str, chat_id: str, thread_id: int | None
) -> str | None:
    """The channel (public id) already bound to this Telegram target without an exception."""
    row = session.execute(
        text(
            "SELECT c.channel_id FROM telegram_bridges b JOIN channels c ON c.id = b.channel_id "
            "WHERE b.provider_instance_id = :pi AND b.telegram_chat_id = :chat "
            "AND COALESCE(b.telegram_thread_id, '') = :thread AND b.admin_exception = false"
        ),
        {
            "pi": provider_instance_id,
            "chat": chat_id,
            "thread": str(thread_id) if thread_id else "",
        },
    ).first()
    return None if row is None else str(row[0])


def create_bridge(
    session: Session,
    *,
    workspace_id: str,
    config: dict[str, Any],
    created_by: str,
    now: dt.datetime,
    exception_allowed: bool,
) -> BridgeView:
    validate_config(config)
    ch = channel_uuid(session, workspace_id, str(config["channel_id"]))
    thread = config.get("telegram_thread_id")
    owner = target_owner(
        session, str(config["provider_instance_id"]), str(config["telegram_chat_id"]), thread
    )
    exception = bool(config.get("admin_exception", False))
    if owner is not None and owner != config["channel_id"]:
        if not exception:
            raise BridgeAdminError(
                "BRIDGE_TARGET_DUPLICATE",
                f"Telegram target already bound to channel {owner}; "
                "an administrator exception is required",
            )
        if not exception_allowed:
            raise BridgeAdminError("BRIDGE_EXCEPTION_FORBIDDEN", "admin.settings required", 403)
    if exception and not exception_allowed:
        raise BridgeAdminError("BRIDGE_EXCEPTION_FORBIDDEN", "admin.settings required", 403)
    bridge_id = config.get("bridge_id") or "bridge-" + uuid.uuid4().hex[:16]
    try:
        with session.begin_nested():
            defaults_cp = {
                "text": True,
                "attachment": True,
                "system_event": False,
                "approval_notice": True,
                "mention": True,
            }
            session.execute(
                text(
                    "INSERT INTO telegram_bridges (id, bridge_id, workspace_id, channel_id, "
                    "provider_instance_id, telegram_chat_id, telegram_thread_id, thread_mode, "
                    "direction, content_policy, redaction_policy, identity_display, rate_limit, "
                    "allow_commands, admin_exception, admin_exception_reason, status, created_by, "
                    "created_at, updated_at) VALUES (:id, :b, :ws, :ch, :pi, :chat, :thread, "
                    ":mode, "
                    ":dir, CAST(:cp AS jsonb), CAST(:rp AS jsonb), CAST(:idd AS jsonb), "
                    "CAST(:rl AS jsonb), :cmd, :exc, :reason, :status, :by, :now, :now)"
                ),
                {
                    "id": uuid.uuid4(),
                    "b": bridge_id,
                    "ws": uuid.UUID(workspace_id),
                    "ch": ch,
                    "pi": config["provider_instance_id"],
                    "chat": str(config["telegram_chat_id"]),
                    "thread": str(thread) if thread is not None else None,
                    "mode": config.get("thread_mode", "topic_per_root"),
                    "dir": config["direction"],
                    "cp": json.dumps({**defaults_cp, **config.get("content_policy", {})}),
                    "rp": json.dumps(config.get("redaction_policy", {"secret_patterns": True})),  # nosec B105 - boolean secret_configured flag, not a value
                    "idd": json.dumps(config.get("identity_display", {"show_sender": True})),
                    "rl": json.dumps(config.get("rate_limit", {"per_minute": 20})),
                    "cmd": bool(config.get("allow_commands", False)),
                    "exc": exception,
                    "reason": config.get("admin_exception_reason"),
                    "status": config.get("status", "enabled"),
                    "by": uuid.UUID(created_by),
                    "now": now,
                },
            )
    except IntegrityError as exc:
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", "") or ""
        if "one_channel_per_target" in name:
            raise BridgeAdminError(
                "BRIDGE_TARGET_DUPLICATE", "Telegram target already bound to another channel"
            ) from exc
        raise BridgeAdminError("BRIDGE_CONFIG_INVALID", name or str(exc.orig), 400) from exc
    return get_bridge(session, bridge_id)


def get_bridge(session: Session, bridge_id: str) -> BridgeView:
    row = (
        session.execute(text(_VIEW_SQL + "WHERE b.bridge_id = :b"), {"b": bridge_id})
        .mappings()
        .first()
    )
    if row is None:
        raise BridgeAdminError("NOT_FOUND", "bridge not found", 404)
    return _view(row)


def list_bridges(session: Session, workspace_id: str, channel_public_id: str) -> list[BridgeView]:
    rows = session.execute(
        text(_VIEW_SQL + "WHERE c.channel_id = :c AND b.workspace_id = :ws ORDER BY b.bridge_id"),
        {"c": channel_public_id, "ws": uuid.UUID(workspace_id)},
    ).mappings()
    return [_view(r) for r in rows]


_UPDATABLE = {
    "direction": "direction",
    "thread_mode": "thread_mode",
    "telegram_thread_id": "telegram_thread_id",
    "content_policy": "content_policy",
    "redaction_policy": "redaction_policy",
    "identity_display": "identity_display",
    "rate_limit": "rate_limit",
    "allow_commands": "allow_commands",
}


def update_bridge(
    session: Session, bridge_id: str, changes: dict[str, Any], now: dt.datetime
) -> BridgeView:
    current = get_bridge(session, bridge_id)
    unknown = set(changes) - set(_UPDATABLE)
    if unknown:
        raise BridgeAdminError("BRIDGE_CONFIG_INVALID", f"not updatable: {sorted(unknown)}", 400)
    merged = {
        "channel_id": current.channel_id,
        "provider_instance_id": current.provider_instance_id,
        "telegram_chat_id": current.telegram_chat_id,
        "telegram_thread_id": changes.get("telegram_thread_id", current.telegram_thread_id),
        "thread_mode": changes.get("thread_mode", current.thread_mode),
        "direction": changes.get("direction", current.direction),
    }
    validate_config({k: v for k, v in merged.items() if v is not None or k == "telegram_thread_id"})
    for key, value in changes.items():
        column = _UPDATABLE[key]
        if isinstance(value, dict):
            session.execute(
                text(
                    f"UPDATE telegram_bridges SET {column} = CAST(:v AS jsonb), "  # noqa: S608
                    "updated_at = :now WHERE bridge_id = :b"
                ),
                {"v": json.dumps(value), "now": now, "b": bridge_id},
            )
        else:
            param = str(value) if key == "telegram_thread_id" and value is not None else value
            session.execute(
                text(
                    f"UPDATE telegram_bridges SET {column} = :v, updated_at = :now "  # noqa: S608
                    "WHERE bridge_id = :b"
                ),
                {"v": param, "now": now, "b": bridge_id},
            )
    return get_bridge(session, bridge_id)


def set_status(session: Session, bridge_id: str, status: str, now: dt.datetime) -> BridgeView:
    if status not in ("enabled", "disabled"):
        raise BridgeAdminError("BRIDGE_CONFIG_INVALID", status, 400)
    get_bridge(session, bridge_id)
    session.execute(
        text("UPDATE telegram_bridges SET status = :s, updated_at = :now WHERE bridge_id = :b"),
        {"s": status, "now": now, "b": bridge_id},
    )
    return get_bridge(session, bridge_id)


def test_bridge(session: Session, client: TelegramClient, bridge_id: str) -> dict[str, Any]:
    """Send a probe to the Telegram target; no mapping row, no relay."""
    view = get_bridge(session, bridge_id)
    sent = client.send_message(
        view.telegram_chat_id,
        f"Agent-Colab bridge test ({bridge_id})",
        message_thread_id=view.telegram_thread_id if view.thread_mode == "fixed_topic" else None,
    )
    return {"bridge_id": bridge_id, "message_id": sent.message_id, "chat_id": view.telegram_chat_id}


def bridge_status(session: Session, bridge_id: str) -> dict[str, Any]:
    view = get_bridge(session, bridge_id)
    counts = session.execute(
        text(
            "SELECT delivery_status, count(*) FROM message_mappings WHERE bridge_id = :b "
            "GROUP BY delivery_status"
        ),
        {"b": bridge_id},
    ).all()
    dead = session.execute(
        text(
            "SELECT count(*) FROM bridge_dead_letters WHERE bridge_id = :b AND replayed_at IS NULL"
        ),
        {"b": bridge_id},
    ).scalar_one()
    by_direction = session.execute(
        text(
            "SELECT source_platform, count(*) FROM message_mappings WHERE bridge_id = :b "
            "AND delivery_status = 'sent' GROUP BY source_platform"
        ),
        {"b": bridge_id},
    ).all()
    return {
        "bridge_id": bridge_id,
        "status": view.status,
        "direction": view.direction,
        "deliveries": {str(k): int(v) for k, v in counts},
        "delivered_by_source": {str(k): int(v) for k, v in by_direction},
        "dead_letters_open": int(dead),
        "supported_directions": [d.value for d in tc.Direction],
    }
