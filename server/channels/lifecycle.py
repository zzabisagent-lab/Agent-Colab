"""Channel lifecycle: soft delete after archive and mapping checks (P2-09; spec §8.1, §11.2).

Deletion never removes rows. It requires ``archived`` status, no enabled Telegram Bridge, and no
open Task in the channel; it sets ``status = 'deleted'`` and ``deleted_at``, and writes an audit
row. Thread bindings, channel posts, Artifact links, Documents, and message mappings remain
readable so provenance stays intact; a hard delete is the P4-11 administrator workflow. Spec §9.3
defines no channel-deletion Event type, so the deletion is recorded by status plus AuditEvent.
"""

from __future__ import annotations

import uuid
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
from server.observability.audit import append_audit

OPEN_TASK_STATES = (
    "OPEN",
    "DELEGATED",
    "ACCEPTED",
    "RUNNING",
    "WAITING",
    "IMPLEMENTED",
    "VERIFYING",
    "VERIFIED",
    "CANCEL_REQUESTED",
)


@dataclass(frozen=True)
class DeleteChannel(Command):
    channel_id: str
    reason_code: str = "RETIRED"
    idempotency_scope: str = "channel:delete"


def _table_exists(session: Session, name: str) -> bool:
    return session.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar() is not None


def deletion_blockers(session: Session, channel_uuid: uuid.UUID) -> list[str]:
    """Mapping checks that must be clear before a soft delete (development plan §8.1)."""
    blockers: list[str] = []
    if _table_exists(session, "telegram_bridges"):
        enabled = session.execute(
            text(
                "SELECT count(*) FROM telegram_bridges WHERE channel_id = :c AND status = 'enabled'"
            ),
            {"c": channel_uuid},
        ).scalar_one()
        if int(enabled):
            blockers.append("CHANNEL_HAS_ENABLED_BRIDGE")
    open_tasks = session.execute(
        text("SELECT count(*) FROM tasks_projection WHERE channel_id = :c AND status = ANY(:open)"),
        {"c": channel_uuid, "open": list(OPEN_TASK_STATES)},
    ).scalar_one()
    if int(open_tasks):
        blockers.append("CHANNEL_HAS_OPEN_TASKS")
    return blockers


def references(session: Session, channel_uuid: uuid.UUID) -> dict[str, int]:
    """Counts of rows that keep referring to a channel (all must survive a soft delete)."""
    counts = {
        "thread_bindings": int(
            session.execute(
                text(
                    "SELECT count(*) FROM thread_bindings tb JOIN channels c "
                    "ON c.provider_instance_id = tb.provider_instance_id "
                    "AND c.external_channel_id = tb.external_channel_id WHERE c.id = :c"
                ),
                {"c": channel_uuid},
            ).scalar_one()
        ),
        "channel_posts": int(
            session.execute(
                text(
                    "SELECT count(*) FROM channel_posts cp JOIN channels c "
                    "ON c.external_channel_id = cp.external_channel_id WHERE c.id = :c"
                ),
                {"c": channel_uuid},
            ).scalar_one()
        ),
        "artifact_links": int(
            session.execute(
                text(
                    "SELECT count(*) FROM artifact_links al JOIN tasks_projection t "
                    "ON t.task_id = al.subject_id AND al.subject_type = 'task' "
                    "WHERE t.channel_id = :c"
                ),
                {"c": channel_uuid},
            ).scalar_one()
        ),
        "documents": int(
            session.execute(
                text(
                    "SELECT count(*) FROM documents d JOIN tasks_projection t "
                    "ON t.task_id = d.source_id AND d.source_type = 'task' WHERE t.channel_id = :c"
                ),
                {"c": channel_uuid},
            ).scalar_one()
        ),
    }
    if _table_exists(session, "message_mappings"):
        counts["message_mappings"] = int(
            session.execute(
                text(
                    "SELECT count(*) FROM message_mappings mm JOIN telegram_bridges b "
                    "ON b.bridge_id = mm.bridge_id WHERE b.channel_id = :c"
                ),
                {"c": channel_uuid},
            ).scalar_one()
        )
    return counts


@handles(DeleteChannel)
def delete_channel(cmd: DeleteChannel, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_archive")
    ws = uuid.UUID(ctx.workspace_id)
    row = (
        ctx.session.execute(
            text(
                "SELECT id, status, deleted_at FROM channels WHERE channel_id = :c "
                "AND workspace_id = :ws FOR UPDATE"
            ),
            {"c": cmd.channel_id, "ws": ws},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CommandError("CHANNEL_NOT_FOUND", cmd.channel_id, status=404)
    channel_uuid = uuid.UUID(str(row["id"]))
    if row["status"] == "deleted":
        return CommandResult(cmd.channel_id, "", 0, "channel", replayed=True)
    if row["status"] != "archived":
        raise CommandError(
            "CHANNEL_NOT_ARCHIVED", f"status {row['status']}; archive first", status=409
        )
    blockers = deletion_blockers(ctx.session, channel_uuid)
    if blockers:
        raise CommandError(
            "CHANNEL_DELETE_BLOCKED", ", ".join(blockers), status=409, extra={"blockers": blockers}
        )
    ctx.session.execute(
        text("UPDATE channels SET status = 'deleted', deleted_at = :now WHERE id = :c"),
        {"now": ctx.clock.now(), "c": channel_uuid},
    )
    kept = references(ctx.session, channel_uuid)
    append_audit(
        ctx.session,
        action="channel.delete",
        target_type="channel",
        target_id=cmd.channel_id,
        result="OK",
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=ws,
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata={"reason_code": cmd.reason_code, "soft_delete": True, "references_kept": kept},
        clock=ctx.clock,
    )
    return CommandResult(cmd.channel_id, "", 0, "channel", data={"references_kept": kept})


def channel_view(
    session: Session, workspace_id: uuid.UUID, channel_id: str
) -> dict[str, Any] | None:
    """Deleted channels stay queryable by id with their status (soft delete only)."""
    row = (
        session.execute(
            text(
                "SELECT channel_id, status, archived_at, deleted_at, channel_type, display_name "
                "FROM channels WHERE channel_id = :c AND workspace_id = :ws"
            ),
            {"c": channel_id, "ws": workspace_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None
