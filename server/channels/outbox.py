"""Channel delivery outbox (P2-03; development plan §3.1 Renderer/outbox, §10.2 retry).

Renderer enqueues are written in the **same transaction** as the Event they render, so a rollback
removes both, and a replay after a crash redelivers exactly once per ``dedupe_key`` (the
``delivery_outbox.dedupe_key`` unique index plus the provider's idempotency). ``drain_channels``
routes rows to providers by destination prefix (``mattermost:`` / ``telegram:``) and records the
resulting post ids in ``channel_posts`` so cards can be edited in place later.
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

BACKOFF_S = (1, 5, 25, 125, 625)
DEFAULT_MAX_ATTEMPTS = 5


class ChannelProvider(Protocol):
    """Idempotent per ``payload["dedupe_key"]``; returns the provider post/message id."""

    def deliver(self, destination: str, payload: dict[str, Any]) -> str: ...


@dataclass
class Delivery:
    """A rendered side effect to enqueue with an Event."""

    kind: str  # mattermost.post | mattermost.patch | mattermost.ephemeral | telegram.send | ...
    destination: str  # "<provider>:<external channel id>[:<thread>]"
    payload: dict[str, Any]
    dedupe_key: str
    subject_type: str | None = None
    subject_id: str | None = None
    role: str | None = None  # card | reply | link_card | ephemeral
    root_post_id: str | None = None


def enqueue_delivery(
    session: Session,
    *,
    workspace_id: str,
    source_event_id: str | None,
    delivery: Delivery,
    provider_instance_id: str,
    external_channel_id: str,
    now: dt.datetime,
) -> str | None:
    """Insert the outbox row (and its channel_posts record); None when the dedupe key exists."""
    outbox_id = "obx-" + uuid.uuid4().hex[:20]
    row = session.execute(
        text(
            "INSERT INTO delivery_outbox (outbox_id, workspace_id, kind, destination, dedupe_key, "
            "payload, source_event_id, status, next_attempt_at) VALUES (:id, :ws, :kind, :dest, "
            ":key, CAST(:payload AS jsonb), :ev, 'pending', :now) "
            "ON CONFLICT (dedupe_key) DO NOTHING RETURNING outbox_id"
        ),
        {
            "id": outbox_id,
            "ws": uuid.UUID(workspace_id),
            "kind": delivery.kind,
            "dest": delivery.destination,
            "key": delivery.dedupe_key,
            "payload": json.dumps({**delivery.payload, "dedupe_key": delivery.dedupe_key}),
            "ev": source_event_id,
            "now": now,
        },
    ).first()
    if row is None:
        return None
    if delivery.subject_type and delivery.subject_id and delivery.role:
        session.execute(
            text(
                "INSERT INTO channel_posts (workspace_id, provider_instance_id, "
                "external_channel_id, subject_type, subject_id, role, dedupe_key, root_post_id, "
                "source_event_id, status) "
                "VALUES (:ws, :pi, :ch, :st, :sid, :role, :key, :root, :ev, 'pending') "
                "ON CONFLICT (dedupe_key) DO NOTHING"
            ),
            {
                "ws": uuid.UUID(workspace_id),
                "pi": provider_instance_id,
                "ch": external_channel_id,
                "st": delivery.subject_type,
                "sid": delivery.subject_id,
                "role": delivery.role,
                "key": delivery.dedupe_key,
                "root": delivery.root_post_id,
                "ev": source_event_id,
            },
        )
    return str(row[0])


def card_post_id(
    session: Session, provider_instance_id: str, subject_type: str, subject_id: str
) -> str | None:
    row = session.execute(
        text(
            "SELECT post_id FROM channel_posts WHERE provider_instance_id = :pi "
            "AND subject_type = :st AND subject_id = :sid AND role = 'card' AND status = 'sent'"
        ),
        {"pi": provider_instance_id, "st": subject_type, "sid": subject_id},
    ).first()
    return None if row is None or row[0] is None else str(row[0])


@dataclass
class ChannelDrainResult:
    sent: int = 0
    failed: int = 0
    dead: int = 0
    skipped_no_provider: int = 0
    latencies_ms: list[int] = field(default_factory=list)


def requeue_dead(
    session: Session,
    workspace_id: str,
    clock: Clock,
    *,
    reason: str,
    kinds_prefix: tuple[str, ...] = ("mattermost.", "telegram."),
    limit: int = 500,
) -> int:
    """Return dead-lettered deliveries to `pending` after the destination recovers (V-P7-05).

    A dependency outage longer than the backoff budget (1/5/25/125/625 s over five attempts)
    exhausts a row's attempts, so without this a ten-minute outage would drop messages the
    criterion requires to be preserved and delivered exactly once after recovery. The dedupe key
    still makes redelivery exactly-once, and a genuinely undeliverable payload simply dies again;
    `last_error` keeps the requeue reason so repeated revivals are visible to an operator.
    """
    prefixes = [f"{p}%" for p in kinds_prefix]
    rows = session.execute(
        text(
            "UPDATE delivery_outbox SET status = 'pending', attempts = 0, next_attempt_at = :now, "
            "last_error = :reason WHERE outbox_id IN (SELECT outbox_id FROM delivery_outbox "
            "WHERE workspace_id = :ws AND status = 'dead' AND kind LIKE ANY(:pfx) "
            "ORDER BY id LIMIT :lim) RETURNING dedupe_key"
        ),
        {
            "now": clock.now(),
            "reason": f"requeued after {reason}"[:500],
            "ws": uuid.UUID(workspace_id),
            "pfx": prefixes,
            "lim": limit,
        },
    ).all()
    keys = [str(r[0]) for r in rows]
    if keys:
        session.execute(
            text("UPDATE channel_posts SET status = 'pending' WHERE dedupe_key = ANY(:k)"),
            {"k": keys},
        )
    return len(keys)


def drain_channels(
    session: Session,
    providers: dict[str, ChannelProvider],
    clock: Clock,
    workspace_id: str,
    *,
    batch: int = 100,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    kinds_prefix: tuple[str, ...] = ("mattermost.", "telegram."),
) -> ChannelDrainResult:
    """Deliver due channel rows; failures only touch outbox/channel_posts rows (never Events)."""
    now = clock.now()
    # the kind filter belongs in the query: rows of another kind (notifications have their own
    # drain) would otherwise fill the batch and starve channel deliveries behind them
    kinds = " OR ".join(f"left(kind, {len(k)}) = :k{i}" for i, k in enumerate(kinds_prefix))
    params: dict[str, Any] = {"ws": uuid.UUID(workspace_id), "now": now, "lim": batch}
    params.update({f"k{i}": k for i, k in enumerate(kinds_prefix)})
    rows = session.execute(
        text(
            "SELECT id, outbox_id, kind, destination, payload, attempts, dedupe_key, created_at "  # noqa: S608
            "FROM delivery_outbox WHERE workspace_id = :ws AND status = 'pending' "
            f"AND ({kinds or 'true'}) "
            "AND next_attempt_at <= :now ORDER BY next_attempt_at, id LIMIT :lim "
            "FOR UPDATE SKIP LOCKED"
        ),
        params,
    ).all()
    result = ChannelDrainResult()
    for row_id, _outbox_id, kind, destination, payload, attempts, dedupe_key, created_at in rows:
        if not str(kind).startswith(kinds_prefix):
            continue
        prefix = str(destination).split(":", 1)[0]
        provider = providers.get(prefix)
        if provider is None:
            result.skipped_no_provider += 1
            continue
        data = payload if isinstance(payload, dict) else json.loads(payload)
        try:
            post_id = provider.deliver(str(destination), data)
        except Exception as exc:
            attempts = int(attempts) + 1
            if attempts >= max_attempts:
                session.execute(
                    text(
                        "UPDATE delivery_outbox SET status = 'dead', attempts = :a, last_error "
                        "= :e "
                        "WHERE id = :i"
                    ),
                    {"a": attempts, "e": str(exc)[:500], "i": row_id},
                )
                session.execute(
                    text("UPDATE channel_posts SET status = 'dead' WHERE dedupe_key = :k"),
                    {"k": dedupe_key},
                )
                result.dead += 1
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
                result.failed += 1
            continue
        session.execute(
            text(
                "UPDATE delivery_outbox SET status = 'sent', attempts = attempts + 1, "
                "sent_at = :now "
                "WHERE id = :i"
            ),
            {"now": now, "i": row_id},
        )
        session.execute(
            text(
                "UPDATE channel_posts SET status = 'sent', post_id = :p, sent_at = :now "
                "WHERE dedupe_key = :k"
            ),
            {"p": post_id, "now": now, "k": dedupe_key},
        )
        result.sent += 1
        result.latencies_ms.append(int((now - created_at).total_seconds() * 1000))
    return result


@dataclass
class RecordingChannelProvider:
    """Test double: idempotent per dedupe key, optional injected failures after the side effect."""

    prefix: str = "mattermost"
    delivered: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_after_send_times: int = 0
    _failures: int = 0

    def deliver(self, destination: str, payload: dict[str, Any]) -> str:
        key = str(payload["dedupe_key"])
        if key in self.delivered:
            return self.delivered[key]  # provider-side idempotency: no second side effect
        post_id = f"{self.prefix}-post-{len(self.delivered) + 1:04d}"
        self.delivered[key] = post_id
        self.calls.append((destination, payload))
        if self._failures < self.fail_after_send_times:
            self._failures += 1
            raise RuntimeError("crash right after the provider send")
        return post_id
