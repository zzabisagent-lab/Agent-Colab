"""Message retention commands on the command bus (P2-15) and the provenance read helper used by
the Documentation Service (development plan §7H: deleted Messages are marked
``REDACTED_BY_RETENTION`` in provenance)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.channels.ingestion import REDACTED_BY_RETENTION
from server.channels.retention import RetentionError, retention_job, set_retention
from server.secrets.envelope import EnvelopeCrypto


@dataclass(frozen=True)
class SetChannelRetention(Command):
    channel_id: str  # channels.id (uuid string)
    retention_days: int
    legal_hold: bool = False
    documentation_policy: str = "task_threads"
    idempotency_scope: str = "channel:retention"


@dataclass(frozen=True)
class RunRetention(Command):
    idempotency_scope: str = "channel:retention_job"


@handles(SetChannelRetention)
def set_channel_retention(cmd: SetChannelRetention, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", channel_id=cmd.channel_id)
    try:
        policy = set_retention(
            ctx.session,
            cmd.channel_id,
            cmd.retention_days,
            cmd.legal_hold,
            ctx.principal.account_uuid,
            ctx.clock,
            documentation_policy=cmd.documentation_policy,
            correlation_id=ctx.correlation_id,
            workspace_id=ctx.workspace_id,
        )
    except RetentionError as exc:
        status = 404 if exc.code == "CHANNEL_NOT_FOUND" else 422
        raise CommandError(exc.code, exc.detail, status=status) from exc
    return CommandResult(
        resource_id=cmd.channel_id,
        event_id="",
        aggregate_seq=0,
        aggregate_type="channel",
        data={
            "retention_days": policy.retention_days,
            "legal_hold": policy.legal_hold,
            "documentation_policy": policy.documentation_policy,
        },
    )


@handles(RunRetention)
def run_retention(cmd: RunRetention, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "ops.manage")
    crypto = ctx.extras.get("crypto")
    if not isinstance(crypto, EnvelopeCrypto):
        raise CommandError("CRYPTO_UNAVAILABLE", "no envelope crypto configured", status=503)
    report = retention_job(
        ctx.session,
        crypto,
        ctx.clock,
        workspace_id=ctx.workspace_id,
        actor_account_id=ctx.principal.account_uuid,
    )
    return CommandResult(
        resource_id=ctx.workspace_id,
        event_id="",
        aggregate_seq=0,
        aggregate_type="channel",
        data={
            "expired": report.expired,
            "destroyed": report.destroyed,
            "skipped_legal_hold": report.skipped_legal_hold,
        },
    )


def provenance_for(session: Session, conversation_id: str) -> list[dict[str, Any]]:
    """Message references for document provenance; deleted ones carry the retention mark."""
    rows = session.execute(
        text(
            "SELECT message_id, source, source_message_id, sender_label, received_at, deleted_at, "
            "tombstone_ref, body_redacted FROM messages WHERE conversation_id = :c "
            "ORDER BY received_at, message_id"
        ),
        {"c": conversation_id},
    ).all()
    out: list[dict[str, Any]] = []
    for (
        message_id,
        source,
        source_message_id,
        sender_label,
        received_at,
        deleted_at,
        tomb,
        body,
    ) in rows:
        deleted = deleted_at is not None
        out.append(
            {
                "message_id": str(message_id),
                "source": str(source),
                "source_message_id": str(source_message_id),
                "sender_label": str(sender_label),
                "received_at": received_at.isoformat(),
                "status": str(body)
                if deleted and str(body)
                else ("available" if not deleted else REDACTED_BY_RETENTION),
                "tombstone_ref": tomb,
            }
        )
    return out
