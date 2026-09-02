"""Channel and Mattermost provider commands on the bus (P2-01; spec §8.1, plan §7A.1)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.channels import templates as tpl
from server.channels.mattermost import provider as prov
from server.channels.mattermost.client import MattermostError
from server.events.store import AppendRequest
from server.observability.audit import append_audit

CHANNEL_TYPES = ("work", "brainstorm", "approval", "ops", "custom")


@dataclass(frozen=True)
class RegisterProviderInstance(Command):
    """Register a Mattermost provider instance (base URL + team); probes identity display."""

    base_url: str
    team_name: str
    team_id: str
    bot_user_id: str | None = None
    identity_display: str | None = None  # override | prefix; probed from config when None
    idempotency_scope: str = "channel:provider_register"


@dataclass(frozen=True)
class RegisterSlashCommand(Command):
    provider_instance_id: str
    callback_url: str
    trigger: str = "colab"
    idempotency_scope: str = "channel:command_register"


@dataclass(frozen=True)
class ImportChannel(Command):
    provider_instance_id: str
    external_channel_id: str
    channel_type: str = "work"
    display_name: str | None = None
    template_id: str | None = None  # defaults to the channel_type template; custom = none
    language: str | None = None
    idempotency_scope: str = "channel:import"


@dataclass(frozen=True)
class ConfigureChannel(Command):
    channel_id: str
    policy: dict[str, Any] | None = None
    documentation_template: str | None = None
    language: str | None = None
    retention_days: int | None = None
    legal_hold: bool | None = None
    template_id: str | None = None
    idempotency_scope: str = "channel:configure"


@dataclass(frozen=True)
class ArchiveChannel(Command):
    channel_id: str
    idempotency_scope: str = "channel:archive"


@dataclass(frozen=True)
class CreateChannelTemplate(Command):
    template_id: str
    name: str
    channel_type: str
    definition: dict[str, Any] = field(default_factory=dict)
    idempotency_scope: str = "channel:template_create"


@dataclass(frozen=True)
class UpdateChannelTemplate(Command):
    template_id: str
    definition: dict[str, Any] = field(default_factory=dict)
    name: str | None = None
    idempotency_scope: str = "channel:template_update"


@dataclass(frozen=True)
class DeleteChannelTemplate(Command):
    template_id: str
    idempotency_scope: str = "channel:template_delete"


def _ws(ctx: CommandContext) -> uuid.UUID:
    return uuid.UUID(ctx.workspace_id)


def _audit(ctx: CommandContext, action: str, target_type: str, target_id: str, **meta: Any) -> None:
    append_audit(
        ctx.session,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result="OK",
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=_ws(ctx),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata=meta,
        clock=ctx.clock,
    )


@handles(RegisterProviderInstance)
def register_provider_instance(cmd: RegisterProviderInstance, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:provider_instance_create")
    pid = prov.provider_instance_id_for(cmd.base_url, cmd.team_name)
    existing = prov.load_instance(ctx.session, pid)
    if existing is not None:
        return CommandResult(
            pid,
            "",
            0,
            "provider_instance",
            replayed=True,
            data={"identity_display": existing.identity_display},
        )
    display = cmd.identity_display
    if display is None:
        try:
            client = prov.admin_client_for(
                prov.ProviderInstance(
                    uuid.uuid4(),
                    pid,
                    _ws(ctx),
                    cmd.base_url,
                    cmd.team_id,
                    cmd.team_name,
                    cmd.bot_user_id,
                    "prefix",
                    "active",
                )
            )
            display = prov.detect_identity_display(client.get_config())
        except Exception:
            display = "prefix"
    if display not in ("override", "prefix"):
        raise CommandError("PROVIDER_DISPLAY_INVALID", display, status=400)
    ctx.session.execute(
        text(
            "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
            "base_url, "
            "team_or_bot_ref, bot_user_id, identity_display, config) VALUES (:id, :p, :ws, "
            "'mattermost', "
            ":url, :team, :bot, :disp, CAST(:cfg AS jsonb))"
        ),
        {
            "id": uuid.uuid4(),
            "p": pid,
            "ws": _ws(ctx),
            "url": cmd.base_url.rstrip("/"),
            "team": cmd.team_id,
            "bot": cmd.bot_user_id,
            "disp": display,
            "cfg": json.dumps({"team_name": cmd.team_name}),
        },
    )
    tpl.sync_defaults(ctx.session, _ws(ctx))
    _audit(ctx, "provider.register", "provider_instance", pid, identity_display=display)
    return CommandResult(pid, "", 0, "provider_instance", data={"identity_display": display})


@handles(RegisterSlashCommand)
def register_slash_command(cmd: RegisterSlashCommand, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:provider_instance_create")
    inst = prov.load_instance(ctx.session, cmd.provider_instance_id)
    if inst is None:
        raise CommandError("PROVIDER_INSTANCE_UNKNOWN", cmd.provider_instance_id, status=404)
    try:
        client = prov.admin_client_for(inst)
        existing = [
            c for c in client.list_commands(inst.team_id) if c.get("trigger") == cmd.trigger
        ]
        if existing:
            current = dict(existing[0])
            current.update({"url": cmd.callback_url, "method": "P", "auto_complete": True})
            client.update_command(current)  # re-point the existing trigger at this instance
            created = client.regen_command_token(str(existing[0]["id"]))
        else:
            created = client.create_command(inst.team_id, cmd.trigger, cmd.callback_url)
    except (MattermostError, prov.ProviderError) as exc:
        code = getattr(exc, "code", "PROVIDER_ERROR")
        raise CommandError("PROVIDER_ERROR", f"{code}: {exc.detail}", status=502) from exc
    token = str(created.get("token", ""))
    if not token:
        raise CommandError("PROVIDER_COMMAND_TOKEN_MISSING", "no token returned", status=502)
    prov.store_command_token(ctx.session, inst.id, cmd.trigger, token, str(created.get("id", "")))
    _audit(
        ctx,
        "provider.command_register",
        "provider_instance",
        inst.provider_instance_id,
        trigger=cmd.trigger,
        command_ref=str(created.get("id", "")),
    )
    return CommandResult(
        str(created.get("id", "")),
        "",
        0,
        "provider_command",
        data={"trigger": cmd.trigger, "rotated": bool(existing)},
    )


def _policy_for(
    ctx: CommandContext, channel_type: str, template_id: str | None
) -> tuple[dict[str, Any], str | None, int]:
    if channel_type == "custom" and template_id is None:
        return {}, None, 365
    tid = template_id or channel_type
    template = tpl.get_template(ctx.session, _ws(ctx), tid)
    if template is None:
        raise CommandError("TEMPLATE_NOT_FOUND", tid, status=404)
    d = template.definition
    return dict(d), tid, int(d.get("retention_days", 365))


@handles(ImportChannel)
def import_channel(cmd: ImportChannel, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_import")
    if cmd.channel_type not in CHANNEL_TYPES:
        raise CommandError("CHANNEL_TYPE_INVALID", cmd.channel_type, status=400)
    inst = prov.load_instance(ctx.session, cmd.provider_instance_id)
    if inst is None:
        raise CommandError("PROVIDER_INSTANCE_UNKNOWN", cmd.provider_instance_id, status=404)
    channel_id = prov.channel_id_for(inst.provider_instance_id, cmd.external_channel_id)
    existing = ctx.session.execute(
        text("SELECT channel_id FROM channels WHERE channel_id = :c"), {"c": channel_id}
    ).first()
    if existing:
        return CommandResult(channel_id, "", 0, "channel", replayed=True)
    policy, template_id, retention = _policy_for(ctx, cmd.channel_type, cmd.template_id)
    display_name = cmd.display_name
    if display_name is None:
        try:
            display_name = str(
                prov.client_for(inst).get_channel(cmd.external_channel_id).get("display_name")
                or cmd.external_channel_id
            )
        except Exception:
            display_name = cmd.external_channel_id
    res = ctx.store.append(
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type="channel",
            aggregate_id=channel_id,
            type="CHANNEL_CONFIGURED",
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            payload={
                "channel_id": channel_id,
                "channel_type": cmd.channel_type,
                "policy_version": "policy-v1",
                "template_id": template_id,
                "external_channel_id": cmd.external_channel_id,
                "provider_instance_id": inst.provider_instance_id,
            },
        )
    )
    ctx.session.execute(
        text(
            "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
            "external_channel_id, "
            "channel_type, display_name, policy, documentation_template, language, template_id, "
            "retention_days) "
            "VALUES (:id, :c, :ws, :p, :ext, :type, :name, CAST(:policy AS jsonb), :doc, :lang, "
            ":tid, :ret)"
        ),
        {
            "id": uuid.uuid4(),
            "c": channel_id,
            "ws": _ws(ctx),
            "p": inst.id,
            "ext": cmd.external_channel_id,
            "type": cmd.channel_type,
            "name": display_name,
            "policy": json.dumps(policy),
            "doc": policy.get("documentation_template"),
            "lang": cmd.language,
            "tid": template_id,
            "ret": retention,
        },
    )
    return CommandResult(
        channel_id,
        res.event_id,
        res.aggregate_seq,
        "channel",
        data={"channel_type": cmd.channel_type, "template_id": template_id},
    )


def _channel_row(ctx: CommandContext, channel_id: str) -> Any:
    row = (
        ctx.session.execute(
            text(
                "SELECT id, channel_id, channel_type, policy, documentation_template, language, "
                "retention_days, "
                "legal_hold, template_id, status FROM channels WHERE channel_id = :c AND "
                "workspace_id = :ws FOR UPDATE"
            ),
            {"c": channel_id, "ws": _ws(ctx)},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CommandError("CHANNEL_NOT_FOUND", channel_id, status=404)
    return row


@handles(ConfigureChannel)
def configure_channel(cmd: ConfigureChannel, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_configure")
    row = _channel_row(ctx, cmd.channel_id)
    if row["status"] != "active":
        raise CommandError("CHANNEL_NOT_ACTIVE", row["status"], status=409)
    policy = dict(row["policy"] or {})
    template_id = row["template_id"]
    if cmd.template_id is not None:
        policy, template_id, _ = _policy_for(ctx, str(row["channel_type"]), cmd.template_id)
    if cmd.policy is not None:
        merged = {**policy, **cmd.policy}
        if merged:
            try:
                tpl.validate_definition(merged)
            except tpl.TemplateError as exc:
                raise CommandError(exc.code, exc.detail, status=exc.status) from exc
        policy = merged
    stream = ctx.store.stream(ctx.workspace_id, "channel", cmd.channel_id)
    res = ctx.store.append(
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type="channel",
            aggregate_id=cmd.channel_id,
            type="CHANNEL_CONFIGURED",
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            expected_seq=len(stream) + 1,
            channel_id=str(row["id"]),
            payload={
                "channel_id": cmd.channel_id,
                "channel_type": str(row["channel_type"]),
                "policy_version": "policy-v1",
                "template_id": template_id,
                "changed": sorted(
                    k
                    for k in (
                        "policy",
                        "documentation_template",
                        "language",
                        "retention_days",
                        "legal_hold",
                        "template_id",
                    )
                    if getattr(cmd, k) is not None
                ),
            },
        )
    )
    if not res.replayed:
        ctx.session.execute(
            text(
                "UPDATE channels SET policy = CAST(:policy AS jsonb), documentation_template = "
                "COALESCE(:doc, documentation_template), language = COALESCE(:lang, language), "
                "retention_days = COALESCE(:ret, retention_days), legal_hold = COALESCE(:hold, "
                "legal_hold), "
                "template_id = :tid WHERE channel_id = :c"
            ),
            {
                "policy": json.dumps(policy),
                "doc": cmd.documentation_template,
                "lang": cmd.language,
                "ret": cmd.retention_days,
                "hold": cmd.legal_hold,
                "tid": template_id,
                "c": cmd.channel_id,
            },
        )
    return CommandResult(
        cmd.channel_id,
        res.event_id,
        res.aggregate_seq,
        "channel",
        res.replayed,
        data={"template_id": template_id},
    )


@handles(ArchiveChannel)
def archive_channel(cmd: ArchiveChannel, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_archive")
    row = _channel_row(ctx, cmd.channel_id)
    if row["status"] != "active":
        raise CommandError("CHANNEL_NOT_ACTIVE", row["status"], status=409)
    stream = ctx.store.stream(ctx.workspace_id, "channel", cmd.channel_id)
    res = ctx.store.append(
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type="channel",
            aggregate_id=cmd.channel_id,
            type="CHANNEL_ARCHIVED",
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            expected_seq=len(stream) + 1,
            channel_id=str(row["id"]),
            payload={"channel_id": cmd.channel_id},
        )
    )
    if not res.replayed:
        ctx.session.execute(
            text(
                "UPDATE channels SET status = 'archived', archived_at = now() WHERE channel_id = :c"
            ),
            {"c": cmd.channel_id},
        )
    return CommandResult(cmd.channel_id, res.event_id, res.aggregate_seq, "channel", res.replayed)


def _template_result(t: tpl.ChannelTemplate, replayed: bool = False) -> CommandResult:
    return CommandResult(
        t.template_id,
        "",
        0,
        "channel_template",
        replayed,
        data={"version": t.version, "protected": t.protected, "channel_type": t.channel_type},
    )


@handles(CreateChannelTemplate)
def create_channel_template(cmd: CreateChannelTemplate, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_template_create")
    if cmd.channel_type not in CHANNEL_TYPES:
        raise CommandError("CHANNEL_TYPE_INVALID", cmd.channel_type, status=400)
    tpl.sync_defaults(ctx.session, _ws(ctx))
    try:
        t = tpl.create_template(
            ctx.session,
            _ws(ctx),
            cmd.template_id,
            cmd.name,
            cmd.channel_type,
            cmd.definition,
            uuid.UUID(ctx.principal.account_uuid),
        )
    except tpl.TemplateError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    _audit(ctx, "channel.template_create", "channel_template", cmd.template_id)
    return _template_result(t)


@handles(UpdateChannelTemplate)
def update_channel_template(cmd: UpdateChannelTemplate, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_template_update")
    try:
        t = tpl.update_template(ctx.session, _ws(ctx), cmd.template_id, cmd.definition, cmd.name)
    except tpl.TemplateError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    _audit(ctx, "channel.template_update", "channel_template", cmd.template_id, version=t.version)
    return _template_result(t)


@handles(DeleteChannelTemplate)
def delete_channel_template(cmd: DeleteChannelTemplate, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "channel.manage", action="api:channel_template_delete")
    try:
        tpl.delete_template(ctx.session, _ws(ctx), cmd.template_id)
    except tpl.TemplateError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    _audit(ctx, "channel.template_delete", "channel_template", cmd.template_id)
    return CommandResult(cmd.template_id, "", 0, "channel_template")
