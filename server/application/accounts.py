"""Account administration on the command bus (P4-01; spec §4.1, development plan §11.1).

Humans and service Accounts are created here; Agents are registered through the Phase 3 registry
(their Account is created by ``RegisterAgent``). Every change appends an ``ACCOUNT_*`` Event and
a redacted audit row. Suspension and deletion requests check live references (assigned Tasks,
channel memberships, Agents, role assignments) and report them with ``ACCOUNT_HAS_REFERENCES``.
Deletion never removes rows: it starts the P4-11 hard-delete workflow.
"""

from __future__ import annotations

import re
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
from server.events.store import AppendRequest, AppendResult, EventStoreError
from server.identity.principals import (
    fingerprint_of,
    issue_service_token,
    revoke_service_token,
    rotate_service_token,
)
from server.observability.audit import append_audit

ACCOUNT_ID_RE = re.compile(r"^acct-[a-z0-9][a-z0-9-]{1,62}$")
TERMINAL_TASKS = ("COMPLETED", "CANCELLED", "VERIFIED")


@dataclass(frozen=True)
class CreateAccount(Command):
    account_id: str
    display_name: str
    account_type: str = "human"  # human | service (agents: /api/v1/agents)
    auth_subject: str | None = None
    roles: tuple[str, ...] = ()
    issue_token: bool = False
    idempotency_scope: str = "account:create"


@dataclass(frozen=True)
class UpdateAccount(Command):
    account_id: str
    display_name: str | None = None
    auth_subject: str | None = None
    idempotency_scope: str = "account:update"


@dataclass(frozen=True)
class SuspendAccount(Command):
    account_id: str
    reason_code: str = "ADMIN_SUSPEND"
    force: bool = False  # suspend even with live references (they are reported either way)
    idempotency_scope: str = "account:suspend"


@dataclass(frozen=True)
class ReinstateAccount(Command):
    account_id: str
    reason_code: str = "ADMIN_REINSTATE"
    idempotency_scope: str = "account:reinstate"


@dataclass(frozen=True)
class RequestAccountDeletion(Command):
    account_id: str
    reason: str
    idempotency_scope: str = "account:deletion_request"


@dataclass(frozen=True)
class IssueCredential(Command):
    account_id: str
    idempotency_scope: str = "account:credential_issue"


@dataclass(frozen=True)
class RotateCredential(Command):
    account_id: str
    old_fingerprint: str
    idempotency_scope: str = "account:credential_rotate"


@dataclass(frozen=True)
class RevokeCredential(Command):
    account_id: str
    fingerprint: str
    idempotency_scope: str = "account:credential_revoke"


@dataclass(frozen=True)
class AccountRow:
    id: uuid.UUID
    account_id: str
    workspace_id: uuid.UUID
    account_type: str
    status: str
    display_name: str
    auth_subject: str | None
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ helpers


def _ws(ctx: CommandContext) -> uuid.UUID:
    return uuid.UUID(ctx.workspace_id)


def load_account(
    session: Session, workspace_id: uuid.UUID, account_id: str, *, lock: bool = False
) -> AccountRow | None:
    row = session.execute(
        text(
            "SELECT id, account_id, workspace_id, account_type, status, display_name, auth_subject "
            "FROM accounts WHERE account_id = :a AND workspace_id = :w"
            + (" FOR UPDATE" if lock else "")
        ),
        {"a": account_id, "w": workspace_id},
    ).first()
    if row is None:
        return None
    return AccountRow(row[0], str(row[1]), row[2], str(row[3]), str(row[4]), str(row[5]), row[6])


def _load(ctx: CommandContext, account_id: str, *, lock: bool = True) -> AccountRow:
    row = load_account(ctx.session, _ws(ctx), account_id, lock=lock)
    if row is None:
        raise CommandError("ACCOUNT_NOT_FOUND", account_id, status=404)
    return row


def references_of(session: Session, row: AccountRow) -> dict[str, Any]:
    """Live references that a suspension/deletion must consider (never removed silently)."""
    tasks = session.execute(
        text(
            "SELECT task_id, status FROM tasks_projection WHERE assignee_account_id = :a "
            "AND status NOT IN ('COMPLETED','CANCELLED','VERIFIED') ORDER BY task_id"
        ),
        {"a": row.id},
    ).all()
    channels = session.execute(
        text(
            "SELECT c.channel_id FROM channel_members m JOIN channels c ON c.id = m.channel_id "
            "WHERE m.account_id = :a AND m.status = 'active' ORDER BY c.channel_id"
        ),
        {"a": row.id},
    ).all()
    agent = session.execute(
        text("SELECT agent_id, status FROM agents WHERE account_id = :a"), {"a": row.id}
    ).first()
    roles = session.execute(
        text(
            "SELECT role_id FROM principal_role_assignments WHERE account_id = :a "
            "AND revoked_at IS NULL AND (valid_to IS NULL OR valid_to > now()) ORDER BY role_id"
        ),
        {"a": row.id},
    ).all()
    return {
        "open_tasks": [{"task_id": str(t[0]), "status": str(t[1])} for t in tasks],
        "channels": [str(c[0]) for c in channels],
        "agent": None if agent is None else {"agent_id": str(agent[0]), "status": str(agent[1])},
        "roles": [str(r[0]) for r in roles],
    }


def _replay(ctx: CommandContext, account_id: str, scope: str) -> AppendResult | None:
    """Idempotent retries return the original Event (same scope/key)."""
    row = ctx.session.execute(
        text(
            "SELECT event_id, aggregate_seq, content_hash, recorded_seq FROM events "
            "WHERE workspace_id = :w AND aggregate_type = 'account' AND aggregate_id = :a "
            "AND idempotency_scope = :s AND idempotency_key = :k"
        ),
        {"w": _ws(ctx), "a": account_id, "s": scope, "k": ctx.idempotency_key},
    ).first()
    if row is None:
        return None
    return AppendResult(str(row[0]), int(row[1]), str(row[2]), int(row[3]), replayed=True)


def _append(
    ctx: CommandContext, cmd: Command, account_id: str, event_type: str, payload: dict[str, Any]
) -> AppendResult:
    try:
        return ctx.store.append(
            AppendRequest(
                workspace_id=ctx.workspace_id,
                aggregate_type="account",
                aggregate_id=account_id,
                type=event_type,
                actor_account_id=ctx.principal.account_uuid,
                correlation_id=ctx.correlation_id,
                idempotency_scope=cmd.idempotency_scope,
                idempotency_key=ctx.idempotency_key,
                payload=payload,
            )
        )
    except EventStoreError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc


def _audit(ctx: CommandContext, action: str, account_id: str, **meta: Any) -> None:
    append_audit(
        ctx.session,
        action=action,
        target_type="account",
        target_id=account_id,
        result="OK",
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=_ws(ctx),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata=meta,
        clock=ctx.clock,
    )


def _result(res: AppendResult, account_id: str, **data: Any) -> CommandResult:
    return CommandResult(
        account_id, res.event_id, res.aggregate_seq, "account", replayed=res.replayed, data=data
    )


def account_view(
    session: Session, workspace_id: uuid.UUID, account_id: str
) -> dict[str, Any] | None:
    row = load_account(session, workspace_id, account_id)
    if row is None:
        return None
    creds = session.execute(
        text(
            "SELECT fingerprint, status, created_at, revoked_at FROM service_credentials "
            "WHERE account_id = :a ORDER BY created_at"
        ),
        {"a": row.id},
    ).all()
    pending = session.execute(
        text(
            "SELECT request_id, status FROM hard_delete_requests WHERE target_type = 'account' "
            "AND target_id = :a AND status IN ('PENDING_APPROVAL','APPROVED_WAITING')"
        ),
        {"a": account_id},
    ).first()
    return {
        "account_id": row.account_id,
        "account_type": row.account_type,
        "status": row.status,
        "display_name": row.display_name,
        "auth_subject": row.auth_subject,
        "credentials": [
            {
                "fingerprint": str(c[0]),
                "status": str(c[1]),
                "created_at": c[2].isoformat() if c[2] else None,
                "revoked_at": c[3].isoformat() if c[3] else None,
            }
            for c in creds
        ],
        "references": references_of(session, row),
        "deletion_request": None
        if pending is None
        else {"request_id": str(pending[0]), "status": str(pending[1])},
    }


def list_accounts(
    session: Session, workspace_id: uuid.UUID, *, account_type: str | None = None
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT account_id, account_type, status, display_name FROM accounts "
            "WHERE workspace_id = :w AND (CAST(:t AS text) IS NULL OR "
            "account_type = CAST(:t AS text)) "
            "ORDER BY account_id"
        ),
        {"w": workspace_id, "t": account_type},
    ).all()
    return [
        {
            "account_id": str(r[0]),
            "account_type": str(r[1]),
            "status": str(r[2]),
            "display_name": str(r[3]),
        }
        for r in rows
    ]


# ------------------------------------------------------------------ handlers


@handles(CreateAccount)
def create_account(cmd: CreateAccount, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:account_create")
    if cmd.account_type == "agent":
        raise CommandError(
            "ACCOUNT_TYPE_AGENT_VIA_REGISTRY",
            "Agents are registered through POST /api/v1/agents (the registry creates the Account)",
            status=400,
        )
    if cmd.account_type not in ("human", "service"):
        raise CommandError("ACCOUNT_TYPE_INVALID", cmd.account_type, status=400)
    if not ACCOUNT_ID_RE.match(cmd.account_id):
        raise CommandError("ACCOUNT_ID_INVALID", cmd.account_id, status=400)
    if not cmd.display_name.strip():
        raise CommandError("ACCOUNT_DISPLAY_NAME_REQUIRED", cmd.account_id, status=400)
    replay = _replay(ctx, cmd.account_id, cmd.idempotency_scope)
    if replay is not None:
        return _result(replay, cmd.account_id)
    if load_account(ctx.session, _ws(ctx), cmd.account_id) is not None:
        raise CommandError("ACCOUNT_ALREADY_EXISTS", cmd.account_id, status=409)
    account_uuid = uuid.uuid4()
    ctx.session.execute(
        text(
            "INSERT INTO accounts (id, account_id, workspace_id, account_type, status, "
            "display_name, auth_subject) VALUES (:i, :a, :w, :t, 'ACTIVE', :d, :s)"
        ),
        {
            "i": account_uuid,
            "a": cmd.account_id,
            "w": _ws(ctx),
            "t": cmd.account_type,
            "d": cmd.display_name.strip(),
            "s": cmd.auth_subject,
        },
    )
    assigned: list[str] = []
    if cmd.roles:
        from server.policy.repository import PostgresPolicyRepository

        repo = PostgresPolicyRepository()
        for role_id in cmd.roles:
            exists = ctx.session.execute(
                text("SELECT 1 FROM roles WHERE role_id = :r AND workspace_id = :w"),
                {"r": role_id, "w": _ws(ctx)},
            ).first()
            if exists is None:
                raise CommandError("ROLE_NOT_FOUND", role_id, status=404)
            repo.assign_role(
                ctx.session,
                account_uuid,
                role_id,
                uuid.UUID(ctx.principal.account_uuid),
                ctx.clock.now(),
            )
            assigned.append(role_id)
    token: str | None = None
    fingerprint: str | None = None
    if cmd.issue_token:
        token, fingerprint = issue_service_token(
            ctx.session,
            cmd.account_id,
            actor_label=ctx.principal.account_id,
            correlation_id=ctx.correlation_id,
            clock=ctx.clock,
        )
    res = _append(
        ctx,
        cmd,
        cmd.account_id,
        "ACCOUNT_CREATED",
        {"account_id": cmd.account_id, "account_type": cmd.account_type, "roles": assigned},
    )
    _audit(
        ctx,
        "account.create",
        cmd.account_id,
        account_type=cmd.account_type,
        roles=assigned,
        credential_fingerprint=fingerprint,
    )
    data: dict[str, Any] = {"roles": assigned, "credential_fingerprint": fingerprint}
    if token is not None:
        data["service_token"] = token  # returned exactly once; only the hash is stored
    return _result(res, cmd.account_id, **data)


@handles(UpdateAccount)
def update_account(cmd: UpdateAccount, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:account_update")
    row = _load(ctx, cmd.account_id)
    replay = _replay(ctx, cmd.account_id, cmd.idempotency_scope)
    if replay is not None:
        return _result(replay, cmd.account_id)
    if row.status == "DELETED":
        raise CommandError("ACCOUNT_DELETED", cmd.account_id, status=409)
    changes: dict[str, Any] = {}
    if cmd.display_name is not None and cmd.display_name.strip() != row.display_name:
        if not cmd.display_name.strip():
            raise CommandError("ACCOUNT_DISPLAY_NAME_REQUIRED", cmd.account_id, status=400)
        changes["display_name"] = cmd.display_name.strip()
    if cmd.auth_subject is not None and cmd.auth_subject != row.auth_subject:
        changes["auth_subject"] = cmd.auth_subject
    if not changes:
        raise CommandError("ACCOUNT_NO_CHANGES", cmd.account_id, status=400)
    sets = ", ".join(f"{k} = :{k}" for k in changes)
    ctx.session.execute(
        text(f"UPDATE accounts SET {sets} WHERE id = :i"),  # noqa: S608 - column names are fixed keys
        {**changes, "i": row.id},
    )
    res = _append(
        ctx,
        cmd,
        cmd.account_id,
        "ACCOUNT_UPDATED",
        {"account_id": cmd.account_id, "fields": sorted(changes)},
    )
    _audit(ctx, "account.update", cmd.account_id, fields=sorted(changes))
    return _result(res, cmd.account_id, fields=sorted(changes))


@handles(SuspendAccount)
def suspend_account(cmd: SuspendAccount, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:account_suspend")
    row = _load(ctx, cmd.account_id)
    replay = _replay(ctx, cmd.account_id, cmd.idempotency_scope)
    if replay is not None:
        return _result(replay, cmd.account_id)
    if row.status != "ACTIVE":
        raise CommandError(
            "ACCOUNT_STATUS_INVALID", f"cannot suspend from {row.status}", status=409
        )
    if row.id == uuid.UUID(ctx.principal.account_uuid):
        raise CommandError("ACCOUNT_SELF_SUSPEND", cmd.account_id, status=409)
    refs = references_of(ctx.session, row)
    if refs["open_tasks"] and not cmd.force:
        raise CommandError(
            "ACCOUNT_HAS_REFERENCES",
            f"{len(refs['open_tasks'])} open Task(s); pass force to suspend anyway",
            status=409,
            extra={"references": refs},
        )
    ctx.session.execute(
        text("UPDATE accounts SET status = 'SUSPENDED' WHERE id = :i"), {"i": row.id}
    )
    if row.account_type == "agent" and refs["agent"] is not None:
        # keep the registry consistent: the Agent stops receiving work at once
        ctx.session.execute(
            text(
                "UPDATE agents SET status = 'suspended', online = false, updated_at = :n "
                "WHERE account_id = :i AND status NOT IN ('revoked')"
            ),
            {"n": ctx.clock.now(), "i": row.id},
        )
    res = _append(
        ctx,
        cmd,
        cmd.account_id,
        "ACCOUNT_SUSPENDED",
        {"account_id": cmd.account_id, "reason_code": cmd.reason_code, "references": refs},
    )
    _audit(ctx, "account.suspend", cmd.account_id, reason_code=cmd.reason_code, references=refs)
    return _result(res, cmd.account_id, references=refs)


@handles(ReinstateAccount)
def reinstate_account(cmd: ReinstateAccount, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:account_reinstate")
    row = _load(ctx, cmd.account_id)
    replay = _replay(ctx, cmd.account_id, cmd.idempotency_scope)
    if replay is not None:
        return _result(replay, cmd.account_id)
    if row.status != "SUSPENDED":
        raise CommandError(
            "ACCOUNT_STATUS_INVALID", f"cannot reinstate from {row.status}", status=409
        )
    pending = ctx.session.execute(
        text(
            "SELECT request_id FROM hard_delete_requests WHERE target_type = 'account' AND "
            "target_id = :a AND status IN ('PENDING_APPROVAL','APPROVED_WAITING')"
        ),
        {"a": cmd.account_id},
    ).first()
    if pending is not None:
        raise CommandError("ACCOUNT_DELETION_PENDING", str(pending[0]), status=409)
    ctx.session.execute(text("UPDATE accounts SET status = 'ACTIVE' WHERE id = :i"), {"i": row.id})
    res = _append(
        ctx,
        cmd,
        cmd.account_id,
        "ACCOUNT_REINSTATED",
        {"account_id": cmd.account_id, "reason_code": cmd.reason_code},
    )
    _audit(ctx, "account.reinstate", cmd.account_id, reason_code=cmd.reason_code)
    return _result(res, cmd.account_id)


@handles(RequestAccountDeletion)
def request_account_deletion(cmd: RequestAccountDeletion, ctx: CommandContext) -> CommandResult:
    """Deletion is never immediate: it opens the dual-approval hard-delete workflow (P4-11)."""
    require_permission(ctx, "admin.accounts", action="api:account_delete_request")
    from server.application import hard_delete as hd

    row = _load(ctx, cmd.account_id)
    replay = _replay(ctx, cmd.account_id, cmd.idempotency_scope)
    if replay is not None:
        return _result(replay, cmd.account_id)
    if row.status == "DELETED":
        raise CommandError("ACCOUNT_DELETED", cmd.account_id, status=409)
    if row.id == uuid.UUID(ctx.principal.account_uuid):
        raise CommandError("ACCOUNT_SELF_DELETE", cmd.account_id, status=409)
    refs = references_of(ctx.session, row)
    if refs["open_tasks"]:
        raise CommandError(
            "ACCOUNT_HAS_REFERENCES",
            "open Tasks must be reassigned or cancelled before deletion",
            status=409,
            extra={"references": refs},
        )
    if row.status == "ACTIVE":  # a deletion candidate stops acting immediately
        ctx.session.execute(
            text("UPDATE accounts SET status = 'SUSPENDED' WHERE id = :i"), {"i": row.id}
        )
    request = hd.open_request(ctx, "account", cmd.account_id, cmd.reason)
    res = _append(
        ctx,
        cmd,
        cmd.account_id,
        "ACCOUNT_DELETION_REQUESTED",
        {"account_id": cmd.account_id, "request_id": request["request_id"], "references": refs},
    )
    _audit(
        ctx,
        "account.deletion_request",
        cmd.account_id,
        request_id=request["request_id"],
        approval_id=request["approval_id"],
        references=refs,
    )
    return _result(res, cmd.account_id, **request, references=refs)


@handles(IssueCredential)
def issue_credential(cmd: IssueCredential, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:credential_rotate")
    row = _load(ctx, cmd.account_id)
    if row.status != "ACTIVE":
        raise CommandError("ACCOUNT_STATUS_INVALID", row.status, status=409)
    token, fp = issue_service_token(
        ctx.session,
        cmd.account_id,
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        clock=ctx.clock,
    )
    return CommandResult(
        cmd.account_id,
        "",
        0,
        "account",
        data={"service_token": token, "credential_fingerprint": fp},
    )


@handles(RotateCredential)
def rotate_credential(cmd: RotateCredential, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:credential_rotate")
    row = _load(ctx, cmd.account_id)
    if row.status != "ACTIVE":
        raise CommandError("ACCOUNT_STATUS_INVALID", row.status, status=409)
    owned = ctx.session.execute(
        text(
            "SELECT 1 FROM service_credentials WHERE account_id = :a AND fingerprint = :f "
            "AND status = 'active'"
        ),
        {"a": row.id, "f": cmd.old_fingerprint},
    ).first()
    if owned is None:
        raise CommandError("CREDENTIAL_NOT_FOUND", cmd.old_fingerprint, status=404)
    token, fp = rotate_service_token(
        ctx.session,
        cmd.account_id,
        cmd.old_fingerprint,
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        clock=ctx.clock,
    )
    return CommandResult(
        cmd.account_id,
        "",
        0,
        "account",
        data={"service_token": token, "credential_fingerprint": fp},
    )


@handles(RevokeCredential)
def revoke_credential(cmd: RevokeCredential, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.accounts", action="api:credential_revoke")
    row = _load(ctx, cmd.account_id)
    owned = ctx.session.execute(
        text(
            "SELECT 1 FROM service_credentials WHERE account_id = :a AND fingerprint = :f "
            "AND status = 'active'"
        ),
        {"a": row.id, "f": cmd.fingerprint},
    ).first()
    if owned is None:
        raise CommandError("CREDENTIAL_NOT_FOUND", cmd.fingerprint, status=404)
    revoke_service_token(
        ctx.session,
        cmd.fingerprint,
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        clock=ctx.clock,
    )
    return CommandResult(cmd.account_id, "", 0, "account", data={"revoked": cmd.fingerprint})


__all__ = [
    "TERMINAL_TASKS",
    "CreateAccount",
    "IssueCredential",
    "ReinstateAccount",
    "RequestAccountDeletion",
    "RevokeCredential",
    "RotateCredential",
    "SuspendAccount",
    "UpdateAccount",
    "account_view",
    "fingerprint_of",
    "list_accounts",
    "references_of",
]
