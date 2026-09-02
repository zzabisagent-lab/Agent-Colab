"""Agent Registry commands on the bus (P3-01; spec §4.2/§5.1, development plan §7.3).

register/update/activate/suspend/revoke/heartbeat/offline are the only write paths for Agents.
Every command appends exactly one ``AGENT_*`` Event and refreshes the runtime columns by folding
the stream (``registry.refresh_state``), so live state and rebuilds agree by construction.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from server.agents import registry as reg
from server.agents.adapters.contract import AdapterError, adapter_for
from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.events.store import AppendRequest, AppendResult
from server.identity.principals import token_hash
from server.observability.audit import append_audit
from server.policy.repository import PostgresPolicyRepository
from server.usage.pricing import UsageError
from server.usage.records import record_usage


@dataclass(frozen=True)
class RegisterAgent(Command):
    agent_id: str
    display_name: str
    adapter_type: str  # mcp | webhook | mattermost_bot
    endpoint: dict[str, Any] = field(default_factory=dict)  # no secret values
    credential_ref: str | None = None  # Secret Broker reference
    owner_account_id: str | None = None  # public account id
    roles: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    channel_ids: tuple[str, ...] = ()  # public channel ids
    limits: dict[str, int] = field(default_factory=dict)
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    delivery_modes: tuple[str, ...] = ("pull",)
    idempotency_scope: str = "agent:register"


@dataclass(frozen=True)
class UpdateAgent(Command):
    agent_id: str
    display_name: str | None = None
    endpoint: dict[str, Any] | None = None
    credential_ref: str | None = None
    limits: dict[str, int] | None = None
    runtime_metadata: dict[str, Any] | None = None
    delivery_modes: tuple[str, ...] | None = None
    capabilities: tuple[str, ...] | None = None  # replaces the capability set
    idempotency_scope: str = "agent:update"


@dataclass(frozen=True)
class TestAgentConnection(Command):
    """Connection test through the Adapter contract; stores the probe as capabilities_snapshot."""

    agent_id: str
    idempotency_scope: str = "agent:test"


@dataclass(frozen=True)
class ActivateAgent(Command):
    agent_id: str
    probe: dict[str, Any] | None = None  # connection-test result when not stored yet
    idempotency_scope: str = "agent:activate"


@dataclass(frozen=True)
class SuspendAgent(Command):
    agent_id: str
    reason_code: str = "ADMIN_SUSPEND"
    idempotency_scope: str = "agent:suspend"


@dataclass(frozen=True)
class RevokeAgent(Command):
    agent_id: str
    reason_code: str = "ADMIN_REVOKE"
    security_revoke: bool = False  # True: in-flight Tasks lose their policy snapshot protection
    idempotency_scope: str = "agent:revoke"


@dataclass(frozen=True)
class RecordHeartbeat(Command):
    agent_id: str
    health: str = "ok"
    capacity: int = 1
    usage: dict[str, Any] | None = None  # §7C usage_since_last
    usage_unavailable: str | None = None  # reason code when no usage was measured
    capabilities: tuple[str, ...] = ()  # re-confirmed capabilities after a returning heartbeat
    idempotency_scope: str = "agent:heartbeat"


@dataclass(frozen=True)
class MarkOffline(Command):
    agent_id: str
    idempotency_scope: str = "agent:offline"


@dataclass(frozen=True)
class SweepOffline(Command):
    """Mark every Agent whose heartbeats stopped (3 misses or 90 s) offline."""

    idempotency_scope: str = "agent:sweep"


# ------------------------------------------------------------------ helpers


def _ws(ctx: CommandContext) -> uuid.UUID:
    return uuid.UUID(ctx.workspace_id)


def _err(exc: reg.RegistryError) -> CommandError:
    return CommandError(exc.code, exc.detail, status=exc.status)


def _load(ctx: CommandContext, agent_id: str, *, lock: bool = True) -> reg.AgentRow:
    row = reg.load_agent(ctx.session, _ws(ctx), agent_id, for_update=lock)
    if row is None:
        raise CommandError("AGENT_NOT_FOUND", agent_id, status=404)
    return row


def _replay(ctx: CommandContext, agent_id: str, scope: str) -> AppendResult | None:
    for ev in ctx.store.stream(ctx.workspace_id, "agent", agent_id):
        if (
            ev.get("idempotency_scope") == scope
            and ev.get("idempotency_key") == ctx.idempotency_key
            and ev.get("actor_account_id") == ctx.principal.account_uuid
        ):
            return AppendResult(
                ev["event_id"],
                ev["aggregate_seq"],
                ev["content_hash"],
                int(ev.get("recorded_seq", 0)),
                replayed=True,
            )
    return None


def _append(
    ctx: CommandContext, cmd: Command, agent_id: str, event_type: str, payload: dict[str, Any]
) -> AppendResult:
    return ctx.store.append(
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type="agent",
            aggregate_id=agent_id,
            type=event_type,
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            payload=payload,
        )
    )


def _audit(
    ctx: CommandContext, action: str, agent_id: str, result: str = "OK", **meta: Any
) -> None:
    append_audit(
        ctx.session,
        action=action,
        target_type="agent",
        target_id=agent_id,
        result=result,
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=_ws(ctx),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata=meta,
        clock=ctx.clock,
    )


def _finish(ctx: CommandContext, res: AppendResult, agent_id: str, **data: Any) -> CommandResult:
    state = reg.refresh_state(ctx.session, ctx.store, ctx.workspace_id, agent_id, ctx.clock.now())
    return CommandResult(
        agent_id,
        res.event_id,
        res.aggregate_seq,
        "agent",
        replayed=res.replayed,
        data={
            "status": state.status,
            "online": state.online,
            "lifecycle_hash": state.lifecycle_hash,
            **data,
        },
    )


def _account_uuid(ctx: CommandContext, public_id: str) -> uuid.UUID:
    row = ctx.session.execute(
        text("SELECT id FROM accounts WHERE account_id = :a AND workspace_id = :w"),
        {"a": public_id, "w": _ws(ctx)},
    ).first()
    if row is None:
        raise CommandError("ACCOUNT_NOT_FOUND", public_id, status=404)
    return uuid.UUID(str(row[0]))


def _set_account_status(ctx: CommandContext, account_uuid: uuid.UUID, status: str) -> None:
    ctx.session.execute(
        text("UPDATE accounts SET status = :s WHERE id = :a"), {"s": status, "a": account_uuid}
    )


def _replace_capabilities(
    ctx: CommandContext, agent_id: str, capabilities: tuple[str, ...]
) -> None:
    known = {
        str(r[0])
        for r in ctx.session.execute(
            text("SELECT capability_id FROM capabilities WHERE capability_id = ANY(:ids)"),
            {"ids": list(capabilities)},
        ).all()
    }
    unknown = sorted(set(capabilities) - known)
    if unknown:
        raise CommandError("CAPABILITY_UNKNOWN", ", ".join(unknown), status=404)
    ctx.session.execute(text("DELETE FROM agent_capabilities WHERE agent_id = :g"), {"g": agent_id})
    repo = PostgresPolicyRepository()
    for cap in sorted(set(capabilities)):
        repo.grant_capability(ctx.session, agent_id, cap)


def _is_self(ctx: CommandContext, row: reg.AgentRow) -> bool:
    return str(row.account_id) == ctx.principal.account_uuid


# ------------------------------------------------------------------ handlers


@handles(RegisterAgent)
def register_agent(cmd: RegisterAgent, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "agent.manage", action="api:agent_register")
    try:
        reg.validate_agent_id(cmd.agent_id)
        reg.validate_adapter_type(cmd.adapter_type)
        reg.reject_secret_values(cmd.endpoint)
        limits = reg.validate_limits(cmd.limits)
        modes = reg.validate_delivery_modes(cmd.delivery_modes)
    except reg.RegistryError as exc:
        raise _err(exc) from exc
    if not cmd.display_name.strip():
        raise CommandError("AGENT_DISPLAY_NAME_REQUIRED", "display_name", status=400)
    existing = reg.load_agent(ctx.session, _ws(ctx), cmd.agent_id)
    if existing is not None:
        replay = _replay(ctx, cmd.agent_id, cmd.idempotency_scope)
        if replay is not None:
            return _finish(ctx, replay, cmd.agent_id, account_id=existing.account_public_id)
        raise CommandError("AGENT_ALREADY_EXISTS", cmd.agent_id, status=409)
    if ctx.session.execute(
        text("SELECT 1 FROM agents WHERE agent_id = :g"), {"g": cmd.agent_id}
    ).first():
        raise CommandError("AGENT_ALREADY_EXISTS", cmd.agent_id, status=409)
    owner = None if cmd.owner_account_id is None else _account_uuid(ctx, cmd.owner_account_id)
    account_public = f"acct-{cmd.agent_id}"
    account_uuid = uuid.uuid4()
    ctx.session.execute(
        text(
            "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
            "VALUES (:i, :a, :w, 'agent', :d)"
        ),
        {"i": account_uuid, "a": account_public, "w": _ws(ctx), "d": cmd.display_name.strip()},
    )
    # the Agent's own service token: returned exactly once, only its hash is stored
    service_token = "svc-" + secrets.token_urlsafe(32)
    fingerprint = f"sha256:{account_public}"
    ctx.session.execute(
        text(
            "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
            "VALUES (:i, :a, :f, :h)"
        ),
        {"i": uuid.uuid4(), "a": account_uuid, "f": fingerprint, "h": token_hash(service_token)},
    )
    ctx.session.execute(
        text(
            "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, status, "
            "display_name, owner_account_id, endpoint, credential_ref, runtime_metadata, limits, "
            "delivery_modes, updated_at) VALUES (:i, :g, :w, :a, :t, 'pending', :d, :o, "
            "CAST(:e AS jsonb), :c, CAST(:m AS jsonb), CAST(:l AS jsonb), CAST(:dm AS jsonb), :now)"
        ),
        {
            "i": uuid.uuid4(),
            "g": cmd.agent_id,
            "w": _ws(ctx),
            "a": account_uuid,
            "t": cmd.adapter_type,
            "d": cmd.display_name.strip(),
            "o": owner,
            "e": reg.json_dumps(dict(cmd.endpoint)),
            "c": cmd.credential_ref,
            "m": reg.json_dumps(dict(cmd.runtime_metadata)),
            "l": reg.json_dumps(limits),
            "dm": reg.json_dumps(modes),
            "now": ctx.clock.now(),
        },
    )
    if cmd.capabilities:
        _replace_capabilities(ctx, cmd.agent_id, cmd.capabilities)
    res = _append(
        ctx,
        cmd,
        cmd.agent_id,
        "AGENT_REGISTERED",
        {
            "agent_id": cmd.agent_id,
            "account_id": account_public,
            "adapter_type": cmd.adapter_type,
            "display_name": cmd.display_name.strip(),
            "owner_account_id": cmd.owner_account_id,
            "delivery_modes": modes,
            "limits": limits,
            "capabilities": sorted(set(cmd.capabilities)),
            "roles": sorted(set(cmd.roles)),
            "channel_ids": sorted(set(cmd.channel_ids)),
            "credential_fingerprint": fingerprint,
        },
    )
    repo = PostgresPolicyRepository()
    now = ctx.clock.now()
    for role_id in sorted(set(cmd.roles)):
        if not ctx.session.execute(
            text("SELECT 1 FROM roles WHERE role_id = :r AND workspace_id = :w"),
            {"r": role_id, "w": _ws(ctx)},
        ).first():
            raise CommandError("ROLE_NOT_FOUND", role_id, status=404)
        repo.assign_role(
            ctx.session,
            account_uuid,
            role_id,
            uuid.UUID(ctx.principal.account_uuid),
            now,
            event_id=res.event_id,
        )
    for channel_id in sorted(set(cmd.channel_ids)):
        chan = ctx.session.execute(
            text("SELECT id FROM channels WHERE channel_id = :c AND workspace_id = :w"),
            {"c": channel_id, "w": _ws(ctx)},
        ).first()
        if chan is None:
            raise CommandError("CHANNEL_NOT_FOUND", channel_id, status=404)
        ctx.session.execute(
            text(
                "INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a) "
                "ON CONFLICT (channel_id, account_id) DO UPDATE SET status = 'active'"
            ),
            {"c": chan[0], "a": account_uuid},
        )
    _audit(
        ctx,
        "agent.register",
        cmd.agent_id,
        adapter_type=cmd.adapter_type,
        credential_fingerprint=fingerprint,
        roles=sorted(set(cmd.roles)),
    )
    return _finish(ctx, res, cmd.agent_id, account_id=account_public, service_token=service_token)


@handles(UpdateAgent)
def update_agent(cmd: UpdateAgent, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "agent.manage", action="api:agent_update")
    row = _load(ctx, cmd.agent_id)
    replay = _replay(ctx, cmd.agent_id, cmd.idempotency_scope)
    if replay is not None:
        return _finish(ctx, replay, cmd.agent_id)
    if row.status == "revoked":
        raise CommandError("AGENT_REVOKED", "revoked Agents cannot be updated", status=409)
    changed: list[str] = []
    sets: list[str] = []
    params: dict[str, Any] = {"g": cmd.agent_id, "now": ctx.clock.now()}
    try:
        if cmd.display_name is not None and cmd.display_name.strip() != row.display_name:
            sets.append("display_name = :d")
            params["d"] = cmd.display_name.strip()
            changed.append("display_name")
        if cmd.endpoint is not None:
            reg.reject_secret_values(cmd.endpoint)
            sets.append("endpoint = CAST(:e AS jsonb)")
            params["e"] = reg.json_dumps(dict(cmd.endpoint))
            changed.append("endpoint")
        if cmd.credential_ref is not None:
            sets.append("credential_ref = :c")
            params["c"] = cmd.credential_ref
            changed.append("credential_ref")
        if cmd.limits is not None:
            sets.append("limits = CAST(:l AS jsonb)")
            params["l"] = reg.json_dumps(reg.validate_limits(cmd.limits))
            changed.append("limits")
        if cmd.runtime_metadata is not None:
            sets.append("runtime_metadata = CAST(:m AS jsonb)")
            params["m"] = reg.json_dumps(dict(cmd.runtime_metadata))
            changed.append("runtime_metadata")
        if cmd.delivery_modes is not None:
            sets.append("delivery_modes = CAST(:dm AS jsonb)")
            params["dm"] = reg.json_dumps(reg.validate_delivery_modes(cmd.delivery_modes))
            changed.append("delivery_modes")
    except reg.RegistryError as exc:
        raise _err(exc) from exc
    if cmd.capabilities is not None:
        _replace_capabilities(ctx, cmd.agent_id, cmd.capabilities)
        changed.append("capabilities")
    if not changed:
        raise CommandError("AGENT_UPDATE_EMPTY", "nothing to change", status=400)
    if sets:
        ctx.session.execute(
            text(f"UPDATE agents SET {', '.join(sets)}, updated_at = :now WHERE agent_id = :g"),  # noqa: S608
            params,
        )
    if "display_name" in changed:
        ctx.session.execute(
            text("UPDATE accounts SET display_name = :d WHERE id = :a"),
            {"d": params["d"], "a": row.account_id},
        )
    res = _append(
        ctx,
        cmd,
        cmd.agent_id,
        "AGENT_UPDATED",
        {"agent_id": cmd.agent_id, "changed_fields": sorted(changed)},
    )
    _audit(ctx, "agent.update", cmd.agent_id, changed_fields=sorted(changed))
    return _finish(ctx, res, cmd.agent_id, changed_fields=sorted(changed))


def _probe_dict(agent_id: str, probe: Any) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "identity_hash": probe.identity_hash,
        "adapter_type": probe.adapter_type,
        "capabilities": sorted(probe.capabilities),
        "unsupported": sorted(probe.unsupported),
        "delivery_modes": sorted(str(m) for m in probe.delivery_modes),
        "secret_handles": probe.secret_handles,
        "limits": dict(probe.limits),
        "runtime": dict(probe.runtime),
    }


@handles(TestAgentConnection)
def test_agent_connection(cmd: TestAgentConnection, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "agent.manage", action="api:agent_test")
    row = _load(ctx, cmd.agent_id)
    try:
        adapter = adapter_for(row.adapter_type, {**row.endpoint, "agent_id": row.agent_id})
        probe = adapter.probe()
    except AdapterError as exc:
        _audit(ctx, "agent.test_connection", cmd.agent_id, result="FAIL", code=exc.code)
        return CommandResult(
            cmd.agent_id, "", 0, "agent", data={"ok": False, "code": exc.code, "detail": exc.detail}
        )
    except Exception as exc:  # adapter crashed: normalized, never re-raised with internals
        _audit(ctx, "agent.test_connection", cmd.agent_id, result="FAIL", code="ADAPTER_INTERNAL")
        return CommandResult(
            cmd.agent_id,
            "",
            0,
            "agent",
            data={"ok": False, "code": "ADAPTER_INTERNAL", "detail": type(exc).__name__},
        )
    snapshot = _probe_dict(cmd.agent_id, probe)
    ctx.session.execute(
        text(
            "UPDATE agents SET capabilities_snapshot = CAST(:s AS jsonb), updated_at = :now "
            "WHERE agent_id = :g"
        ),
        {"s": reg.json_dumps(snapshot), "now": ctx.clock.now(), "g": cmd.agent_id},
    )
    _audit(ctx, "agent.test_connection", cmd.agent_id, identity_hash=probe.identity_hash)
    return CommandResult(cmd.agent_id, "", 0, "agent", data={"ok": True, "probe": snapshot})


@handles(ActivateAgent)
def activate_agent(cmd: ActivateAgent, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "agent.manage", action="api:agent_activate")
    row = _load(ctx, cmd.agent_id)
    replay = _replay(ctx, cmd.agent_id, cmd.idempotency_scope)
    if replay is not None:
        return _finish(ctx, replay, cmd.agent_id)
    if row.status == "revoked":
        raise CommandError("AGENT_REVOKED", "revoked Agents cannot be activated", status=409)
    if row.status == "active":
        raise CommandError("AGENT_ALREADY_ACTIVE", cmd.agent_id, status=409)
    snapshot = row.capabilities_snapshot
    if cmd.probe:
        snapshot = {**dict(cmd.probe), "agent_id": cmd.agent_id}
        ctx.session.execute(
            text("UPDATE agents SET capabilities_snapshot = CAST(:s AS jsonb) WHERE agent_id = :g"),
            {"s": reg.json_dumps(snapshot), "g": cmd.agent_id},
        )
    if not snapshot.get("identity_hash") and not snapshot.get("capabilities"):
        raise CommandError(
            "AGENT_CONNECTION_TEST_REQUIRED",
            "activate needs a passing connection test (probe) first",
            status=409,
        )
    _set_account_status(ctx, row.account_id, "ACTIVE")
    res = _append(
        ctx,
        cmd,
        cmd.agent_id,
        "AGENT_ACTIVATED",
        {"agent_id": cmd.agent_id, "identity_hash": snapshot.get("identity_hash")},
    )
    _audit(ctx, "agent.activate", cmd.agent_id)
    return _finish(ctx, res, cmd.agent_id)


def _reroute_unavailable(ctx: CommandContext, agent_id: str, reason: str) -> None:
    """§7D.3: Tasks assigned to a suspended/revoked/offline Agent are re-routed once."""
    from server.agents import rerouting

    try:
        rerouting.on_agent_unavailable(
            ctx.session, ctx.store, agent_id, reason, clock=ctx.clock, authorizer=ctx.authorizer
        )
    except CommandError as exc:
        if exc.code != "SYSTEM_ACCOUNT_MISSING":
            raise


@handles(SuspendAgent)
def suspend_agent(cmd: SuspendAgent, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "agent.manage", action="api:agent_suspend")
    row = _load(ctx, cmd.agent_id)
    replay = _replay(ctx, cmd.agent_id, cmd.idempotency_scope)
    if replay is not None:
        return _finish(ctx, replay, cmd.agent_id)
    if row.status in ("revoked", "suspended"):
        raise CommandError("AGENT_STATUS_INVALID", f"cannot suspend from {row.status}", 409)
    _set_account_status(ctx, row.account_id, "SUSPENDED")  # new requests denied immediately
    res = _append(
        ctx,
        cmd,
        cmd.agent_id,
        "AGENT_SUSPENDED",
        {"agent_id": cmd.agent_id, "reason_code": cmd.reason_code},
    )
    _audit(ctx, "agent.suspend", cmd.agent_id, reason_code=cmd.reason_code)
    _reroute_unavailable(ctx, cmd.agent_id, "AGENT_SUSPENDED")
    return _finish(ctx, res, cmd.agent_id)


@handles(RevokeAgent)
def revoke_agent(cmd: RevokeAgent, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "agent.manage", action="api:agent_revoke")
    row = _load(ctx, cmd.agent_id)
    replay = _replay(ctx, cmd.agent_id, cmd.idempotency_scope)
    if replay is not None:
        return _finish(ctx, replay, cmd.agent_id)
    if row.status == "revoked":
        raise CommandError("AGENT_REVOKED", "already revoked", status=409)
    _set_account_status(ctx, row.account_id, "SUSPENDED")
    ctx.session.execute(  # credentials die with the Agent: authentication fails from now on
        text(
            "UPDATE service_credentials SET status = 'revoked', revoked_at = :now "
            "WHERE account_id = :a AND status = 'active'"
        ),
        {"now": ctx.clock.now(), "a": row.account_id},
    )
    res = _append(
        ctx,
        cmd,
        cmd.agent_id,
        "AGENT_REVOKED",
        {
            "agent_id": cmd.agent_id,
            "reason_code": cmd.reason_code,
            "security_revoke": cmd.security_revoke,
        },
    )
    _audit(
        ctx,
        "agent.revoke",
        cmd.agent_id,
        reason_code=cmd.reason_code,
        security_revoke=cmd.security_revoke,
    )
    _reroute_unavailable(ctx, cmd.agent_id, "AGENT_REVOKED")
    return _finish(ctx, res, cmd.agent_id)


@handles(RecordHeartbeat)
def record_heartbeat(cmd: RecordHeartbeat, ctx: CommandContext) -> CommandResult:
    row = _load(ctx, cmd.agent_id)
    if _is_self(ctx, row):
        require_permission(ctx, "agent.self", action="api:agent_heartbeat")
    else:
        require_permission(ctx, "agent.manage", action="api:agent_heartbeat")
    replay = _replay(ctx, cmd.agent_id, cmd.idempotency_scope)
    if replay is not None:
        return _finish(ctx, replay, cmd.agent_id)
    if row.status in ("pending", "suspended", "revoked"):
        raise CommandError("AGENT_STATUS_INVALID", f"heartbeat not accepted in {row.status}", 409)
    if cmd.health not in reg.HEALTH_VALUES:
        raise CommandError("AGENT_HEALTH_INVALID", cmd.health, status=400)
    if cmd.capacity < 0:
        raise CommandError("AGENT_CAPACITY_INVALID", str(cmd.capacity), status=400)
    if cmd.usage is None and not cmd.usage_unavailable:
        raise CommandError(
            "USAGE_REQUIRED", "usage or a usage_unavailable reason is required (§7C)", status=422
        )
    payload: dict[str, Any] = {
        "agent_id": cmd.agent_id,
        "capacity": cmd.capacity,
        "health": cmd.health,
        "returning": not row.online,
    }
    if cmd.capabilities:
        payload["capabilities"] = sorted(set(cmd.capabilities))
    if cmd.usage_unavailable:
        payload["usage_unavailable"] = cmd.usage_unavailable
    res = _append(ctx, cmd, cmd.agent_id, "AGENT_HEARTBEAT_RECORDED", payload)
    now = ctx.clock.now()
    ctx.session.execute(
        text(
            "INSERT INTO agent_heartbeats (agent_id, reported_at, health, capacity, usage, "
            "event_id) VALUES (:g, :t, :h, :c, CAST(:u AS jsonb), :e)"
        ),
        {
            "g": cmd.agent_id,
            "t": now,
            "h": cmd.health,
            "c": cmd.capacity,
            "u": reg.json_dumps(
                cmd.usage if cmd.usage is not None else {"usage_unavailable": cmd.usage_unavailable}
            ),
            "e": res.event_id,
        },
    )
    if cmd.capabilities:  # returning heartbeat re-confirms capabilities (§7.3)
        ctx.session.execute(
            text(
                "UPDATE agents SET capabilities_snapshot = capabilities_snapshot || "
                "CAST(:c AS jsonb) WHERE agent_id = :g"
            ),
            {
                "c": reg.json_dumps({"capabilities": sorted(set(cmd.capabilities))}),
                "g": cmd.agent_id,
            },
        )
    if cmd.usage is not None or cmd.usage_unavailable:
        try:
            record_usage(
                ctx.session,
                workspace_id=ctx.workspace_id,
                account_id=str(row.account_id),
                agent_id=cmd.agent_id,
                work_item_id=None,
                usage=cmd.usage,
                usage_unavailable_reason=cmd.usage_unavailable,
                clock=ctx.clock,
            )
        except UsageError as exc:
            raise CommandError(exc.code, exc.detail, status=422) from exc
    return _finish(ctx, res, cmd.agent_id, returning=payload["returning"])


def _mark_offline(ctx: CommandContext, cmd: Command, row: reg.AgentRow) -> AppendResult:
    missed = reg.missed_heartbeats_at(row.last_heartbeat_at, ctx.clock.now())
    ctx.session.execute(
        text("UPDATE agents SET missed_heartbeats = :m WHERE agent_id = :g"),
        {"m": missed, "g": row.agent_id},
    )
    return _append(
        ctx,
        cmd,
        row.agent_id,
        "AGENT_MARKED_OFFLINE",
        {"agent_id": row.agent_id, "missed_heartbeats": max(missed, reg.OFFLINE_AFTER_MISSES)},
    )


@handles(MarkOffline)
def mark_offline(cmd: MarkOffline, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "agent.manage", action="api:agent_update")
    row = _load(ctx, cmd.agent_id)
    replay = _replay(ctx, cmd.agent_id, cmd.idempotency_scope)
    if replay is not None:
        return _finish(ctx, replay, cmd.agent_id)
    if row.status != "active" or not row.online:
        raise CommandError("AGENT_STATUS_INVALID", f"not online ({row.status})", status=409)
    res = _mark_offline(ctx, cmd, row)
    _audit(ctx, "agent.offline", cmd.agent_id, missed_heartbeats=row.missed_heartbeats)
    _reroute_unavailable(ctx, cmd.agent_id, "AGENT_OFFLINE")
    return _finish(ctx, res, cmd.agent_id)


@handles(SweepOffline)
def sweep_offline(cmd: SweepOffline, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "agent.manage", action="api:agent_update")
    now = ctx.clock.now()
    marked: list[str] = []
    for row in reg.list_agents(ctx.session, _ws(ctx)):
        if row.status != "active" or not row.online:
            continue
        if not reg.is_offline_due(row.last_heartbeat_at, now):
            missed = reg.missed_heartbeats_at(row.last_heartbeat_at, now)
            if missed != row.missed_heartbeats:
                ctx.session.execute(
                    text("UPDATE agents SET missed_heartbeats = :m WHERE agent_id = :g"),
                    {"m": missed, "g": row.agent_id},
                )
            continue
        locked = _load(ctx, row.agent_id)
        sweep_cmd = MarkOffline(locked.agent_id)
        idem_ctx = CommandContext(
            **{**ctx.__dict__, "idempotency_key": f"{ctx.idempotency_key}:{locked.agent_id}"}
        )
        res = _mark_offline(idem_ctx, sweep_cmd, locked)
        reg.refresh_state(ctx.session, ctx.store, ctx.workspace_id, locked.agent_id, now)
        _audit(ctx, "agent.offline", locked.agent_id, sweep=True, event_id=res.event_id)
        marked.append(locked.agent_id)
    return CommandResult("sweep", "", 0, "agent", data={"marked_offline": marked})


def agent_id_for_principal(ctx: CommandContext) -> str | None:
    row = reg.agent_for_account(ctx.session, uuid.UUID(ctx.principal.account_uuid))
    return None if row is None else row.agent_id


def heartbeat_deadline(now: dt.datetime) -> dt.datetime:
    return now + dt.timedelta(seconds=reg.OFFLINE_AFTER_S)
