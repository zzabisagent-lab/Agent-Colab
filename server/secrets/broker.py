"""Secret Broker: grants → leases → one-time handles → resolve (development plan §9.3; P4-06).

Every resolve is checked for scope (Agent, Task, action, work item, sidecar instance), expiry,
single use, revocation and LLM-exposure approval. A granted resolve appends ``SECRET_ACCESSED``
in the caller's transaction and returns the bytes exactly once; every denial appends exactly one
redacted audit entry (``secret.resolve_denied``) in an independent transaction so it survives
the caller's rollback. Nothing in this module logs, hashes or measures a secret value.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.events.store import AppendRequest, EventStore, EventStoreError
from server.observability.audit import append_audit
from server.secrets import leases as ls
from server.secrets.envelope import MasterKey
from server.secrets.local_provider import read_secret_bytes
from server.secrets.provider import Lease, LeaseScope, ResolveContext, SecretError

ANY = "*"


def iso_ms(when: dt.datetime) -> str:
    """Event-contract timestamp form: UTC, milliseconds, ``Z`` suffix."""
    return (
        when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
    )


DEFAULT_GRANT_VALIDITY = dt.timedelta(hours=24)
EXPOSURE_ACTION = "api:secret_grant_scope_expand"  # policy class secret_exposure (Human approval)


@dataclass(frozen=True)
class GrantRow:
    grant_id: str
    workspace_id: uuid.UUID
    secret_ref: str
    agent_id: str
    task_id: str | None
    action: str | None
    ttl_seconds: int
    single_use: bool
    exposure_allowed: bool
    exposure_approval_id: str | None
    expires_at: dt.datetime
    revoked_at: dt.datetime | None

    def matches(self, scope: LeaseScope) -> bool:
        if self.agent_id != scope.agent_id:
            return False
        if self.task_id is not None and self.task_id != scope.task_id:
            return False
        return not (self.action is not None and self.action != scope.action)


@dataclass(frozen=True)
class LeaseRow:
    lease_id: str
    workspace_id: uuid.UUID
    grant_id: str
    secret_ref: str
    agent_id: str
    task_id: str | None
    action: str | None
    work_item_id: str | None
    sidecar_instance_id: str | None
    single_use: bool
    issued_at: dt.datetime
    expires_at: dt.datetime
    used_at: dt.datetime | None
    use_count: int
    revoked_at: dt.datetime | None


_GRANT_COLS = (
    "grant_id, workspace_id, secret_ref, agent_id, task_id, action, ttl_seconds, single_use, "
    "exposure_allowed, exposure_approval_id, expires_at, revoked_at"
)
_LEASE_COLS = (
    "lease_id, workspace_id, grant_id, secret_ref, agent_id, task_id, action, work_item_id, "
    "sidecar_instance_id, single_use, issued_at, expires_at, used_at, use_count, revoked_at"
)


def _grant(row: Any) -> GrantRow:
    return GrantRow(
        str(row[0]),
        uuid.UUID(str(row[1])),
        str(row[2]),
        str(row[3]),
        row[4],
        row[5],
        int(row[6]),
        bool(row[7]),
        bool(row[8]),
        row[9],
        row[10],
        row[11],
    )


def _lease(row: Any) -> LeaseRow:
    return LeaseRow(
        str(row[0]),
        uuid.UUID(str(row[1])),
        str(row[2]),
        str(row[3]),
        str(row[4]),
        row[5],
        row[6],
        row[7],
        row[8],
        bool(row[9]),
        row[10],
        row[11],
        row[12],
        int(row[13]),
        row[14],
    )


def load_grant(session: Session, grant_id: str, *, lock: bool = False) -> GrantRow | None:
    row = session.execute(
        text(
            f"SELECT {_GRANT_COLS} FROM secret_grants WHERE grant_id = :g"  # noqa: S608
            + (" FOR UPDATE" if lock else "")
        ),
        {"g": grant_id},
    ).first()
    return None if row is None else _grant(row)


def load_lease(session: Session, lease_id: str, *, lock: bool = False) -> LeaseRow | None:
    row = session.execute(
        text(
            f"SELECT {_LEASE_COLS} FROM secret_leases WHERE lease_id = :l"  # noqa: S608
            + (" FOR UPDATE" if lock else "")
        ),
        {"l": lease_id},
    ).first()
    return None if row is None else _lease(row)


def _append(
    store: EventStore | None,
    *,
    workspace_id: uuid.UUID,
    aggregate_type: str,
    aggregate_id: str,
    type_: str,
    actor_uuid: str | None,
    correlation_id: str,
    scope: str,
    key: str,
    payload: dict[str, Any],
) -> str | None:
    if store is None or actor_uuid is None:
        return None
    try:
        return store.append(
            AppendRequest(
                workspace_id=str(workspace_id),
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                type=type_,
                actor_account_id=actor_uuid,
                correlation_id=correlation_id,
                idempotency_scope=scope,
                idempotency_key=key,
                payload=payload,
            )
        ).event_id
    except EventStoreError as exc:
        if exc.code != "IDEMPOTENCY_CONFLICT":
            raise
        return None


# ------------------------------------------------------------------ grants


def create_grant(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    secret_ref: str,
    agent_id: str,
    task_id: str | None,
    action: str | None,
    ttl_seconds: int,
    single_use: bool,
    valid_for: dt.timedelta | None,
    created_by: uuid.UUID | None,
    now: dt.datetime,
    store: EventStore | None,
    correlation_id: str,
    idempotency_key: str,
) -> GrantRow:
    if ttl_seconds <= 0 or dt.timedelta(seconds=ttl_seconds) > ls.MAX_TTL:
        raise SecretError("SECRET_SCOPE_DENIED", "ttl out of range")
    if not session.execute(
        text("SELECT 1 FROM secrets WHERE secret_ref = :r AND workspace_id = :w"),
        {"r": secret_ref, "w": workspace_id},
    ).first():
        raise SecretError("SECRET_NOT_FOUND", secret_ref)
    if not session.execute(
        text("SELECT 1 FROM agents WHERE agent_id = :a AND workspace_id = :w"),
        {"a": agent_id, "w": workspace_id},
    ).first():
        raise SecretError("SECRET_SCOPE_DENIED", f"agent {agent_id} unknown")
    grant_id = "grant-" + uuid.uuid4().hex[:20]
    expires_at = now + (valid_for or DEFAULT_GRANT_VALIDITY)
    session.execute(
        text(
            "INSERT INTO secret_grants (grant_id, workspace_id, secret_ref, agent_id, task_id, "
            "action, ttl_seconds, single_use, expires_at, created_by, created_at) VALUES "
            "(:g, :w, :r, :a, :t, :ac, :ttl, :su, :e, :c, :n)"
        ),
        {
            "g": grant_id,
            "w": workspace_id,
            "r": secret_ref,
            "a": agent_id,
            "t": task_id,
            "ac": action,
            "ttl": ttl_seconds,
            "su": single_use,
            "e": expires_at,
            "c": created_by,
            "n": now,
        },
    )
    _append(
        store,
        workspace_id=workspace_id,
        aggregate_type="secret_grant",
        aggregate_id=grant_id,
        type_="SECRET_GRANT_CREATED",
        actor_uuid=str(created_by) if created_by else None,
        correlation_id=correlation_id,
        scope="secret_grant:create",
        key=idempotency_key,
        payload={
            "grant_id": grant_id,
            "secret_id": secret_ref,
            "grantee_agent_id": agent_id,
            "task_id": task_id or ANY,
            "action_scope": action or ANY,
            "expires_at": iso_ms(expires_at),
            "ttl_seconds": ttl_seconds,
            "single_use": single_use,
        },
    )
    grant = load_grant(session, grant_id)
    assert grant is not None
    return grant


# ------------------------------------------------------------------ leases


def issue_lease(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    secret_ref: str,
    scope: LeaseScope,
    ttl: dt.timedelta | None,
    single_use: bool | None,
    now: dt.datetime,
    actor_label: str,
    correlation_id: str,
    grant_id: str | None = None,
) -> Lease:
    """Issue a one-time handle under a matching, live grant (§9.3 TTL default 5 minutes)."""
    rows = session.execute(
        text(
            f"SELECT {_GRANT_COLS} FROM secret_grants WHERE workspace_id = :w "  # noqa: S608
            "AND secret_ref = :r AND agent_id = :a AND revoked_at IS NULL AND expires_at > :n "
            "AND (CAST(:g AS text) IS NULL OR grant_id = :g) ORDER BY created_at DESC"
        ),
        {"w": workspace_id, "r": secret_ref, "a": scope.agent_id, "n": now, "g": grant_id},
    ).all()
    grant = next((g for g in (_grant(r) for r in rows) if g.matches(scope)), None)
    if grant is None:
        _deny_audit(
            session,
            workspace_id,
            "secret.lease_denied",
            "SECRET_SCOPE_DENIED",
            actor_label,
            correlation_id,
            secret_ref,
            {"agent_id": scope.agent_id, "task_id": scope.task_id},
            None,
        )
        raise SecretError("SECRET_SCOPE_DENIED", "no matching grant")
    ttl = ttl or dt.timedelta(seconds=grant.ttl_seconds)
    if ttl <= dt.timedelta(0) or ttl > ls.MAX_TTL:
        raise SecretError("SECRET_SCOPE_DENIED", "ttl out of range")
    handle = ls.new_handle()
    lease_id = "lease-" + uuid.uuid4().hex[:20]
    su = grant.single_use if single_use is None else single_use
    expires_at = now + ttl
    session.execute(
        text(
            "INSERT INTO secret_leases (lease_id, workspace_id, grant_id, secret_ref, handle_hash, "
            "agent_id, task_id, action, work_item_id, sidecar_instance_id, single_use, issued_at, "
            "expires_at) VALUES (:l, :w, :g, :r, :h, :a, :t, :ac, :wi, :si, :su, :i, :e)"
        ),
        {
            "l": lease_id,
            "w": workspace_id,
            "g": grant.grant_id,
            "r": secret_ref,
            "h": ls.handle_hash(handle),
            "a": scope.agent_id,
            "t": scope.task_id,
            "ac": scope.action,
            "wi": scope.work_item_id,
            "si": scope.sidecar_instance_id,
            "su": su,
            "i": now,
            "e": expires_at,
        },
    )
    ls.LIVE.add(ls.handle_hash(handle), lease_id)
    append_audit(
        session,
        action="secret.lease_issued",
        target_type="secret_lease",
        target_id=lease_id,
        result="OK",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        metadata={
            "grant_id": grant.grant_id,
            "agent_id": scope.agent_id,
            "ttl_s": int(ttl.total_seconds()),
        },
    )
    return Lease(lease_id, handle, secret_ref, scope, now, expires_at, su)


# ------------------------------------------------------------------ resolve


def _deny_audit(
    session: Session,
    workspace_id: uuid.UUID,
    action: str,
    code: str,
    actor_label: str,
    correlation_id: str,
    target_id: str,
    metadata: dict[str, Any],
    actor_uuid: str | None,
) -> None:
    """Exactly one redacted denial audit per request, in an independent transaction."""
    bind = session.get_bind()
    with Session(bind) as own, own.begin():
        append_audit(
            own,
            action=action,
            target_type="secret_lease" if target_id.startswith("lease-") else "secret",
            target_id=target_id,
            result="DENY",
            actor_label=actor_label,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            actor_account_id=uuid.UUID(actor_uuid) if actor_uuid else None,
            error_code=code,
            metadata=metadata,
        )


def _scope_mismatch(lease: LeaseRow, context: ResolveContext) -> str | None:
    if lease.agent_id != context.agent_id:
        return "agent"
    if lease.task_id is not None and lease.task_id != context.task_id:
        return "task"
    if lease.action is not None and lease.action != context.action:
        return "action"
    if lease.work_item_id is not None and lease.work_item_id != context.work_item_id:
        return "work_item"
    if lease.sidecar_instance_id is not None and (
        lease.sidecar_instance_id != context.sidecar_instance_id
    ):
        return "sidecar_instance"
    return None


def exposure_approved(session: Session, grant: GrantRow, now: dt.datetime) -> bool:
    """LLM exposure needs the grant's flag AND an APPROVED, unexpired Human approval."""
    if not grant.exposure_allowed or not grant.exposure_approval_id:
        return False
    row = session.execute(
        text("SELECT status, expires_at FROM approval_grants WHERE approval_id = :a"),
        {"a": grant.exposure_approval_id},
    ).first()
    return row is not None and str(row[0]) == "APPROVED" and row[1] > now


def resolve(
    session: Session,
    master: MasterKey,
    *,
    workspace_id: uuid.UUID,
    handle: str,
    context: ResolveContext,
    now: dt.datetime,
    actor_uuid: str | None,
    actor_label: str,
    correlation_id: str,
    store: EventStore | None,
    purpose: str = "adapter",
) -> bytes:
    """Return the secret bytes exactly once for a valid handle; deny otherwise (audited once)."""
    target = "handle"
    meta: dict[str, Any] = {"agent_id": context.agent_id, "purpose": purpose}

    def deny(code: str, extra: dict[str, Any] | None = None) -> SecretError:
        _deny_audit(
            session,
            workspace_id,
            "secret.resolve_denied",
            code,
            actor_label,
            correlation_id,
            target,
            {**meta, **(extra or {})},
            actor_uuid,
        )
        return SecretError(code, "resolve denied")

    if not ls.is_handle(handle):
        raise deny("SECRET_NOT_FOUND")
    row = session.execute(
        text(f"SELECT {_LEASE_COLS} FROM secret_leases WHERE handle_hash = :h FOR UPDATE"),  # noqa: S608
        {"h": ls.handle_hash(handle)},
    ).first()
    if row is None:
        raise deny("SECRET_NOT_FOUND")
    lease = _lease(row)
    target = lease.lease_id
    meta["lease_id"] = lease.lease_id
    if lease.workspace_id != workspace_id:
        raise deny("SECRET_NOT_FOUND")
    if lease.revoked_at is not None:
        raise deny("SECRET_HANDLE_REVOKED")
    grant = load_grant(session, lease.grant_id)
    if grant is None or grant.revoked_at is not None:
        raise deny("SECRET_HANDLE_REVOKED")
    if lease.expires_at <= now or grant.expires_at <= now:
        raise deny("SECRET_LEASE_EXPIRED")
    if lease.single_use and lease.use_count > 0:
        raise deny("SECRET_HANDLE_USED")
    mismatch = _scope_mismatch(lease, context)
    if mismatch == "sidecar_instance":
        raise deny("SECRET_HANDLE_HOST_MISMATCH", {"mismatch": mismatch})
    if mismatch is not None:
        raise deny("SECRET_SCOPE_DENIED", {"mismatch": mismatch})
    if purpose == "llm_context" and not exposure_approved(session, grant, now):
        raise deny("SECRET_EXPOSURE_APPROVAL_REQUIRED")
    if ls.LIVE.lease_id(ls.handle_hash(handle)) is None and lease.used_at is None:
        # not registered in this process (issued elsewhere or process restarted): the durable
        # checks above are authoritative; register so a later revoke can clear it
        ls.LIVE.add(ls.handle_hash(handle), lease.lease_id)
    value, version = read_secret_bytes(session, master, lease.secret_ref)
    session.execute(
        text(
            "UPDATE secret_leases SET used_at = COALESCE(used_at, :n), use_count = use_count + 1 "
            "WHERE lease_id = :l"
        ),
        {"n": now, "l": lease.lease_id},
    )
    if lease.single_use:
        ls.LIVE.drop(ls.handle_hash(handle))
    _append(
        store,
        workspace_id=workspace_id,
        aggregate_type="secret_grant",
        aggregate_id=grant.grant_id,
        type_="SECRET_ACCESSED",
        actor_uuid=actor_uuid,
        correlation_id=correlation_id,
        scope="secret_grant:access",
        key=f"{lease.lease_id}:{lease.use_count + 1}",
        payload={
            "grant_id": grant.grant_id,
            "secret_id": lease.secret_ref,
            "version": version,
            "result": "GRANTED",
            "lease_id": lease.lease_id,
            "purpose": purpose,
        },
    )
    append_audit(
        session,
        action="secret.resolved",
        target_type="secret_lease",
        target_id=lease.lease_id,
        result="OK",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        actor_account_id=uuid.UUID(actor_uuid) if actor_uuid else None,
        metadata=meta,
    )
    return value


# ------------------------------------------------------------------ revocation


def revoke(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    kind: str,
    target_id: str,
    reason: str,
    now: dt.datetime,
    actor_label: str,
    correlation_id: str,
    store: EventStore | None,
    actor_uuid: str | None,
) -> list[str]:
    """Revoke a grant, a lease, every lease of a Task/Agent, or every grant of a secret.

    New resolves are rejected immediately (durable rows); the in-process registry is cleared and
    listeners (in-memory injectors) wipe their bytes; the feed row tells sidecars.
    """
    if kind == "lease":
        where, params = "lease_id = :t", {"t": target_id}
    elif kind == "grant":
        where, params = "grant_id = :t", {"t": target_id}
    elif kind == "task":
        where, params = "task_id = :t", {"t": target_id}
    elif kind == "agent":
        where, params = "agent_id = :t", {"t": target_id}
    elif kind == "secret":
        where, params = "secret_ref = :t", {"t": target_id}
    else:
        raise SecretError("SECRET_SCOPE_DENIED", f"unknown revoke kind {kind}")
    lease_ids = [
        str(r[0])
        for r in session.execute(
            text(
                f"UPDATE secret_leases SET revoked_at = :n, revoke_reason = :r "  # noqa: S608
                f"WHERE workspace_id = :w AND revoked_at IS NULL AND {where} RETURNING lease_id"
            ),
            {**params, "n": now, "r": reason, "w": workspace_id},
        ).all()
    ]
    grant_ids: list[str] = []
    if kind in ("grant", "task", "agent", "secret"):
        gwhere = {
            "grant": "grant_id = :t",
            "task": "task_id = :t",
            "agent": "agent_id = :t",
            "secret": "secret_ref = :t",  # nosec B105 - SQL fragments
        }[kind]
        grant_ids = [
            str(r[0])
            for r in session.execute(
                text(
                    f"UPDATE secret_grants SET revoked_at = :n, revoke_reason = :r "  # noqa: S608
                    f"WHERE workspace_id = :w AND revoked_at IS NULL AND {gwhere} "
                    "RETURNING grant_id"
                ),
                {**params, "n": now, "r": reason, "w": workspace_id},
            ).all()
        ]
    if not lease_ids and not grant_ids:
        return []
    ls.record_revocation(
        session,
        workspace_id=workspace_id,
        kind=kind,
        target_id=target_id,
        lease_ids=lease_ids,
        reason=reason,
        now=now,
    )
    ls.LIVE.revoke(lease_ids, reason)
    for gid in grant_ids:
        _append(
            store,
            workspace_id=workspace_id,
            aggregate_type="secret_grant",
            aggregate_id=gid,
            type_="SECRET_GRANT_REVOKED",
            actor_uuid=actor_uuid,
            correlation_id=correlation_id,
            scope="secret_grant:revoke",
            key=f"{gid}:{reason}",
            payload={"grant_id": gid, "reason_code": reason},
        )
    append_audit(
        session,
        action="secret.revoked",
        target_type=f"secret_{kind}" if kind in ("grant", "lease", "secret") else kind,
        target_id=target_id,
        result="OK",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        actor_account_id=uuid.UUID(actor_uuid) if actor_uuid else None,
        metadata={"reason": reason, "leases": len(lease_ids), "grants": len(grant_ids)},
    )
    return lease_ids


def revoke_for_task(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    task_id: str,
    now: dt.datetime,
    actor_label: str,
    correlation_id: str,
    store: EventStore | None = None,
    actor_uuid: str | None = None,
) -> list[str]:
    """§9.3: leases and Task-scoped grants end with the Task (terminal transition hook)."""
    return revoke(
        session,
        workspace_id=workspace_id,
        kind="task",
        target_id=task_id,
        reason="TASK_ENDED",
        now=now,
        actor_label=actor_label,
        correlation_id=correlation_id,
        store=store,
        actor_uuid=actor_uuid,
    )


def ack_cleanup(session: Session, lease_id: str, now: dt.datetime) -> bool:
    res = session.execute(
        text(
            "UPDATE secret_leases SET cleanup_acked_at = COALESCE(cleanup_acked_at, :n) "
            "WHERE lease_id = :l AND revoked_at IS NOT NULL"
        ),
        {"n": now, "l": lease_id},
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]


def set_exposure_approval(
    session: Session, grant_id: str, approval_id: str, *, allowed: bool
) -> None:
    session.execute(
        text(
            "UPDATE secret_grants SET exposure_approval_id = :a, exposure_allowed = :f "
            "WHERE grant_id = :g"
        ),
        {"a": approval_id, "f": allowed, "g": grant_id},
    )


def grant_view(grant: GrantRow) -> dict[str, Any]:
    return {
        "grant_id": grant.grant_id,
        "secret_ref": grant.secret_ref,
        "agent_id": grant.agent_id,
        "task_id": grant.task_id,
        "action": grant.action,
        "ttl_seconds": grant.ttl_seconds,
        "single_use": grant.single_use,
        "exposure_allowed": grant.exposure_allowed,
        "exposure_approval_id": grant.exposure_approval_id,
        "expires_at": grant.expires_at.isoformat(),
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
    }


def lease_view(lease: LeaseRow) -> dict[str, Any]:
    return {
        "lease_id": lease.lease_id,
        "grant_id": lease.grant_id,
        "secret_ref": lease.secret_ref,
        "agent_id": lease.agent_id,
        "task_id": lease.task_id,
        "action": lease.action,
        "work_item_id": lease.work_item_id,
        "sidecar_instance_id": lease.sidecar_instance_id,
        "single_use": lease.single_use,
        "issued_at": lease.issued_at.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
        "used": lease.use_count > 0,
        "revoked_at": lease.revoked_at.isoformat() if lease.revoked_at else None,
    }
