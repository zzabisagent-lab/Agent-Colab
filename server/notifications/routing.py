"""Digest scheduling and per-account preferences (P2-17; development plan §7G, §7A.2 notify).

- ``muted``: the rules engine records the notification as ``suppressed`` and enqueues nothing;
  providers additionally skip recipients muted after enqueue (never a send while muted).
- ``digest``: the engine upserts one hourly ``notification_digest`` outbox row per recipient
  (``deliver_at`` = next hour); ``DigestScheduler.flush_due`` delivers the rows whose hour has
  arrived as one DM each, driven by the injected Clock (no real waiting).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.events.store import EventStore
from server.notifications.outbox import DrainResult, Provider, drain
from server.observability.audit import append_audit


@dataclass(frozen=True)
class Preferences:
    account_uuid: str
    muted: bool
    digest: bool


def get_preferences(session: Session, account_uuid: str) -> Preferences:
    row = session.execute(
        text("SELECT muted, digest FROM notification_preferences WHERE account_id = :a"),
        {"a": uuid.UUID(account_uuid)},
    ).first()
    if row is None:
        return Preferences(account_uuid, False, False)
    return Preferences(account_uuid, bool(row[0]), bool(row[1]))


def set_preferences(
    session: Session,
    account_uuid: str,
    *,
    muted: bool | None = None,
    digest: bool | None = None,
    clock: Clock,
    correlation_id: str = "-",
    workspace_id: str | None = None,
    actor_label: str = "self",
) -> Preferences:
    """Upsert the account's own preferences (self-service); audited without values of note."""
    current = get_preferences(session, account_uuid)
    new_muted = current.muted if muted is None else muted
    new_digest = current.digest if digest is None else digest
    session.execute(
        text(
            "INSERT INTO notification_preferences (account_id, muted, digest, updated_at) "
            "VALUES (:a, :m, :d, :t) ON CONFLICT (account_id) DO UPDATE SET "
            "muted = EXCLUDED.muted, digest = EXCLUDED.digest, updated_at = EXCLUDED.updated_at"
        ),
        {"a": uuid.UUID(account_uuid), "m": new_muted, "d": new_digest, "t": clock.now()},
    )
    append_audit(
        session,
        action="notification.preferences_set",
        target_type="account",
        target_id=account_uuid,
        result="OK",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=uuid.UUID(workspace_id) if workspace_id else None,
        actor_account_id=uuid.UUID(account_uuid),
        metadata={"muted": new_muted, "digest": new_digest},
        clock=clock,
    )
    return Preferences(account_uuid, new_muted, new_digest)


@dataclass(frozen=True)
class DigestFlush:
    digests_sent: int
    drain: DrainResult


class DigestScheduler:
    """Hourly batching: digest rows become due at the top of the next hour and are delivered by
    the same drain; ``flush_due`` reports how many digest DMs went out at this clock time."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def pending_digests(self, session: Session, workspace_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                "SELECT destination, next_attempt_at, jsonb_array_length(payload->'items') "
                "FROM delivery_outbox WHERE workspace_id = :ws AND kind = 'notification_digest' "
                "AND status = 'pending' ORDER BY next_attempt_at"
            ),
            {"ws": uuid.UUID(workspace_id)},
        ).all()
        return [{"destination": str(d), "deliver_at": t, "items": int(n)} for d, t, n in rows]

    def flush_due(
        self,
        session: Session,
        provider: Provider,
        store: EventStore,
        service_account_uuid: str,
        workspace_id: str,
    ) -> DigestFlush:
        now = self._clock.now()
        result = drain(session, provider, store, self._clock, service_account_uuid, workspace_id)
        after = session.execute(
            text(
                "SELECT count(*) FROM delivery_outbox WHERE workspace_id = :ws "
                "AND kind = 'notification_digest' AND status = 'sent' AND sent_at >= :t"
            ),
            {"ws": uuid.UUID(workspace_id), "t": now - dt.timedelta(seconds=1)},
        ).scalar_one()
        return DigestFlush(int(after), result)
