"""Self-service notification preferences on the command bus (development plan §7A.2 ``notify``)."""

from __future__ import annotations

from dataclasses import dataclass

from server.application.bus import (
    Command,
    CommandContext,
    CommandResult,
    handles,
    require_permission,
)
from server.notifications.routing import get_preferences, set_preferences


@dataclass(frozen=True)
class SetNotificationPreferences(Command):
    muted: bool | None = None
    digest: bool | None = None
    idempotency_scope: str = "notification:preferences"


@dataclass(frozen=True)
class MuteNotifications(Command):
    idempotency_scope: str = "notification:mute"


@dataclass(frozen=True)
class UnmuteNotifications(Command):
    idempotency_scope: str = "notification:unmute"


@dataclass(frozen=True)
class SetDigest(Command):
    enabled: bool = True
    idempotency_scope: str = "notification:digest"


def _apply(ctx: CommandContext, muted: bool | None, digest: bool | None) -> CommandResult:
    require_permission(ctx, "notification.self", action="command:notify.mute")
    prefs = set_preferences(
        ctx.session,
        ctx.principal.account_uuid,
        muted=muted,
        digest=digest,
        clock=ctx.clock,
        correlation_id=ctx.correlation_id,
        workspace_id=ctx.workspace_id,
        actor_label=ctx.principal.account_id,
    )
    return CommandResult(
        ctx.principal.account_id,
        "",
        0,
        "notification_preference",
        data={"muted": prefs.muted, "digest": prefs.digest},
    )


@handles(SetNotificationPreferences)
def handle_set(cmd: SetNotificationPreferences, ctx: CommandContext) -> CommandResult:
    return _apply(ctx, cmd.muted, cmd.digest)


@handles(MuteNotifications)
def handle_mute(cmd: MuteNotifications, ctx: CommandContext) -> CommandResult:
    return _apply(ctx, True, None)


@handles(UnmuteNotifications)
def handle_unmute(cmd: UnmuteNotifications, ctx: CommandContext) -> CommandResult:
    return _apply(ctx, False, None)


@handles(SetDigest)
def handle_digest(cmd: SetDigest, ctx: CommandContext) -> CommandResult:
    return _apply(ctx, None, cmd.enabled)


def current_preferences(ctx: CommandContext) -> dict[str, bool]:
    prefs = get_preferences(ctx.session, ctx.principal.account_uuid)
    return {"muted": prefs.muted, "digest": prefs.digest}
