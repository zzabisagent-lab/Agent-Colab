"""Telegram Bridge commands on the common command bus (P2-05 admin; spec §10, §11.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.channels import bridge_admin
from server.channels.bridge_admin import BridgeAdminError
from server.channels.telegram.client import TelegramClient
from server.events.store import AppendRequest, EventStoreError
from server.observability.audit import append_audit


def _translate(exc: BridgeAdminError) -> CommandError:
    return CommandError(exc.code, exc.detail, status=exc.status)


def _has_permission(ctx: CommandContext, permission: str, **scope: Any) -> bool:
    try:
        require_permission(ctx, permission, **scope)
    except CommandError:
        return False
    return True


def _bridge_event(
    ctx: CommandContext,
    event_type: str,
    bridge_id: str,
    channel_id: str,
    scope: str,
    extra: dict[str, Any],
) -> tuple[str, int]:
    try:
        res = ctx.store.append(
            AppendRequest(
                workspace_id=ctx.workspace_id,
                aggregate_type="bridge",
                aggregate_id=bridge_id,
                type=event_type,
                actor_account_id=ctx.principal.account_uuid,
                correlation_id=ctx.correlation_id,
                idempotency_scope=scope,
                idempotency_key=ctx.idempotency_key,
                payload={"bridge_id": bridge_id, "channel_id": channel_id, **extra},
            )
        )
    except EventStoreError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc
    return res.event_id, res.aggregate_seq


def _audit(ctx: CommandContext, action: str, bridge_id: str, metadata: dict[str, Any]) -> None:
    import uuid

    append_audit(
        ctx.session,
        action=action,
        target_type="bridge",
        target_id=bridge_id,
        result="OK",
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=uuid.UUID(ctx.workspace_id),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata=metadata,
        clock=ctx.clock,
    )


@dataclass(frozen=True)
class CreateBridge(Command):
    channel_id: str
    provider_instance_id: str
    telegram_chat_id: str
    direction: str = "bidirectional"
    telegram_thread_id: int | None = None
    thread_mode: str = "topic_per_root"
    content_policy: dict[str, Any] = field(default_factory=dict)
    redaction_policy: dict[str, Any] = field(default_factory=dict)
    identity_display: dict[str, Any] = field(default_factory=dict)
    rate_limit: dict[str, Any] = field(default_factory=dict)
    allow_commands: bool = False
    admin_exception: bool = False
    admin_exception_reason: str | None = None
    bridge_id: str | None = None
    idempotency_scope: str = "bridge:create"


@handles(CreateBridge)
def create_bridge(cmd: CreateBridge, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "bridge.manage", channel_id=cmd.channel_id)
    exception_allowed = cmd.admin_exception and _has_permission(ctx, "admin.settings")
    config: dict[str, Any] = {
        "channel_id": cmd.channel_id,
        "provider_instance_id": cmd.provider_instance_id,
        "telegram_chat_id": cmd.telegram_chat_id,
        "direction": cmd.direction,
        "thread_mode": cmd.thread_mode,
        "telegram_thread_id": cmd.telegram_thread_id,
        "content_policy": cmd.content_policy,
        "redaction_policy": cmd.redaction_policy,
        "identity_display": cmd.identity_display,
        "rate_limit": cmd.rate_limit,
        "allow_commands": cmd.allow_commands,
        "admin_exception": cmd.admin_exception,
        "admin_exception_reason": cmd.admin_exception_reason,
    }
    if cmd.bridge_id:
        config["bridge_id"] = cmd.bridge_id
    try:
        view = bridge_admin.create_bridge(
            ctx.session,
            workspace_id=ctx.workspace_id,
            config=config,
            created_by=ctx.principal.account_uuid,
            now=ctx.clock.now(),
            exception_allowed=exception_allowed,
        )
    except BridgeAdminError as exc:
        raise _translate(exc) from exc
    if cmd.admin_exception:
        _audit(
            ctx,
            "bridge.admin_exception",
            view.bridge_id,
            {"reason": cmd.admin_exception_reason or ""},
        )
    event_id, seq = _bridge_event(
        ctx,
        "TELEGRAM_BRIDGE_ENABLED",
        view.bridge_id,
        cmd.channel_id,
        "bridge:create",
        {"direction": view.direction},
    )
    return CommandResult(view.bridge_id, event_id, seq, "bridge", False, {"status": view.status})


@dataclass(frozen=True)
class UpdateBridge(Command):
    bridge_id: str
    changes: dict[str, Any] = field(default_factory=dict)
    idempotency_scope: str = "bridge:update"


@handles(UpdateBridge)
def update_bridge(cmd: UpdateBridge, ctx: CommandContext) -> CommandResult:
    try:
        current = bridge_admin.get_bridge(ctx.session, cmd.bridge_id)
        require_permission(ctx, "bridge.manage", channel_id=current.channel_id)
        view = bridge_admin.update_bridge(ctx.session, cmd.bridge_id, cmd.changes, ctx.clock.now())
    except BridgeAdminError as exc:
        raise _translate(exc) from exc
    _audit(ctx, "bridge.updated", cmd.bridge_id, {"fields": sorted(cmd.changes)})
    return CommandResult(
        view.bridge_id, "", 0, "bridge", False, {"status": view.status, "direction": view.direction}
    )


@dataclass(frozen=True)
class EnableBridge(Command):
    bridge_id: str
    idempotency_scope: str = "bridge:enable"


@dataclass(frozen=True)
class DisableBridge(Command):
    bridge_id: str
    idempotency_scope: str = "bridge:disable"


def _set_status(
    ctx: CommandContext, bridge_id: str, status: str, event_type: str, scope: str
) -> CommandResult:
    try:
        current = bridge_admin.get_bridge(ctx.session, bridge_id)
        require_permission(ctx, "bridge.manage", channel_id=current.channel_id)
        view = bridge_admin.set_status(ctx.session, bridge_id, status, ctx.clock.now())
    except BridgeAdminError as exc:
        raise _translate(exc) from exc
    extra = {"direction": view.direction} if event_type == "TELEGRAM_BRIDGE_ENABLED" else {}
    event_id, seq = _bridge_event(ctx, event_type, bridge_id, view.channel_id, scope, extra)
    return CommandResult(bridge_id, event_id, seq, "bridge", False, {"status": view.status})


@handles(EnableBridge)
def enable_bridge(cmd: EnableBridge, ctx: CommandContext) -> CommandResult:
    return _set_status(
        ctx, cmd.bridge_id, "enabled", "TELEGRAM_BRIDGE_ENABLED", cmd.idempotency_scope
    )


@handles(DisableBridge)
def disable_bridge(cmd: DisableBridge, ctx: CommandContext) -> CommandResult:
    return _set_status(
        ctx, cmd.bridge_id, "disabled", "TELEGRAM_BRIDGE_DISABLED", cmd.idempotency_scope
    )


@dataclass(frozen=True)
class TestBridge(Command):
    bridge_id: str
    idempotency_scope: str = "bridge:test"


@handles(TestBridge)
def test_bridge(cmd: TestBridge, ctx: CommandContext) -> CommandResult:
    client = ctx.extras.get("telegram_client")
    if client is None:
        raise CommandError(
            "TELEGRAM_CLIENT_UNAVAILABLE", "no Telegram client configured", status=503
        )
    try:
        current = bridge_admin.get_bridge(ctx.session, cmd.bridge_id)
        require_permission(ctx, "bridge.manage", channel_id=current.channel_id)
        result = bridge_admin.test_bridge(ctx.session, cast(TelegramClient, client), cmd.bridge_id)
    except BridgeAdminError as exc:
        raise _translate(exc) from exc
    _audit(ctx, "bridge.tested", cmd.bridge_id, {"message_id": result["message_id"]})
    return CommandResult(cmd.bridge_id, "", 0, "bridge", False, result)


def bridge_status(ctx: CommandContext, bridge_id: str) -> dict[str, Any]:
    try:
        current = bridge_admin.get_bridge(ctx.session, bridge_id)
        require_permission(ctx, "bridge.manage", channel_id=current.channel_id)
        return bridge_admin.bridge_status(ctx.session, bridge_id)
    except BridgeAdminError as exc:
        raise _translate(exc) from exc


def list_bridges(ctx: CommandContext, channel_id: str) -> list[dict[str, Any]]:
    require_permission(ctx, "bridge.manage", channel_id=channel_id)
    return [
        v.__dict__ for v in bridge_admin.list_bridges(ctx.session, ctx.workspace_id, channel_id)
    ]
