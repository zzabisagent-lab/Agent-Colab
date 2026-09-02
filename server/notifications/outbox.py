"""Delivery outbox for notifications (development plan §7G, §10.2 "transactional outbox").

Rows are written in the same transaction as the notification; a drain claims pending rows with
``FOR UPDATE SKIP LOCKED``, calls the provider, and marks ``sent``/``failed`` (exponential
backoff) or ``dead`` after ``max_attempts``. A successful per-recipient send appends exactly one
``NOTIFICATION_SENT`` Event (idempotent on the outbox id). Providers are stubs until P2-17.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.events.store import AppendRequest, EventStore, EventStoreError

BACKOFF_S = (1, 5, 25, 125, 625)
DEFAULT_MAX_ATTEMPTS = 5


class Provider(Protocol):
    def send(self, destination: str, payload: dict[str, Any]) -> None:
        """Deliver ``payload`` to ``destination``; raise on failure."""
        ...


@dataclass
class StubProvider:
    """Records deliveries; fails the first ``fail_times`` sends when configured."""

    fail_times: int = 0
    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    failures: int = 0

    def send(self, destination: str, payload: dict[str, Any]) -> None:
        if self.failures < self.fail_times:
            self.failures += 1
            raise RuntimeError(f"stub provider failure {self.failures}")
        self.sent.append((destination, payload))


def enqueue(
    session: Session,
    workspace_id: str,
    kind: str,
    destination: str,
    dedupe_key: str,
    payload: dict[str, Any],
    source_event_id: str | None,
    next_attempt_at: dt.datetime,
) -> str | None:
    """Insert one outbox row; returns its id, or None when the dedupe key already exists."""
    outbox_id = "obx-" + uuid.uuid4().hex[:20]
    row = session.execute(
        text(
            "INSERT INTO delivery_outbox (outbox_id, workspace_id, kind, destination, dedupe_key, "
            "payload, "
            "source_event_id, status, next_attempt_at) VALUES (:id, :ws, :kind, :dest, :key, "
            "CAST(:payload AS jsonb), "
            ":ev, 'pending', :next) ON CONFLICT (dedupe_key) DO NOTHING RETURNING outbox_id"
        ),
        {
            "id": outbox_id,
            "ws": uuid.UUID(workspace_id),
            "kind": kind,
            "dest": destination,
            "key": dedupe_key,
            "payload": json.dumps(payload),
            "ev": source_event_id,
            "next": next_attempt_at,
        },
    ).first()
    return None if row is None else str(row[0])


def enqueue_digest(
    session: Session,
    workspace_id: str,
    recipient: str,
    digest_key: str,
    item: dict[str, Any],
    source_event_id: str,
    deliver_at: dt.datetime,
) -> str:
    """One hourly digest row per recipient; later items are appended to the same row."""
    outbox_id = "obx-" + uuid.uuid4().hex[:20]
    row = session.execute(
        text(
            "INSERT INTO delivery_outbox (outbox_id, workspace_id, kind, destination, dedupe_key, "
            "payload, "
            "source_event_id, status, next_attempt_at) VALUES (:id, :ws, 'notification_digest', "
            ":dest, :key, "
            "CAST(:payload AS jsonb), :ev, 'pending', :next) ON CONFLICT (dedupe_key) DO UPDATE "
            "SET "
            "payload = jsonb_set(delivery_outbox.payload, '{items}', "
            "(delivery_outbox.payload->'items') || "
            "(EXCLUDED.payload->'items')) RETURNING outbox_id"
        ),
        {
            "id": outbox_id,
            "ws": uuid.UUID(workspace_id),
            "dest": f"mattermost:dm:{recipient}",
            "key": digest_key,
            "payload": json.dumps(
                {"digest": True, "recipient_account_id": recipient, "items": [item]}
            ),
            "ev": source_event_id,
            "next": deliver_at,
        },
    ).first()
    assert row is not None
    return str(row[0])


def schedule_reminders(
    session: Session,
    workspace_id: str,
    notification_id: str,
    recipient: str,
    channels: list[str],
    times: list[tuple[str, dt.datetime]],
    source_event_id: str,
    extra: dict[str, Any],
) -> list[str]:
    ids: list[str] = []
    for tag, when in times:
        for channel in channels:
            oid = enqueue(
                session,
                workspace_id,
                "notification_reminder",
                f"{channel}:{recipient}",
                f"{notification_id}|{channel}|{tag}",
                {
                    "notification_id": notification_id,
                    "recipient_account_id": recipient,
                    "channel": channel,
                    "reminder": tag,
                    **extra,
                },
                source_event_id,
                when,
            )
            if oid:
                ids.append(oid)
    return ids


def cancel_pending(session: Session, notification_id: str, reason: str = "cancelled") -> int:
    """Stop pending reminders/re-notifications for a notification (e.g. verifier accepted)."""
    result = session.execute(
        text(
            "UPDATE delivery_outbox SET status = 'dead', last_error = :reason WHERE status = "
            "'pending' "
            "AND payload->>'notification_id' = :n AND kind IN ('notification_reminder')"
        ),
        {"reason": reason, "n": notification_id},
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class DrainResult:
    sent: int = 0
    failed: int = 0
    dead: int = 0
    events_appended: int = 0


def drain(
    session: Session,
    provider: Provider,
    store: EventStore,
    clock: Clock,
    service_account_uuid: str,
    workspace_id: str,
    batch: int = 50,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> DrainResult:
    """Deliver due pending rows. Failures never touch Task/Event state; only outbox rows change."""
    now = clock.now()
    rows = session.execute(
        text(
            "SELECT id, outbox_id, kind, destination, payload, attempts FROM delivery_outbox "
            "WHERE workspace_id = :ws AND status = 'pending' AND next_attempt_at <= :now "
            "ORDER BY next_attempt_at, id LIMIT :lim FOR UPDATE SKIP LOCKED"
        ),
        {"ws": uuid.UUID(workspace_id), "now": now, "lim": batch},
    ).all()
    sent = failed = dead = appended = 0
    for row_id, outbox_id, kind, destination, payload, attempts in rows:
        data = payload if isinstance(payload, dict) else json.loads(payload)
        try:
            provider.send(str(destination), data)
        except Exception as exc:
            attempts = int(attempts) + 1
            if attempts >= max_attempts:
                session.execute(
                    text(
                        "UPDATE delivery_outbox SET status = 'dead', attempts = :a, last_error = "
                        ":e WHERE id = :i"
                    ),
                    {"a": attempts, "e": str(exc)[:500], "i": row_id},
                )
                dead += 1
            else:
                delay = BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)]
                session.execute(
                    text(
                        "UPDATE delivery_outbox SET attempts = :a, last_error = :e, "
                        "next_attempt_at = :n WHERE id = :i"
                    ),
                    {
                        "a": attempts,
                        "e": str(exc)[:500],
                        "n": now + dt.timedelta(seconds=delay),
                        "i": row_id,
                    },
                )
                failed += 1
            continue
        session.execute(
            text(
                "UPDATE delivery_outbox SET status = 'sent', attempts = attempts + 1, sent_at = "
                ":n WHERE id = :i"
            ),
            {"n": now, "i": row_id},
        )
        sent += 1
        notification_id = data.get("notification_id")
        if kind == "notification" and notification_id:
            session.execute(
                text(
                    "UPDATE notifications SET status = 'sent', sent_at = :n WHERE notification_id "
                    "= :id AND status = 'queued'"
                ),
                {"n": now, "id": notification_id},
            )
            try:
                store.append(
                    AppendRequest(
                        workspace_id=workspace_id,
                        aggregate_type="notification",
                        aggregate_id=str(notification_id),
                        type="NOTIFICATION_SENT",
                        actor_account_id=service_account_uuid,
                        correlation_id=str(data.get("event_id", outbox_id)),
                        idempotency_scope="notification:send",
                        idempotency_key=str(outbox_id),
                        payload={
                            "notification_id": str(notification_id),
                            "rule_id": str(data.get("rule_id")),
                            "recipient_account_id": str(data.get("recipient_account_id")),
                            "channel": str(data.get("channel")),
                        },
                    )
                )
                appended += 1
            except EventStoreError as exc:
                if exc.code != "IDEMPOTENCY_CONFLICT":
                    raise
    return DrainResult(sent, failed, dead, appended)
