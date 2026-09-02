"""Approval commands on the common command bus (P1-08). REST/MCP/Mattermost all execute these."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from server.application import bus
from server.application.bus import Command, CommandContext, CommandError, CommandResult, handles
from server.approvals import service
from server.approvals.model import ApprovalError, Subject
from server.policy.authorization import Authorizer, independent_audit_sink
from server.policy.catalog import PolicyCatalog, default_catalog


def _catalog(ctx: CommandContext) -> PolicyCatalog:
    cat = ctx.extras.get("policy_catalog")
    return cat if isinstance(cat, PolicyCatalog) else default_catalog()


def _authorizer(ctx: CommandContext) -> Authorizer:
    """Authorizer for decision paths: denials are audited in their own transaction so the audit
    survives the rejected command's rollback."""
    given = ctx.extras.get("policy_authorizer")
    base = given if isinstance(given, Authorizer) else getattr(ctx.authorizer, "authorizer", None)
    if isinstance(base, Authorizer):
        return Authorizer(base.repository, base.catalog, base.clock, independent_audit_sink)
    return Authorizer(catalog=_catalog(ctx), clock=ctx.clock, audit_sink=independent_audit_sink)


def _translate(exc: ApprovalError) -> CommandError:
    return CommandError(exc.code, exc.detail, status=exc.status)


@dataclass(frozen=True)
class RequestApproval(Command):
    subject_type: str
    subject_id: str
    action: str
    risk: str | None = None
    resource_scope: dict[str, Any] = field(default_factory=dict)
    valid_for_seconds: int | None = None
    max_uses: int | None = None
    implementing_agent_account_uuid: str | None = None
    channel_uuid: str | None = None
    requires_human_approval: bool = False
    idempotency_scope: str = "approval:request"


@handles(RequestApproval)
def handle_request(cmd: RequestApproval, ctx: CommandContext) -> CommandResult:
    bus.require_permission(
        ctx, "approval.request", action="api:approval_request", channel_id=cmd.channel_uuid
    )
    try:
        r = service.request_approval(
            ctx.session,
            ctx.store,
            _catalog(ctx),
            ctx.clock,
            workspace_uuid=uuid.UUID(ctx.workspace_id),
            requested_by=uuid.UUID(ctx.principal.account_uuid),
            subject=Subject(cmd.subject_type, cmd.subject_id),
            action=cmd.action,
            correlation_id=ctx.correlation_id,
            idempotency_key=ctx.idempotency_key,
            resource_scope=cmd.resource_scope,
            risk=cmd.risk,
            valid_for=dt.timedelta(seconds=cmd.valid_for_seconds)
            if cmd.valid_for_seconds
            else None,
            max_uses=cmd.max_uses,
            implementing_agent_account=uuid.UUID(cmd.implementing_agent_account_uuid)
            if cmd.implementing_agent_account_uuid
            else None,
            channel_uuid=uuid.UUID(cmd.channel_uuid) if cmd.channel_uuid else None,
            requires_human_approval=cmd.requires_human_approval,
        )
    except ApprovalError as exc:
        raise _translate(exc) from exc
    return CommandResult(
        r.approval_id,
        r.event.event_id,
        r.event.aggregate_seq,
        "approval",
        r.event.replayed,
        {
            "risk": r.risk,
            "quorum_required": r.quorum_required,
            "expires_at": r.expires_at.isoformat(),
        },
    )


@dataclass(frozen=True)
class DecideApproval(Command):
    approval_id: str
    decision: str  # APPROVE | REJECT
    reason_code: str = "REJECTED_BY_APPROVER"
    idempotency_scope: str = "approval:decide"


@handles(DecideApproval)
def handle_decide(cmd: DecideApproval, ctx: CommandContext) -> CommandResult:
    # eligibility performs the policy evaluation (permission, membership, risk) and audits denials
    try:
        r = service.decide_approval(
            ctx.session,
            ctx.store,
            _authorizer(ctx),
            _catalog(ctx),
            ctx.clock,
            approval_id=cmd.approval_id,
            approver_account_id=ctx.principal.account_id,
            decision=cmd.decision,
            credential_fingerprint=ctx.principal.credential_fingerprint,
            reauth_verified=bool(ctx.extras.get("reauth_verified", False))
            or ctx.principal.mfa_verified,
            correlation_id=ctx.correlation_id,
            idempotency_key=ctx.idempotency_key,
            reason_code=cmd.reason_code,
        )
    except ApprovalError as exc:
        raise _translate(exc) from exc
    return CommandResult(
        r.approval_id,
        r.event.event_id if r.event else "",
        r.event.aggregate_seq if r.event else 0,
        "approval",
        False,
        {
            "status": r.status.value,
            "approvals_recorded": r.approvals_recorded,
            "quorum_required": r.quorum_required,
        },
    )


@dataclass(frozen=True)
class CancelApproval(Command):
    approval_id: str
    reason_code: str = "CANCELLED_BY_REQUESTER"
    idempotency_scope: str = "approval:cancel"


@handles(CancelApproval)
def handle_cancel(cmd: CancelApproval, ctx: CommandContext) -> CommandResult:
    bus.require_permission(ctx, "approval.request", action="api:approval_request")
    try:
        r = service.cancel_approval(
            ctx.session,
            ctx.store,
            ctx.clock,
            approval_id=cmd.approval_id,
            actor_uuid=uuid.UUID(ctx.principal.account_uuid),
            correlation_id=ctx.correlation_id,
            idempotency_key=ctx.idempotency_key,
            reason_code=cmd.reason_code,
        )
    except ApprovalError as exc:
        raise _translate(exc) from exc
    return CommandResult(cmd.approval_id, r.event_id, r.aggregate_seq, "approval", r.replayed)


@dataclass(frozen=True)
class RevokeApproval(Command):
    approval_id: str
    reason_code: str = "REVOKED"
    idempotency_scope: str = "approval:revoke"


@handles(RevokeApproval)
def handle_revoke(cmd: RevokeApproval, ctx: CommandContext) -> CommandResult:
    bus.require_permission(ctx, "approval.revoke", action="api:approval_revoke")
    try:
        r = service.revoke_approval(
            ctx.session,
            ctx.store,
            ctx.clock,
            approval_id=cmd.approval_id,
            actor_uuid=uuid.UUID(ctx.principal.account_uuid),
            correlation_id=ctx.correlation_id,
            idempotency_key=ctx.idempotency_key,
            reason_code=cmd.reason_code,
        )
    except ApprovalError as exc:
        raise _translate(exc) from exc
    return CommandResult(cmd.approval_id, r.event_id, r.aggregate_seq, "approval", r.replayed)


@dataclass(frozen=True)
class ExpireApprovals(Command):
    """Scheduler/ops job: expire and escalate every grant whose validity ended."""

    idempotency_scope: str = "approval:expire"


@handles(ExpireApprovals)
def handle_expire(cmd: ExpireApprovals, ctx: CommandContext) -> CommandResult:
    bus.require_permission(ctx, "approval.revoke", action="api:approval_revoke")
    r = service.expire_approvals(
        ctx.session,
        ctx.store,
        _catalog(ctx),
        ctx.clock,
        actor_uuid=uuid.UUID(ctx.principal.account_uuid),
        correlation_id=ctx.correlation_id,
        workspace_uuid=uuid.UUID(ctx.workspace_id),
    )
    return CommandResult(
        "", "", 0, "approval", False, {"expired": r.expired, "escalated_to": r.escalated_to}
    )


@dataclass(frozen=True)
class ConsumeApproval(Command):
    """Internal command API (§7.2): used by Task/Run execution paths, never exposed to clients."""

    approval_id: str
    consumption_key: str
    consumed_for_type: str
    consumed_for_id: str
    idempotency_scope: str = "approval:consume"


@handles(ConsumeApproval)
def handle_consume(cmd: ConsumeApproval, ctx: CommandContext) -> CommandResult:
    try:
        r = service.consume_approval(
            ctx.session,
            ctx.store,
            ctx.clock,
            approval_id=cmd.approval_id,
            consumption_key=cmd.consumption_key,
            consumed_by=uuid.UUID(ctx.principal.account_uuid),
            consumed_for=Subject(cmd.consumed_for_type, cmd.consumed_for_id),
            correlation_id=ctx.correlation_id,
        )
    except ApprovalError as exc:
        raise _translate(exc) from exc
    return CommandResult(
        r.approval_id,
        r.event_id,
        0,
        "approval",
        r.replayed,
        {"used_count": r.used_count, "status": r.status.value},
    )
