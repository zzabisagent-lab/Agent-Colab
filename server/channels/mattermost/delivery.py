"""Mattermost channel provider for the outbox drain (P2-03/P2-14).

Delivers ``mattermost.post|patch|ephemeral|dm`` payloads through a ``MattermostClient``; idempotent
per dedupe key (an already-sent ``channel_posts`` row short-circuits without a client call);
applies the server-decided Agent identity display when the payload names an Agent author.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.channels.identity_display import (
    apply_display,
    audit_injection,
    display_for_agent,
    strip_injected_identity,
)
from server.channels.mattermost.client import MattermostClient
from server.channels.mattermost.provider import ProviderInstance


class MattermostChannelProvider:
    def __init__(
        self,
        client: MattermostClient,
        provider: ProviderInstance,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._client = client
        self._provider = provider
        self._session_factory = session_factory
        self.injections: list[tuple[str, list[str]]] = []
        self.delivered: dict[str, str] = {}

    def _already_sent(self, dedupe_key: str) -> str | None:
        if dedupe_key in self.delivered:
            return self.delivered[dedupe_key]
        if self._session_factory is None:
            return None
        session = self._session_factory()
        try:
            row = session.execute(
                text(
                    "SELECT post_id FROM channel_posts WHERE dedupe_key = :k AND status = 'sent' "
                    "AND post_id IS NOT NULL"
                ),
                {"k": dedupe_key},
            ).first()
        finally:
            session.close()
        return None if row is None else str(row[0])

    def _prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_name = payload.get("agent_display_name")
        if not agent_name:
            return payload
        clean, removed = strip_injected_identity(payload)
        if removed:
            self.injections.append((str(agent_name), removed))
            if self._session_factory is not None:
                session = self._session_factory()
                try:
                    with session.begin():
                        audit_injection(
                            session,
                            workspace_id=self._provider.workspace_id,
                            agent_label=str(agent_name),
                            subject_id=str(payload.get("dedupe_key", "-")),
                            removed=removed,
                            correlation_id=str(payload.get("dedupe_key", "-")),
                        )
                finally:
                    session.close()
        identity = display_for_agent(self._provider, str(agent_name), payload.get("agent_icon_url"))
        return apply_display(clean, identity)

    def deliver(self, destination: str, payload: dict[str, Any]) -> str:
        dedupe_key = str(payload.get("dedupe_key", ""))
        if dedupe_key:
            prior = self._already_sent(dedupe_key)
            if prior is not None:
                return prior
        channel = destination.split(":", 1)[1] if ":" in destination else destination
        data = self._prepare(payload)
        props = data.get("props")
        message = str(data.get("message", ""))
        if "post_id" in data:
            post = self._client.patch_post(str(data["post_id"]), message, props)
            post_id = post.id
        elif data.get("ephemeral"):
            self._client.ephemeral(str(data["user_id"]), channel, message)
            post_id = f"ephemeral:{dedupe_key or channel}"
        elif data.get("dm_user_id"):
            post_id = self._client.direct_message(str(data["dm_user_id"]), message).id
        else:
            post_id = self._client.create_post(
                channel, message, data.get("root_id") or None, props
            ).id
        if dedupe_key:
            self.delivered[dedupe_key] = post_id
        return post_id
