"""Role / Capability administration on the bus (P3-02; spec §4.3, development plan §16).

Roles are policy objects: ``CreateRole`` and ``CommitRoleVersion`` append an immutable
RoleVersion (``ROLE_VERSION_CREATED``), ``AssignRole``/``RevokeRole`` bind Roles to any Account
(Human, Agent, service) with ``PRINCIPAL_ROLE_ASSIGNED``/``PRINCIPAL_ROLE_REVOKED``. The Policy
Engine reads ``roles.current_version`` at decision time, so the first authorization after a
commit already sees the new version (V-P3-02). ``effective_preview`` explains a decision without
auditing it (explicit deny > scope restriction > allow).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
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
from server.events.store import AppendRequest, AppendResult
from server.observability.audit import append_audit
from server.policy.catalog import PolicyCatalog, default_catalog, permission_in_vocabulary
from server.policy.engine import PolicyEngine
from server.policy.model import ActionRequest, Decision
from server.policy.repository import (
    PolicyRepositoryError,
    PostgresPolicyRepository,
    policy_hash,
)

ROLE_ID_PREFIX = "role-"


@dataclass(frozen=True)
class CreateRole(Command):
    role_id: str
    display_name: str
    permissions: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)
    idempotency_scope: str = "role:create"


@dataclass(frozen=True)
class CommitRoleVersion(Command):
    role_id: str
    permissions: tuple[str, ...]
    deny: tuple[str, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)
    idempotency_scope: str = "role:commit"


@dataclass(frozen=True)
class AssignRole(Command):
    account_id: str  # public account id (Human, Agent or service)
    role_id: str
    scope: dict[str, Any] = field(default_factory=dict)
    valid_to: str | None = None  # ISO-8601
    idempotency_scope: str = "account:role_assign"


@dataclass(frozen=True)
class RevokeRole(Command):
    account_id: str
    role_id: str
    reason_code: str = "ADMIN_REVOKE"
    idempotency_scope: str = "account:role_revoke"


def _ws(ctx: CommandContext) -> uuid.UUID:
    return uuid.UUID(ctx.workspace_id)


def _validate_role_id(role_id: str) -> None:
    if not role_id.startswith(ROLE_ID_PREFIX) or len(role_id) < 6 or " " in role_id:
        raise CommandError("ROLE_ID_INVALID", "expected role-<slug>", status=400)


def _validate_permissions(
    catalog: PolicyCatalog, permissions: tuple[str, ...], deny: tuple[str, ...]
) -> None:
    vocab = catalog.vocabulary()
    for pattern in (*permissions, *deny):
        if not permission_in_vocabulary(pattern, vocab):
            raise CommandError("PERMISSION_UNKNOWN", pattern, status=400)
    if not permissions and not deny:
        raise CommandError("ROLE_EMPTY", "a RoleVersion needs permissions or deny entries", 400)


def _validate_constraints(constraints: dict[str, Any]) -> None:
    allowed = {"domains", "channels", "resources", "side_effects", "requires_human_approval"}
    unknown = sorted(set(constraints) - allowed)
    if unknown:
        raise CommandError("ROLE_CONSTRAINT_INVALID", ", ".join(unknown), status=400)
    if constraints.get("side_effects") not in (None, "allow", "deny"):
        raise CommandError("ROLE_CONSTRAINT_INVALID", "side_effects must be allow|deny", 400)


def _replay(
    ctx: CommandContext, aggregate_type: str, aggregate_id: str, scope: str
) -> AppendResult | None:
    for ev in ctx.store.stream(ctx.workspace_id, aggregate_type, aggregate_id):
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
    ctx: CommandContext,
    cmd: Command,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> AppendResult:
    return ctx.store.append(
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            type=event_type,
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=cmd.idempotency_scope,
            idempotency_key=ctx.idempotency_key,
            payload=payload,
        )
    )


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


def _catalog(ctx: CommandContext) -> PolicyCatalog:
    catalog = ctx.extras.get("policy_catalog")
    return catalog if isinstance(catalog, PolicyCatalog) else default_catalog()


_ROLE_ROW = text(
    "SELECT role_id, display_name, current_version, status FROM roles "
    "WHERE role_id = :r AND workspace_id = :w"
)
_ROLE_ROW_LOCKED = text(
    "SELECT role_id, display_name, current_version, status FROM roles "
    "WHERE role_id = :r AND workspace_id = :w FOR UPDATE"
)


def _role_row(ctx: CommandContext, role_id: str, *, lock: bool = False) -> Any:
    row = (
        ctx.session.execute(
            _ROLE_ROW_LOCKED if lock else _ROLE_ROW,
            {"r": role_id, "w": _ws(ctx)},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CommandError("ROLE_NOT_FOUND", role_id, status=404)
    return row


def _account(ctx: CommandContext, public_id: str) -> tuple[uuid.UUID, str]:
    row = ctx.session.execute(
        text("SELECT id, account_type FROM accounts WHERE account_id = :a AND workspace_id = :w"),
        {"a": public_id, "w": _ws(ctx)},
    ).first()
    if row is None:
        raise CommandError("ACCOUNT_NOT_FOUND", public_id, status=404)
    return uuid.UUID(str(row[0])), str(row[1])


@handles(CreateRole)
def create_role(cmd: CreateRole, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:role_create")
    _validate_role_id(cmd.role_id)
    if not cmd.display_name.strip():
        raise CommandError("ROLE_DISPLAY_NAME_REQUIRED", "display_name", status=400)
    _validate_permissions(_catalog(ctx), cmd.permissions, cmd.deny)
    _validate_constraints(cmd.constraints)
    exists = ctx.session.execute(
        text("SELECT workspace_id FROM roles WHERE role_id = :r"), {"r": cmd.role_id}
    ).first()
    if exists is not None:
        replay = _replay(ctx, "role", cmd.role_id, cmd.idempotency_scope)
        if replay is not None and exists[0] == _ws(ctx):
            return CommandResult(cmd.role_id, replay.event_id, replay.aggregate_seq, "role", True)
        raise CommandError("ROLE_ALREADY_EXISTS", cmd.role_id, status=409)
    repo = PostgresPolicyRepository()
    repo.create_role(ctx.session, _ws(ctx), cmd.role_id, cmd.display_name.strip())
    version, digest = 1, policy_hash(list(cmd.permissions), list(cmd.deny), dict(cmd.constraints))
    res = _append(  # the Event first: role_versions rows are immutable and reference it
        ctx,
        cmd,
        "role",
        cmd.role_id,
        "ROLE_VERSION_CREATED",
        {
            "role_id": cmd.role_id,
            "role_version": version,
            "permissions_hash": digest,
            "permissions": sorted(cmd.permissions),
            "deny": sorted(cmd.deny),
            "constraints": dict(cmd.constraints),
        },
    )
    repo.commit_role_version(
        ctx.session,
        cmd.role_id,
        list(cmd.permissions),
        list(cmd.deny),
        dict(cmd.constraints),
        uuid.UUID(ctx.principal.account_uuid),
        event_id=res.event_id,
    )
    _audit(ctx, "role.create", "role", cmd.role_id, role_version=version, policy_hash=digest)
    return CommandResult(
        cmd.role_id,
        res.event_id,
        res.aggregate_seq,
        "role",
        data={"role_version": version, "policy_hash": digest},
    )


@handles(CommitRoleVersion)
def commit_role_version(cmd: CommitRoleVersion, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:role_update")
    row = _role_row(ctx, cmd.role_id, lock=True)
    replay = _replay(ctx, "role", cmd.role_id, cmd.idempotency_scope)
    if replay is not None:
        return CommandResult(cmd.role_id, replay.event_id, replay.aggregate_seq, "role", True)
    if row["status"] != "active":
        raise CommandError("ROLE_RETIRED", cmd.role_id, status=409)
    _validate_permissions(_catalog(ctx), cmd.permissions, cmd.deny)
    _validate_constraints(cmd.constraints)
    repo = PostgresPolicyRepository()
    version = int(row["current_version"]) + 1
    digest = policy_hash(list(cmd.permissions), list(cmd.deny), dict(cmd.constraints))
    res = _append(  # Event first (immutable role_versions row references it)
        ctx,
        cmd,
        "role",
        cmd.role_id,
        "ROLE_VERSION_CREATED",
        {
            "role_id": cmd.role_id,
            "role_version": version,
            "permissions_hash": digest,
            "permissions": sorted(cmd.permissions),
            "deny": sorted(cmd.deny),
            "constraints": dict(cmd.constraints),
        },
    )
    try:
        committed, _ = repo.commit_role_version(
            ctx.session,
            cmd.role_id,
            list(cmd.permissions),
            list(cmd.deny),
            dict(cmd.constraints),
            uuid.UUID(ctx.principal.account_uuid),
            event_id=res.event_id,
        )
    except PolicyRepositoryError as exc:
        raise CommandError(exc.code, exc.detail, status=404) from exc
    if committed != version:  # the FOR UPDATE row lock makes this unreachable; keep it honest
        raise CommandError("ROLE_VERSION_CONFLICT", f"{committed} != {version}", status=409)
    _audit(ctx, "role.commit", "role", cmd.role_id, role_version=version, policy_hash=digest)
    return CommandResult(
        cmd.role_id,
        res.event_id,
        res.aggregate_seq,
        "role",
        data={"role_version": version, "policy_hash": digest},
    )


@handles(AssignRole)
def assign_role(cmd: AssignRole, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:role_assign")
    row = _role_row(ctx, cmd.role_id)
    account_uuid, _ = _account(ctx, cmd.account_id)
    replay = _replay(ctx, "account", cmd.account_id, cmd.idempotency_scope)
    if replay is not None:
        return CommandResult(cmd.account_id, replay.event_id, replay.aggregate_seq, "account", True)
    active = ctx.session.execute(
        text(
            "SELECT 1 FROM principal_role_assignments WHERE account_id = :a AND role_id = :r "
            "AND revoked_at IS NULL"
        ),
        {"a": account_uuid, "r": cmd.role_id},
    ).first()
    if active is not None:
        raise CommandError("ROLE_ALREADY_ASSIGNED", f"{cmd.account_id}:{cmd.role_id}", 409)
    valid_to = None
    if cmd.valid_to:
        try:
            valid_to = dt.datetime.fromisoformat(cmd.valid_to.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CommandError("ROLE_VALID_TO_INVALID", cmd.valid_to, status=400) from exc
    res = _append(
        ctx,
        cmd,
        "account",
        cmd.account_id,
        "PRINCIPAL_ROLE_ASSIGNED",
        {
            "account_id": cmd.account_id,
            "role_id": cmd.role_id,
            "role_version": int(row["current_version"]),
            "scope": dict(cmd.scope),
        },
    )
    PostgresPolicyRepository().assign_role(
        ctx.session,
        account_uuid,
        cmd.role_id,
        uuid.UUID(ctx.principal.account_uuid),
        ctx.clock.now(),
        valid_to=valid_to,
        scope=dict(cmd.scope),
        event_id=res.event_id,
    )
    _audit(ctx, "role.assign", "account", cmd.account_id, role_id=cmd.role_id)
    return CommandResult(cmd.account_id, res.event_id, res.aggregate_seq, "account")


@handles(RevokeRole)
def revoke_role(cmd: RevokeRole, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:role_revoke")
    _role_row(ctx, cmd.role_id)
    account_uuid, _ = _account(ctx, cmd.account_id)
    replay = _replay(ctx, "account", cmd.account_id, cmd.idempotency_scope)
    if replay is not None:
        return CommandResult(cmd.account_id, replay.event_id, replay.aggregate_seq, "account", True)
    res = _append(
        ctx,
        cmd,
        "account",
        cmd.account_id,
        "PRINCIPAL_ROLE_REVOKED",
        {"account_id": cmd.account_id, "role_id": cmd.role_id, "reason_code": cmd.reason_code},
    )
    revoked = PostgresPolicyRepository().revoke_role(
        ctx.session, account_uuid, cmd.role_id, ctx.clock.now(), revoke_event_id=res.event_id
    )
    if revoked == 0:
        raise CommandError("ROLE_NOT_ASSIGNED", f"{cmd.account_id}:{cmd.role_id}", status=409)
    _audit(ctx, "role.revoke", "account", cmd.account_id, role_id=cmd.role_id)
    return CommandResult(
        cmd.account_id, res.event_id, res.aggregate_seq, "account", data={"revoked": revoked}
    )


# ------------------------------------------------------------------ reads


def role_view(session: Session, workspace_id: uuid.UUID, role_id: str) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT role_id, display_name, current_version, status, created_at FROM roles "
                "WHERE role_id = :r AND workspace_id = :w"
            ),
            {"r": role_id, "w": workspace_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    versions = (
        session.execute(
            text(
                "SELECT version, permissions, deny, constraints, policy_hash, event_id, "
                "created_at FROM role_versions WHERE role_id = :r ORDER BY version"
            ),
            {"r": role_id},
        )
        .mappings()
        .all()
    )
    return {
        "role_id": row["role_id"],
        "display_name": row["display_name"],
        "current_version": int(row["current_version"]),
        "status": row["status"],
        "versions": [
            {
                "version": int(v["version"]),
                "permissions": list(v["permissions"]),
                "deny": list(v["deny"]),
                "constraints": dict(v["constraints"]),
                "policy_hash": v["policy_hash"],
                "event_id": v["event_id"],
                "created_at": v["created_at"].isoformat() if v["created_at"] else None,
            }
            for v in versions
        ],
    }


def list_roles(session: Session, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                "SELECT role_id, display_name, current_version, status FROM roles "
                "WHERE workspace_id = :w ORDER BY role_id LIMIT 100"
            ),
            {"w": workspace_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def effective_preview(
    session: Session,
    workspace_id: uuid.UUID,
    account_id: str,
    now: dt.datetime,
    *,
    permission: str | None = None,
    domain: str | None = None,
    channel_id: str | None = None,
    resource: str | None = None,
    side_effect: bool = False,
    catalog: PolicyCatalog | None = None,
) -> dict[str, Any]:
    """Effective Roles of an Account and, when ``permission`` is given, the explained decision.

    Pure read: no audit row, no Event. Precedence mirrors the engine (explicit deny > scope >
    allow), so the preview equals what the next real authorization will decide.
    """
    repo = PostgresPolicyRepository()
    principal = repo.principal(session, account_id)
    if principal is None or principal.workspace_uuid != workspace_id:
        raise CommandError("ACCOUNT_NOT_FOUND", account_id, status=404)
    roles = repo.effective_roles(session, principal, now)
    capabilities = sorted(repo.capability_ids(session, principal))
    out: dict[str, Any] = {
        "account_id": account_id,
        "account_type": principal.account_type,
        "account_status": principal.status,
        "computed_at": now.isoformat(),
        "roles": [
            {
                "role_id": r.role_id,
                "version": r.version,
                "permissions": sorted(r.permissions),
                "deny": sorted(r.deny),
                "constraints": {
                    "domains": None
                    if r.constraints.domains is None
                    else sorted(r.constraints.domains),
                    "channels": None
                    if r.constraints.channels is None
                    else sorted(r.constraints.channels),
                    "resources": None
                    if r.constraints.resources is None
                    else sorted(r.constraints.resources),
                    "side_effects": r.constraints.side_effects,
                    "requires_human_approval": sorted(r.constraints.requires_human_approval),
                },
            }
            for r in roles
        ],
        "capabilities": capabilities,
        "effective_permissions": sorted({p for r in roles for p in r.permissions}),
        "effective_deny": sorted({p for r in roles for p in r.deny}),
    }
    if permission:
        engine: PolicyEngine = (catalog or default_catalog()).engine()
        decision = engine.evaluate(
            roles,
            ActionRequest(
                permission=permission,
                domain=domain,
                side_effect=side_effect,
                channel_id=channel_id,
                resource=resource,
            ),
        )
        allowed = decision.decision is Decision.ALLOW and principal.status == "ACTIVE"
        out["decision"] = {
            "permission": permission,
            "allowed": allowed,
            "reason": "PRINCIPAL_INACTIVE"
            if decision.decision is Decision.ALLOW and not allowed
            else str(decision.reason),
            "matched_roles": list(decision.matched_roles),
            "requires_human_approval": decision.requires_human_approval,
            "precedence": "explicit deny > scope restriction > allow",
        }
    return out
