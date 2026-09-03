"""Secret Broker commands (P4-05/P4-06/P4-07) — the only write path for secrets.

Values travel inside command objects as ``bytes`` fields excluded from ``repr`` and never enter
Event payloads, audit metadata, results or logs.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from server.agents.registry import agent_for_account
from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.approvals.model import Subject
from server.approvals.service import request_approval
from server.events.store import AppendRequest, EventStoreError
from server.secrets import broker
from server.secrets import leases as ls
from server.secrets import local_provider as lp
from server.secrets.envelope import MasterKey
from server.secrets.provider import LeaseScope, ResolveContext, SecretError


def _master(ctx: CommandContext) -> MasterKey:
    key = ctx.extras.get("master_key")
    if key is None:
        crypto = ctx.extras.get("crypto")
        key = getattr(crypto, "_master", None) if crypto is not None else None
    if key is None:
        try:
            key = lp.load_master_key()
        except SecretError as exc:
            raise CommandError(exc.code, exc.detail, status=503) from exc
    return key


def _err(exc: SecretError) -> CommandError:
    status = {  # HTTP status per stable code — no credential material here
        "SECRET_NOT_FOUND": 404,  # nosec B105 - status code map
        "SECRET_PROVIDER_UNAVAILABLE": 503,  # nosec B105 - status code map
        "SECRET_EXPOSURE_APPROVAL_REQUIRED": 403,  # nosec B105 - status code map
    }.get(exc.code, 403)
    return CommandError(exc.code, "secret operation denied", status=status)


def _ws(ctx: CommandContext) -> uuid.UUID:
    return uuid.UUID(ctx.workspace_id)


# ------------------------------------------------------------------ admin commands


@dataclass(frozen=True)
class RegisterSecret(Command):
    name: str
    value: bytes = field(repr=False, default=b"")
    metadata: dict[str, Any] = field(default_factory=dict)
    idempotency_scope: str = "secret:register"


@handles(RegisterSecret)
def register_secret(cmd: RegisterSecret, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "secret.register", action="api:secret_register")
    if not cmd.value:
        raise CommandError("SECRET_VALUE_REQUIRED", "value required", status=400)
    if any(k in cmd.metadata for k in ("value", "secret", "password", "token")):
        raise CommandError("SECRET_METADATA_INVALID", "metadata must not carry values", 400)
    try:
        ref = lp.put_secret(
            ctx.session,
            _master(ctx),
            workspace_id=_ws(ctx),
            name=cmd.name,
            value=cmd.value,
            metadata=cmd.metadata,
            created_by=uuid.UUID(ctx.principal.account_uuid),
            now=ctx.clock.now(),
        )
    except SecretError as exc:
        raise _err(exc) from exc
    res = ctx.store.append(
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type="secret",
            aggregate_id=ref.secret_ref,
            type="SECRET_REGISTERED",
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            payload={"secret_id": ref.secret_ref, "provider": ref.provider, "version": 1},
        )
    )
    return CommandResult(
        ref.secret_ref,
        res.event_id,
        res.aggregate_seq,
        "secret",
        data={"secret_ref": ref.secret_ref, "version": 1},
    )


@dataclass(frozen=True)
class RotateSecret(Command):
    secret_ref: str
    value: bytes = field(repr=False, default=b"")
    idempotency_scope: str = "secret:rotate"


@handles(RotateSecret)
def rotate_secret(cmd: RotateSecret, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "secret.register", action="api:secret_register")
    if not cmd.value:
        raise CommandError("SECRET_VALUE_REQUIRED", "value required", status=400)
    try:
        ref = lp.rotate_secret(
            ctx.session,
            _master(ctx),
            secret_ref=cmd.secret_ref,
            value=cmd.value,
            now=ctx.clock.now(),
        )
    except SecretError as exc:
        raise _err(exc) from exc
    try:
        res = ctx.store.append(
            AppendRequest(
                workspace_id=ctx.workspace_id,
                aggregate_type="secret",
                aggregate_id=ref.secret_ref,
                type="SECRET_REGISTERED",
                actor_account_id=ctx.principal.account_uuid,
                correlation_id=ctx.correlation_id,
                idempotency_scope=cmd.idempotency_scope,
                idempotency_key=ctx.idempotency_key,
                payload={
                    "secret_id": ref.secret_ref,
                    "provider": ref.provider,
                    "version": ref.version,
                    "rotated": True,
                },
            )
        )
    except EventStoreError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc
    # leases of the previous version end with the rotation
    broker.revoke(
        ctx.session,
        workspace_id=_ws(ctx),
        kind="secret",
        target_id=ref.secret_ref,
        reason="SECRET_ROTATED",
        now=ctx.clock.now(),
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        store=ctx.store,
        actor_uuid=ctx.principal.account_uuid,
    )
    return CommandResult(
        ref.secret_ref,
        res.event_id,
        res.aggregate_seq,
        "secret",
        data={"secret_ref": ref.secret_ref, "version": ref.version},
    )


@dataclass(frozen=True)
class CreateSecretGrant(Command):
    secret_ref: str
    agent_id: str
    task_id: str | None = None
    action: str | None = None
    ttl_seconds: int = 300
    single_use: bool = True
    valid_for_seconds: int | None = None
    idempotency_scope: str = "secret_grant:create"


@handles(CreateSecretGrant)
def create_secret_grant(cmd: CreateSecretGrant, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "secret.grant", action="api:secret_grant")
    try:
        grant = broker.create_grant(
            ctx.session,
            workspace_id=_ws(ctx),
            secret_ref=cmd.secret_ref,
            agent_id=cmd.agent_id,
            task_id=cmd.task_id,
            action=cmd.action,
            ttl_seconds=cmd.ttl_seconds,
            single_use=cmd.single_use,
            valid_for=dt.timedelta(seconds=cmd.valid_for_seconds)
            if cmd.valid_for_seconds
            else None,
            created_by=uuid.UUID(ctx.principal.account_uuid),
            now=ctx.clock.now(),
            store=ctx.store,
            correlation_id=ctx.correlation_id,
            idempotency_key=ctx.idempotency_key,
        )
    except SecretError as exc:
        raise _err(exc) from exc
    return CommandResult(grant.grant_id, "", 0, "secret_grant", data=broker.grant_view(grant))


@dataclass(frozen=True)
class RevokeSecretGrant(Command):
    target_id: str  # grant-..., lease-..., or a Task/Agent id with kind
    kind: str = "grant"
    reason_code: str = "ADMIN_REVOKE"
    idempotency_scope: str = "secret_grant:revoke"


@handles(RevokeSecretGrant)
def revoke_secret_grant(cmd: RevokeSecretGrant, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "secret.grant", action="api:secret_grant")
    try:
        leases = broker.revoke(
            ctx.session,
            workspace_id=_ws(ctx),
            kind=cmd.kind,
            target_id=cmd.target_id,
            reason=cmd.reason_code,
            now=ctx.clock.now(),
            actor_label=ctx.principal.account_id,
            correlation_id=ctx.correlation_id,
            store=ctx.store,
            actor_uuid=ctx.principal.account_uuid,
        )
    except SecretError as exc:
        raise _err(exc) from exc
    return CommandResult(cmd.target_id, "", 0, "secret_grant", data={"revoked_leases": leases})


@dataclass(frozen=True)
class RequestSecretExposure(Command):
    """§9.3: passing a secret into LLM context needs the exposure flag AND Human approval."""

    grant_id: str
    task_id: str
    reason: str = "llm_context"
    idempotency_scope: str = "approval:request"


@handles(RequestSecretExposure)
def request_secret_exposure(cmd: RequestSecretExposure, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "secret.grant", action="api:secret_grant_scope_expand")
    grant = broker.load_grant(ctx.session, cmd.grant_id, lock=True)
    if grant is None or grant.workspace_id != _ws(ctx):
        raise CommandError("SECRET_NOT_FOUND", cmd.grant_id, status=404)
    from server.application.approvals import _catalog

    try:
        result = request_approval(
            ctx.session,
            ctx.store,
            _catalog(ctx),
            ctx.clock,
            workspace_uuid=_ws(ctx),
            requested_by=uuid.UUID(ctx.principal.account_uuid),
            subject=Subject("task", cmd.task_id),
            action=broker.EXPOSURE_ACTION,
            correlation_id=ctx.correlation_id,
            idempotency_key=ctx.idempotency_key,
            resource_scope={
                "grant_id": grant.grant_id,
                "secret_ref": grant.secret_ref,
                "purpose": cmd.reason,
            },
            requires_human_approval=True,
        )
    except Exception as exc:  # ApprovalError has code/status
        code = getattr(exc, "code", "APPROVAL_REQUEST_FAILED")
        raise CommandError(
            str(code), "exposure request failed", status=getattr(exc, "status", 409)
        ) from exc
    broker.set_exposure_approval(ctx.session, grant.grant_id, result.approval_id, allowed=True)
    return CommandResult(
        result.approval_id,
        "",
        0,
        "approval",
        data={"approval_id": result.approval_id, "grant_id": grant.grant_id, "status": "PENDING"},
    )


# ------------------------------------------------------------------ Agent-side commands


def _caller_agent(ctx: CommandContext) -> str:
    row = agent_for_account(ctx.session, uuid.UUID(ctx.principal.account_uuid))
    if row is None:
        raise CommandError("SECRET_SCOPE_DENIED", "caller is not a registered Agent", status=403)
    return row.agent_id


@dataclass(frozen=True)
class IssueSecretLease(Command):
    secret_ref: str
    task_id: str | None = None
    action: str | None = None
    work_item_id: str | None = None
    sidecar_instance_id: str | None = None
    ttl_seconds: int | None = None
    idempotency_scope: str = "secret_grant:lease"


@handles(IssueSecretLease)
def issue_secret_lease(cmd: IssueSecretLease, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "secret.lease", action="api:secret_lease")
    agent_id = _caller_agent(ctx)
    scope = LeaseScope(agent_id, cmd.task_id, cmd.action, cmd.work_item_id, cmd.sidecar_instance_id)
    try:
        lease = broker.issue_lease(
            ctx.session,
            workspace_id=_ws(ctx),
            secret_ref=cmd.secret_ref,
            scope=scope,
            ttl=dt.timedelta(seconds=cmd.ttl_seconds) if cmd.ttl_seconds else None,
            single_use=None,
            now=ctx.clock.now(),
            actor_label=ctx.principal.account_id,
            correlation_id=ctx.correlation_id,
        )
    except SecretError as exc:
        raise _err(exc) from exc
    return CommandResult(
        lease.lease_id,
        "",
        0,
        "secret_lease",
        data={
            "lease_id": lease.lease_id,
            "handle": lease.handle,  # returned exactly once to the caller; never stored
            "secret_ref": lease.secret_ref,
            "expires_at": lease.expires_at.isoformat(),
            "single_use": lease.single_use,
        },
    )


@dataclass(frozen=True)
class ResolveSecret(Command):
    handle: str = field(repr=False, default="")
    sidecar_instance_id: str | None = None
    work_item_id: str | None = None
    task_id: str | None = None
    action: str | None = None
    purpose: str = "adapter"
    idempotency_scope: str = "secret_grant:access"


@handles(ResolveSecret)
def resolve_secret(cmd: ResolveSecret, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "secret.lease", action="api:secret_lease")
    agent_id = _caller_agent(ctx)
    context = ResolveContext(
        agent_id=agent_id,
        sidecar_instance_id=cmd.sidecar_instance_id,
        task_id=cmd.task_id,
        action=cmd.action,
        work_item_id=cmd.work_item_id,
    )
    try:
        value = broker.resolve(
            ctx.session,
            _master(ctx),
            workspace_id=_ws(ctx),
            handle=cmd.handle,
            context=context,
            now=ctx.clock.now(),
            actor_uuid=ctx.principal.account_uuid,
            actor_label=ctx.principal.account_id,
            correlation_id=ctx.correlation_id,
            store=ctx.store,
            purpose=cmd.purpose,
        )
    except SecretError as exc:
        raise _err(exc) from exc
    lease_id = ls.LIVE.lease_id(ls.handle_hash(cmd.handle)) or "lease"
    # the value is carried to the transport layer only; it is not part of any Event or audit
    return CommandResult(
        lease_id, "", 0, "secret_lease", data={"secret_b64": base64.b64encode(value).decode()}
    )


@dataclass(frozen=True)
class AckLeaseCleanup(Command):
    lease_id: str
    idempotency_scope: str = "secret_grant:cleanup"


@handles(AckLeaseCleanup)
def ack_lease_cleanup(cmd: AckLeaseCleanup, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "secret.lease", action="api:secret_lease")
    agent_id = _caller_agent(ctx)
    lease = broker.load_lease(ctx.session, cmd.lease_id)
    if lease is None or lease.agent_id != agent_id:
        raise CommandError("SECRET_NOT_FOUND", cmd.lease_id, status=404)
    ok = broker.ack_cleanup(ctx.session, cmd.lease_id, ctx.clock.now())
    return CommandResult(cmd.lease_id, "", 0, "secret_lease", data={"acknowledged": ok})


# ------------------------------------------------------------------ Task-end hook


def revoke_for_task_hook(ctx: CommandContext, task_id: str) -> None:
    """Called by the Task terminal transition (P4-06 §9.3 "revoked at Task end")."""
    broker.revoke_for_task(
        ctx.session,
        workspace_id=_ws(ctx),
        task_id=task_id,
        now=ctx.clock.now(),
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        store=ctx.store,
        actor_uuid=ctx.principal.account_uuid,
    )


def _register_hooks() -> None:
    from server.application import tasks as task_cmds

    task_cmds.register_terminal_hook(revoke_for_task_hook)


_register_hooks()  # leases end with the Task wherever the secret commands are in use
