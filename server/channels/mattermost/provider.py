"""Provider instance = Mattermost base URL + team (development plan §7A.1).

Helpers over ``provider_instances``, ``provider_command_tokens``, ``provider_nonces`` and
``thread_bindings``. The bot token comes from settings (``AGENT_COLAB_MATTERMOST_BOT_TOKEN``)
or an injected client factory; it is never stored in the database or Events.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.channels.mattermost.client import HttpMattermostClient, MattermostClient
from server.domain.clock import Clock, SystemClock
from server.domain.defaults import CALLBACK_TIMESTAMP_TOLERANCE_S


class ProviderError(ValueError):
    def __init__(self, code: str, detail: str = "", status: int = 409) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class ProviderInstance:
    id: uuid.UUID
    provider_instance_id: str
    workspace_id: uuid.UUID
    base_url: str
    team_id: str
    team_name: str
    bot_user_id: str | None
    identity_display: str
    status: str


def provider_instance_id_for(base_url: str, team_name: str) -> str:
    host = base_url.split("://", 1)[-1].rstrip("/").replace(":", "_")
    return f"mm:{host}:{team_name}"


def load_instance(session: Session, provider_instance_id: str) -> ProviderInstance | None:
    row = (
        session.execute(
            text(
                "SELECT id, provider_instance_id, workspace_id, base_url, team_or_bot_ref, "
                "bot_user_id, identity_display, status, config FROM provider_instances "
                "WHERE provider_instance_id = :p AND provider = 'mattermost'"
            ),
            {"p": provider_instance_id},
        )
        .mappings()
        .first()
    )
    return _instance(row) if row else None


def load_instance_by_team(session: Session, team_id: str) -> ProviderInstance | None:
    row = (
        session.execute(
            text(
                "SELECT id, provider_instance_id, workspace_id, base_url, team_or_bot_ref, "
                "bot_user_id, identity_display, status, config FROM provider_instances "
                "WHERE team_or_bot_ref = :t AND provider = 'mattermost'"
            ),
            {"t": team_id},
        )
        .mappings()
        .first()
    )
    return _instance(row) if row else None


def _instance(row: Any) -> ProviderInstance:
    cfg = dict(row["config"] or {})
    return ProviderInstance(
        id=uuid.UUID(str(row["id"])),
        provider_instance_id=str(row["provider_instance_id"]),
        workspace_id=uuid.UUID(str(row["workspace_id"])),
        base_url=str(row["base_url"] or ""),
        team_id=str(row["team_or_bot_ref"]),
        team_name=str(cfg.get("team_name", "")),
        bot_user_id=row["bot_user_id"],
        identity_display=str(row["identity_display"]),
        status=str(row["status"]),
    )


# --- client factory --------------------------------------------------------------------------

ClientFactory = Callable[[ProviderInstance], MattermostClient]
_FACTORY: ClientFactory | None = None


def set_client_factory(factory: ClientFactory | None) -> None:
    global _FACTORY
    _FACTORY = factory


def client_for(instance: ProviderInstance) -> MattermostClient:
    """Bot-token client for posting and reading (the runtime identity of the gateway)."""
    if _FACTORY is not None:
        return _FACTORY(instance)
    token = os.environ.get("AGENT_COLAB_MATTERMOST_BOT_TOKEN")
    if not token:
        raise ProviderError("PROVIDER_CREDENTIAL_MISSING", "AGENT_COLAB_MATTERMOST_BOT_TOKEN", 503)
    return HttpMattermostClient(instance.base_url, token)


def admin_client_for(instance: ProviderInstance) -> MattermostClient:
    """Administrative client (slash-command registration, config probe) — spec §12.1 step 5.

    Uses ``AGENT_COLAB_MATTERMOST_ADMIN_TOKEN`` when configured (a Secret reference in Phase 4),
    otherwise the bot token; Mattermost decides whether that credential may manage commands.
    """
    if _FACTORY is not None:
        return _FACTORY(instance)
    token = os.environ.get("AGENT_COLAB_MATTERMOST_ADMIN_TOKEN") or os.environ.get(
        "AGENT_COLAB_MATTERMOST_BOT_TOKEN"
    )
    if not token:
        raise ProviderError(
            "PROVIDER_CREDENTIAL_MISSING", "AGENT_COLAB_MATTERMOST_ADMIN_TOKEN", 503
        )
    return HttpMattermostClient(instance.base_url, token)


# --- identity display probe (development plan §7A.4, P0-10 spike) -------------------------------


def detect_identity_display(config: dict[str, Any] | None) -> str:
    """``override`` only when both override flags are confirmed true; otherwise ``prefix``."""
    if not config:
        return "prefix"
    svc = config.get("ServiceSettings", {})
    if svc.get("EnablePostUsernameOverride") is True and svc.get("EnablePostIconOverride") is True:
        return "override"
    return "prefix"


# --- command tokens ----------------------------------------------------------------------------


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def store_command_token(
    session: Session, instance_uuid: uuid.UUID, trigger: str, token: str, command_ref: str | None
) -> None:
    session.execute(
        text(
            "INSERT INTO provider_command_tokens (provider_instance_id, trigger, token_hash, "
            "command_ref) VALUES (:p, :t, :h, :c) ON CONFLICT (provider_instance_id, trigger) "
            "DO UPDATE SET token_hash = EXCLUDED.token_hash, command_ref = EXCLUDED.command_ref, "
            "rotated_at = now()"
        ),
        {"p": instance_uuid, "t": trigger, "h": token_hash(token), "c": command_ref},
    )


def verify_command_token(
    session: Session, instance_uuid: uuid.UUID, trigger: str, token: str | None
) -> bool:
    row = session.execute(
        text(
            "SELECT token_hash FROM provider_command_tokens WHERE provider_instance_id = :p "
            "AND trigger = :t"
        ),
        {"p": instance_uuid, "t": trigger},
    ).first()
    if row is None or not token:
        return False
    return hmac.compare_digest(str(row[0]), token_hash(token))


# --- one-time nonces (trigger ids, action callbacks) ------------------------------------------


def consume_nonce(
    session: Session,
    instance_uuid: uuid.UUID,
    nonce: str,
    clock: Clock | None = None,
    ttl_s: int = CALLBACK_TIMESTAMP_TOLERANCE_S,
) -> bool:
    """True the first time a nonce is seen within its TTL; False on replay."""
    now = (clock or SystemClock()).now()
    session.execute(text("DELETE FROM provider_nonces WHERE expires_at < :now"), {"now": now})
    inserted = session.execute(
        text(
            "INSERT INTO provider_nonces (provider_instance_id, nonce, expires_at) "
            "VALUES (:p, :n, :e) ON CONFLICT DO NOTHING RETURNING nonce"
        ),
        {"p": instance_uuid, "n": nonce, "e": now + dt.timedelta(seconds=ttl_s)},
    ).first()
    return inserted is not None


# --- thread bindings -------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadBinding:
    root_post_id: str
    external_channel_id: str
    subject_type: str
    subject_id: str


def bind_thread(
    session: Session,
    instance_uuid: uuid.UUID,
    root_post_id: str,
    external_channel_id: str,
    subject_type: str,
    subject_id: str,
) -> None:
    session.execute(
        text(
            "INSERT INTO thread_bindings (provider_instance_id, root_post_id, external_channel_id, "
            "subject_type, subject_id) VALUES (:p, :r, :c, :t, :s) ON CONFLICT DO NOTHING"
        ),
        {
            "p": instance_uuid,
            "r": root_post_id,
            "c": external_channel_id,
            "t": subject_type,
            "s": subject_id,
        },
    )


def binding_for_post(
    session: Session, instance_uuid: uuid.UUID, root_post_id: str
) -> ThreadBinding | None:
    row = session.execute(
        text(
            "SELECT root_post_id, external_channel_id, subject_type, subject_id FROM "
            "thread_bindings "
            "WHERE provider_instance_id = :p AND root_post_id = :r"
        ),
        {"p": instance_uuid, "r": root_post_id},
    ).first()
    return ThreadBinding(str(row[0]), str(row[1]), str(row[2]), str(row[3])) if row else None


def binding_for_subject(
    session: Session, instance_uuid: uuid.UUID, subject_type: str, subject_id: str
) -> ThreadBinding | None:
    row = session.execute(
        text(
            "SELECT root_post_id, external_channel_id, subject_type, subject_id FROM "
            "thread_bindings "
            "WHERE provider_instance_id = :p AND subject_type = :t AND subject_id = :s"
        ),
        {"p": instance_uuid, "t": subject_type, "s": subject_id},
    ).first()
    return ThreadBinding(str(row[0]), str(row[1]), str(row[2]), str(row[3])) if row else None


# --- channels ------------------------------------------------------------------------------------


def channel_id_for(provider_instance_id: str, external_channel_id: str) -> str:
    seed = f"{provider_instance_id}|{external_channel_id}".encode()
    return "chan-" + hashlib.sha256(seed).hexdigest()[:16]


def internal_channel(session: Session, instance_uuid: uuid.UUID, external_channel_id: str) -> Any:
    return (
        session.execute(
            text(
                "SELECT id, channel_id, channel_type, policy, language, status, template_id "
                "FROM channels WHERE provider_instance_id = :p AND external_channel_id = :c"
            ),
            {"p": instance_uuid, "c": external_channel_id},
        )
        .mappings()
        .first()
    )
