"""Message retention and legal hold (P2-15; development plan §7H, spec §11.2, §16).

A daily retention job destroys the per-message DEK of expired Messages (crypto-shredding), marks
the row ``REDACTED_BY_RETENTION`` and appends a chained ``message_tombstones`` row; rows are never
deleted. Channels (or messages) under legal hold are skipped entirely. The job is deterministic
for an injected ``Clock`` and idempotent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.channels.ingestion import DOCUMENTATION_POLICIES, REDACTED_BY_RETENTION
from server.domain.clock import Clock
from server.domain.defaults import MESSAGE_RETENTION_DAYS
from server.events.chain import ChainSpec, chain_hash, hashed_row_fields, last_hash
from server.observability.audit import append_audit
from server.secrets.envelope import CryptoError, EnvelopeCrypto

MAX_RETENTION_DAYS = 3650

MESSAGE_TOMBSTONE_CHAIN = ChainSpec(
    table="message_tombstones",
    order_column="id",
    hashed_fields=("message_id", "channel_id", "reason", "key_ref", "deleted_at"),
    chain_name="message_tombstones",
)


class RetentionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RetentionPolicy:
    channel_id: str
    retention_days: int
    legal_hold: bool
    documentation_policy: str


def policy_for(session: Session, channel_id: str) -> RetentionPolicy:
    row = session.execute(
        text(
            "SELECT retention_days, legal_hold, documentation_policy "
            "FROM message_retention_policies WHERE channel_id = :c"
        ),
        {"c": uuid.UUID(channel_id)},
    ).first()
    if row is None:
        return RetentionPolicy(channel_id, MESSAGE_RETENTION_DAYS, False, "task_threads")
    return RetentionPolicy(channel_id, int(row[0]), bool(row[1]), str(row[2]))


def set_retention(
    session: Session,
    channel_id: str,
    retention_days: int,
    legal_hold: bool,
    changed_by: str,
    clock: Clock,
    *,
    documentation_policy: str = "task_threads",
    correlation_id: str = "-",
    workspace_id: str | None = None,
) -> RetentionPolicy:
    """Upsert the channel policy (mirrored to ``channels`` for display) and audit the change."""
    if not 1 <= int(retention_days) <= MAX_RETENTION_DAYS:
        raise RetentionError(
            "RETENTION_DAYS_INVALID", f"{retention_days} not in 1..{MAX_RETENTION_DAYS}"
        )
    if documentation_policy not in DOCUMENTATION_POLICIES:
        raise RetentionError("DOCUMENTATION_POLICY_INVALID", documentation_policy)
    ch = uuid.UUID(channel_id)
    exists = session.execute(
        text("SELECT workspace_id FROM channels WHERE id = :c"), {"c": ch}
    ).first()
    if exists is None:
        raise RetentionError("CHANNEL_NOT_FOUND", channel_id)
    before = policy_for(session, channel_id)
    session.execute(
        text(
            "INSERT INTO message_retention_policies (channel_id, retention_days, legal_hold, "
            "documentation_policy, changed_by, updated_at) VALUES (:c, :d, :h, :p, :by, :now) "
            "ON CONFLICT (channel_id) DO UPDATE SET retention_days = EXCLUDED.retention_days, "
            "legal_hold = EXCLUDED.legal_hold, "
            "documentation_policy = EXCLUDED.documentation_policy, "
            "changed_by = EXCLUDED.changed_by, updated_at = EXCLUDED.updated_at"
        ),
        {
            "c": ch,
            "d": int(retention_days),
            "h": bool(legal_hold),
            "p": documentation_policy,
            "by": uuid.UUID(changed_by),
            "now": clock.now(),
        },
    )
    session.execute(
        text("UPDATE channels SET retention_days = :d, legal_hold = :h WHERE id = :c"),
        {"d": int(retention_days), "h": bool(legal_hold), "c": ch},
    )
    append_audit(
        session,
        action="channel.retention_set",
        target_type="channel",
        target_id=channel_id,
        result="OK",
        actor_label=changed_by,
        correlation_id=correlation_id,
        workspace_id=uuid.UUID(workspace_id) if workspace_id else uuid.UUID(str(exists[0])),
        actor_account_id=uuid.UUID(changed_by),
        metadata={
            "before": {"retention_days": before.retention_days, "legal_hold": before.legal_hold},
            "after": {"retention_days": int(retention_days), "legal_hold": bool(legal_hold)},
            "documentation_policy": documentation_policy,
        },
        clock=clock,
    )
    return RetentionPolicy(channel_id, int(retention_days), bool(legal_hold), documentation_policy)


@dataclass
class RetentionReport:
    expired: int = 0
    destroyed: int = 0
    skipped_legal_hold: int = 0
    tombstones: list[str] = field(default_factory=list)


def _append_tombstone(
    session: Session,
    *,
    message_id: str,
    channel_id: uuid.UUID,
    reason: str,
    key_ref: str | None,
    deleted_at: dt.datetime,
) -> str:
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('message_tombstones_chain'))"))
    previous = last_hash(session, MESSAGE_TOMBSTONE_CHAIN)
    fields = {
        "message_id": message_id,
        "channel_id": channel_id,
        "reason": reason,
        "key_ref": key_ref,
        "deleted_at": deleted_at,
    }
    content_hash = chain_hash(hashed_row_fields(MESSAGE_TOMBSTONE_CHAIN, fields), previous)
    session.execute(
        text(
            "INSERT INTO message_tombstones (message_id, channel_id, reason, key_ref, deleted_at, "
            "previous_hash, content_hash) VALUES (:m, :c, :r, :k, :d, :p, :h)"
        ),
        {
            **fields,
            "m": message_id,
            "c": channel_id,
            "r": reason,
            "k": key_ref,
            "d": deleted_at,
            "p": previous,
            "h": content_hash,
        },
    )
    return content_hash


def retention_job(
    session: Session,
    crypto: EnvelopeCrypto,
    clock: Clock,
    *,
    workspace_id: str,
    actor_account_id: str,
    batch: int = 1000,
) -> RetentionReport:
    """Destroy DEKs of expired messages and tombstone them; skip legal holds; never delete rows."""
    now = clock.now()
    report = RetentionReport()
    rows = session.execute(
        text(
            "SELECT m.message_id, m.channel_id, m.body_key_ref, m.received_at, m.legal_hold, "
            "COALESCE(p.retention_days, :default_days) AS retention_days, "
            "COALESCE(p.legal_hold, false) AS channel_hold "
            "FROM messages m LEFT JOIN message_retention_policies p ON p.channel_id = m.channel_id "
            "WHERE m.workspace_id = :ws AND m.deleted_at IS NULL "
            "ORDER BY m.received_at, m.message_id LIMIT :lim FOR UPDATE OF m SKIP LOCKED"
        ),
        {"ws": uuid.UUID(workspace_id), "default_days": MESSAGE_RETENTION_DAYS, "lim": batch},
    ).all()
    for (
        message_id,
        channel_id,
        key_ref,
        received_at,
        msg_hold,
        retention_days,
        channel_hold,
    ) in rows:
        expires_at = received_at + dt.timedelta(days=int(retention_days))
        if expires_at >= now:
            continue
        report.expired += 1
        if bool(channel_hold) or bool(msg_hold):
            report.skipped_legal_hold += 1
            continue
        if key_ref:
            try:
                crypto.destroy(session, str(key_ref), actor_account_id, "RETENTION")
            except CryptoError as exc:
                if exc.code != "KEY_ALREADY_DESTROYED":
                    raise
        content_hash = _append_tombstone(
            session,
            message_id=str(message_id),
            channel_id=channel_id,
            reason="RETENTION",
            key_ref=str(key_ref) if key_ref else None,
            deleted_at=now,
        )
        session.execute(
            text(
                "UPDATE messages SET deleted_at = :now, body_redacted = :marker, "
                "tombstone_ref = :t "
                "WHERE message_id = :m"
            ),
            {"now": now, "marker": REDACTED_BY_RETENTION, "t": content_hash, "m": message_id},
        )
        report.destroyed += 1
        report.tombstones.append(content_hash)
    return report
