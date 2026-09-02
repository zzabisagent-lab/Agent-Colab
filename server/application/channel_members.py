"""Channel membership and document-template commands on the bus (P2-02).

Membership changes are part of the channel configuration: each one appends a
``CHANNEL_CONFIGURED`` Event (spec §9.3) carrying the change in its payload, updates
``channel_members`` in the same transaction, and writes a redacted audit row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.channels import members as mem
from server.events.store import AppendRequest
from server.observability.audit import append_audit


@dataclass(frozen=True)
class AddChannelMember(Command):
    channel_id: str
    account_id: str
    permissions: tuple[str, ...] = ("read", "write")
    idempotency_scope: str = "channel:member_add"


@dataclass(frozen=True)
class RemoveChannelMember(Command):
    channel_id: str
    account_id: str
    idempotency_scope: str = "channel:member_remove"


@dataclass(frozen=True)
class SetMemberPermissions(Command):
    channel_id: str
    account_id: str
    permissions: tuple[str, ...]
    idempotency_scope: str = "channel:member_permissions"


@dataclass(frozen=True)
class SetChannelDocumentTemplate(Command):
    channel_id: str
    documentation_template: str | None
    idempotency_scope: str = "channel:document_template"


@dataclass(frozen=True)
class ListChannelMembers(Command):
    channel_id: str
    extra: dict[str, Any] = field(default_factory=dict)
    idempotency_scope: str = "channel:members_read"


def _ws(ctx: CommandContext) -> uuid.UUID:
    return uuid.UUID(ctx.workspace_id)


def _configured_event(
    ctx: CommandContext, cmd: Command, ref: mem.ChannelRef, change: dict[str, Any]
) -> Any:
    stream = ctx.store.stream(ctx.workspace_id, "channel", ref.channel_id)
    channel_type = ctx.session.execute(
        __import__("sqlalchemy").text("SELECT channel_type FROM channels WHERE id = :c"),
        {"c": ref.id},
    ).scalar_one()
    return ctx.store.append(
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type="channel",
            aggregate_id=ref.channel_id,
            type="CHANNEL_CONFIGURED",
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            expected_seq=len(stream) + 1,
            channel_id=str(ref.id),
            payload={
                "channel_id": ref.channel_id,
                "channel_type": str(channel_type),
                "policy_version": "policy-v1",
                "change": change,
            },
        )
    )


def _audit(ctx: CommandContext, action: str, ref: mem.ChannelRef, **meta: Any) -> None:
    append_audit(
        ctx.session,
        action=action,
        target_type="channel",
        target_id=ref.channel_id,
        result="OK",
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=_ws(ctx),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata=meta,
        clock=ctx.clock,
    )


def _ref(ctx: CommandContext, channel_id: str) -> mem.ChannelRef:
    try:
        ref = mem.channel_ref(ctx.session, _ws(ctx), channel_id)
    except mem.MembershipError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    if ref.status == "deleted":
        raise CommandError("CHANNEL_DELETED", channel_id, status=409)
    return ref


def _account(ctx: CommandContext, account_id: str) -> tuple[uuid.UUID, str]:
    try:
        return mem.account_ref(ctx.session, _ws(ctx), account_id)
    except mem.MembershipError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc


@handles(AddChannelMember)
def add_channel_member(cmd: AddChannelMember, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_configure")
    ref = _ref(ctx, cmd.channel_id)
    try:
        perms = mem.validate_permissions(cmd.permissions)
    except mem.MembershipError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    account_uuid, account_type = _account(ctx, cmd.account_id)
    change = {
        "kind": "member_added",
        "account_id": cmd.account_id,
        "account_type": account_type,
        "permissions": list(perms),
    }
    res = _configured_event(ctx, cmd, ref, change)
    if not res.replayed:
        mem.add_member(ctx.session, ref.id, account_uuid, perms)
        _audit(ctx, "channel.member_add", ref, account_id=cmd.account_id, permissions=list(perms))
    return CommandResult(ref.channel_id, res.event_id, res.aggregate_seq, "channel", res.replayed)


@handles(RemoveChannelMember)
def remove_channel_member(cmd: RemoveChannelMember, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_configure")
    ref = _ref(ctx, cmd.channel_id)
    account_uuid, _ = _account(ctx, cmd.account_id)
    if not mem.is_active_member(ctx.session, ref.id, account_uuid):
        raise CommandError("MEMBER_NOT_FOUND", cmd.account_id, status=404)
    change = {"kind": "member_removed", "account_id": cmd.account_id}
    res = _configured_event(ctx, cmd, ref, change)
    if not res.replayed:
        mem.remove_member(ctx.session, ref.id, account_uuid)
        _audit(ctx, "channel.member_remove", ref, account_id=cmd.account_id)
    return CommandResult(ref.channel_id, res.event_id, res.aggregate_seq, "channel", res.replayed)


@handles(SetMemberPermissions)
def set_member_permissions(cmd: SetMemberPermissions, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_configure")
    ref = _ref(ctx, cmd.channel_id)
    try:
        perms = mem.validate_permissions(cmd.permissions)
    except mem.MembershipError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    account_uuid, _ = _account(ctx, cmd.account_id)
    if not mem.is_active_member(ctx.session, ref.id, account_uuid):
        raise CommandError("MEMBER_NOT_FOUND", cmd.account_id, status=404)
    change = {
        "kind": "member_permissions",
        "account_id": cmd.account_id,
        "permissions": list(perms),
    }
    res = _configured_event(ctx, cmd, ref, change)
    if not res.replayed:
        mem.set_permissions(ctx.session, ref.id, account_uuid, perms)
        _audit(
            ctx,
            "channel.member_permissions",
            ref,
            account_id=cmd.account_id,
            permissions=list(perms),
        )
    return CommandResult(ref.channel_id, res.event_id, res.aggregate_seq, "channel", res.replayed)


@handles(SetChannelDocumentTemplate)
def set_channel_document_template(
    cmd: SetChannelDocumentTemplate, ctx: CommandContext
) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_configure")
    ref = _ref(ctx, cmd.channel_id)
    change = {
        "kind": "documentation_template",
        "documentation_template": cmd.documentation_template,
    }
    res = _configured_event(ctx, cmd, ref, change)
    if not res.replayed:
        mem.set_document_template(ctx.session, ref.id, cmd.documentation_template)
        _audit(ctx, "channel.document_template", ref, template=cmd.documentation_template)
    return CommandResult(ref.channel_id, res.event_id, res.aggregate_seq, "channel", res.replayed)


def members_of(ctx: CommandContext, channel_id: str) -> list[dict[str, Any]]:
    """Read model: active members with permissions (channel members or managers may read)."""
    ref = _ref(ctx, channel_id)
    actor_uuid = uuid.UUID(ctx.principal.account_uuid)
    if not mem.is_active_member(ctx.session, ref.id, actor_uuid):
        require_permission(ctx, "channel.manage", action="api:channel_configure")
    return [
        {
            "account_id": m.account_id,
            "account_type": m.account_type,
            "permissions": list(m.permissions),
            "status": m.status,
        }
        for m in mem.list_members(ctx.session, ref.id)
        if m.status == "active"
    ]
