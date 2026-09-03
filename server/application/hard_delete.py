"""Hard-delete workflow (P4-11; spec §9, development plan §9.3).

``RequestHardDelete`` opens a request and a CRITICAL approval (quorum 2, distinct Humans, MFA
re-authentication per decision); once approved the request waits ``waiting_period_hours`` before
``ExecuteHardDelete`` may run. Execution destroys the target's DEKs (crypto-shredding), records key
tombstones in the ledger, writes a display-redaction marker (``hard_delete_tombstones``) and
appends ``HARD_DELETE_EXECUTED``. Event rows are never modified: the Workspace Event chain digest
is captured before and after and must be identical.

The signed key-tombstone ledger is provided by ``server.secrets.ledger`` (P4-05/06). When that
module is absent, :class:`LocalLedger` delegates to the Phase 1 ``key_tombstones`` hash chain,
which is what backups must never be trusted for: ``reconcile_tombstones`` applies an *exported*
ledger to a restored database so that destroyed DEKs stay destroyed (V-P4-29).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
from server.approvals import service as approvals
from server.approvals.model import ApprovalError, ApprovalStatus, Subject
from server.events.chain import TOMBSTONE_CHAIN, chain_hash, hashed_row_fields, last_hash
from server.events.store import AppendRequest, AppendResult, EventStoreError
from server.observability.audit import append_audit
from server.secrets.envelope import CryptoError
from server.security.reauth import require_recent_mfa

log = logging.getLogger(__name__)

TARGET_TYPES = ("account", "conversation", "artifact", "document")
DEFAULT_WAITING_HOURS = 24
APPROVAL_ACTION = "api:hard_delete_execute"
REDACTED = "[hard-deleted]"


# ------------------------------------------------------------------ ledger seam


def _tombstone_columns(session: Session) -> set[str]:
    rows = session.execute(
        text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'key_tombstones'"
        )
    ).all()
    return {str(r[0]) for r in rows}


def _insert_tombstone(
    session: Session,
    *,
    key_ref: str,
    workspace_id: uuid.UUID,
    target_type: str,
    target_id: str,
    reason: str,
    requested_by: uuid.UUID,
    destroyed_at: dt.datetime,
    audit_event_id: str | None = None,
    signature: str | None = None,
    ledger_key_id: str | None = None,
) -> str:
    """Append one chained key-tombstone row (unsigned unless a signature is supplied)."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('key_tombstones_chain'))"))
    previous = last_hash(session, TOMBSTONE_CHAIN)
    fields = {
        "key_ref": key_ref,
        "workspace_id": workspace_id,
        "target_type": target_type,
        "target_id": target_id,
        "reason": reason,
        "requested_by": requested_by,
        "audit_event_id": audit_event_id,
        "destroyed_at": destroyed_at,
    }
    content_hash = chain_hash(hashed_row_fields(TOMBSTONE_CHAIN, fields), previous)
    cols = _tombstone_columns(session)
    extra_cols = ""
    extra_vals = ""
    params: dict[str, Any] = {**fields, "prev": previous, "hash": content_hash}
    if "signature" in cols:
        extra_cols += ", signature"
        extra_vals += ", :sig"
        params["sig"] = signature
    if "ledger_key_id" in cols:
        extra_cols += ", ledger_key_id"
        extra_vals += ", :kid"
        params["kid"] = ledger_key_id
    session.execute(
        text(
            "INSERT INTO key_tombstones (key_ref, workspace_id, target_type, target_id, reason, "  # noqa: S608
            "requested_by, audit_event_id, previous_hash, content_hash, destroyed_at"
            + extra_cols
            + ") VALUES (:key_ref, :workspace_id, :target_type, :target_id, :reason, "
            ":requested_by, :audit_event_id, :prev, :hash, :destroyed_at" + extra_vals + ")"
        ),
        params,
    )
    return content_hash


@dataclass(frozen=True)
class KeyTarget:
    key_ref: str
    workspace_id: uuid.UUID
    target_type: str
    target_id: str


def shred_key(session: Session, key_ref: str) -> KeyTarget | None:
    """Remove the wrapped DEK (crypto-shredding). Returns the key's target, or None when already
    destroyed. The tombstone entry is written separately by the ledger."""
    row = session.execute(
        text(
            "SELECT workspace_id, target_type, target_id, status FROM sensitive_keys "
            "WHERE key_ref = :k FOR UPDATE"
        ),
        {"k": key_ref},
    ).first()
    if row is None:
        raise CryptoError("KEY_UNKNOWN", key_ref)
    if str(row[3]) == "destroyed":
        return None
    session.execute(
        text(
            "UPDATE sensitive_keys SET wrapped_dek = NULL, status = 'destroyed', "
            "destroyed_at = now() WHERE key_ref = :k"
        ),
        {"k": key_ref},
    )
    return KeyTarget(key_ref, uuid.UUID(str(row[0])), str(row[1]), str(row[2]))


class Ledger(Protocol):
    def record_tombstone(
        self,
        session: Session,
        target: KeyTarget,
        reason: str,
        requested_by: uuid.UUID,
        now: dt.datetime,
    ) -> str: ...

    def is_destroyed(self, session: Session, key_ref: str) -> bool: ...

    def verify_chain(self, session: Session) -> list[str]: ...

    def reconcile_tombstones(
        self, session: Session, entries: Iterable[Mapping[str, Any]], now: dt.datetime
    ) -> dict[str, Any]: ...


@dataclass
class LocalLedger:
    """Fallback over the Phase 1 ``key_tombstones`` chain (append-only, trigger-protected) when no
    signed ledger key is configured (``AGENT_COLAB_LEDGER_KEY_B64``)."""

    def record_tombstone(
        self,
        session: Session,
        target: KeyTarget,
        reason: str,
        requested_by: uuid.UUID,
        now: dt.datetime,
    ) -> str:
        return _insert_tombstone(
            session,
            key_ref=target.key_ref,
            workspace_id=target.workspace_id,
            target_type=target.target_type,
            target_id=target.target_id,
            reason=reason,
            requested_by=requested_by,
            destroyed_at=now,
        )

    def is_destroyed(self, session: Session, key_ref: str) -> bool:
        row = session.execute(
            text("SELECT status FROM sensitive_keys WHERE key_ref = :k"), {"k": key_ref}
        ).first()
        return row is not None and str(row[0]) == "destroyed"

    def verify_chain(self, session: Session) -> list[str]:
        from server.events.chain import verify_chain

        return verify_chain(session, TOMBSTONE_CHAIN)

    def reconcile_tombstones(
        self, session: Session, entries: Iterable[Mapping[str, Any]], now: dt.datetime
    ) -> dict[str, Any]:
        return import_and_apply(session, entries, now)


def import_and_apply(
    session: Session, entries: Iterable[Mapping[str, Any]], now: dt.datetime
) -> dict[str, Any]:
    """Apply an exported ledger to this database: missing entries are re-appended to the local
    chain (their original hashes are kept in ``reason``-independent fields for audit), and every
    listed DEK ends destroyed. Idempotent."""
    imported: list[str] = []
    shredded: list[str] = []
    already: list[str] = []
    unknown: list[str] = []
    for entry in entries:
        key_ref = str(entry["key_ref"])
        present = session.execute(
            text("SELECT 1 FROM key_tombstones WHERE key_ref = :k"), {"k": key_ref}
        ).first()
        if present is None:
            _insert_tombstone(
                session,
                key_ref=key_ref,
                workspace_id=uuid.UUID(str(entry["workspace_id"])),
                target_type=str(entry["target_type"]),
                target_id=str(entry["target_id"]),
                reason=f"RECONCILED:{entry.get('reason', 'HARD_DELETE')}",
                requested_by=uuid.UUID(str(entry["requested_by"])),
                destroyed_at=dt.datetime.fromisoformat(str(entry["destroyed_at"])),
                audit_event_id=entry.get("audit_event_id"),
                signature=entry.get("signature"),
                ledger_key_id=entry.get("ledger_key_id"),
            )
            imported.append(key_ref)
        row = session.execute(
            text("SELECT status FROM sensitive_keys WHERE key_ref = :k FOR UPDATE"),
            {"k": key_ref},
        ).first()
        if row is None:
            unknown.append(key_ref)
            continue
        if str(row[0]) == "destroyed":
            already.append(key_ref)
            continue
        session.execute(
            text(
                "UPDATE sensitive_keys SET wrapped_dek = NULL, status = 'destroyed', "
                "destroyed_at = COALESCE(destroyed_at, :t) WHERE key_ref = :k"
            ),
            {"k": key_ref, "t": now},
        )
        shredded.append(key_ref)
    return {
        "imported": imported,
        "shredded": shredded,
        "already_destroyed": already,
        "unknown": unknown,
    }


@dataclass
class SignedLedger:
    """The P4-05/06 signed ledger (``server.secrets.ledger``) behind :class:`Ledger`."""

    module: Any
    key: Any

    def record_tombstone(
        self,
        session: Session,
        target: KeyTarget,
        reason: str,
        requested_by: uuid.UUID,
        now: dt.datetime,
    ) -> str:
        stone = self.module.record_tombstone(
            session,
            self.key,
            dek_id=target.key_ref,
            workspace_id=target.workspace_id,
            target_type=target.target_type,
            target_id=target.target_id,
            reason=reason,
            requested_by=requested_by,
            now=now,
        )
        return str(stone.content_hash)

    def is_destroyed(self, session: Session, key_ref: str) -> bool:
        return bool(self.module.is_destroyed(session, key_ref))

    def verify_chain(self, session: Session) -> list[str]:
        return list(self.module.verify_chain(session, self.key))

    def reconcile_tombstones(
        self, session: Session, entries: Iterable[Mapping[str, Any]], now: dt.datetime
    ) -> dict[str, Any]:
        local = import_and_apply(session, entries, now)
        report = self.module.reconcile_tombstones(session, self.key, now=now)
        return {
            **local,
            "signed_ledger": {
                "tombstones": report.tombstones,
                "sensitive_keys_destroyed": report.sensitive_keys_destroyed,
                "secret_versions_destroyed": report.secret_versions_destroyed,
                "problems": list(report.problems),
            },
        }


def ledger() -> Ledger:
    """Signed ledger when a ledger key is configured, otherwise the local chain."""
    try:
        from server.secrets import ledger as signed
    except ImportError:
        return LocalLedger()
    try:
        key = signed.LedgerKey.from_env()
    except Exception:
        return LocalLedger()
    return SignedLedger(signed, key)


def export_ledger(session: Session) -> list[dict[str, Any]]:
    """The key-tombstone ledger as portable entries (no key material), kept separately from
    database backups so that a restore can reconcile destroyed DEKs (spec §9.3)."""
    cols = _tombstone_columns(session)
    extra = ""
    if "signature" in cols:
        extra += ", signature"
    if "ledger_key_id" in cols:
        extra += ", ledger_key_id"
    rows = (
        session.execute(
            text(
                "SELECT key_ref, workspace_id, target_type, target_id, reason, requested_by, "  # noqa: S608
                "audit_event_id, previous_hash, content_hash, destroyed_at"
                + extra
                + " FROM key_tombstones ORDER BY id"
            )
        )
        .mappings()
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        entry: dict[str, Any] = {
            "key_ref": str(r["key_ref"]),
            "workspace_id": str(r["workspace_id"]),
            "target_type": str(r["target_type"]),
            "target_id": str(r["target_id"]),
            "reason": str(r["reason"]),
            "requested_by": str(r["requested_by"]),
            "audit_event_id": r["audit_event_id"],
            "previous_hash": r["previous_hash"],
            "content_hash": str(r["content_hash"]),
            "destroyed_at": r["destroyed_at"].isoformat(),
        }
        if "signature" in cols:
            entry["signature"] = r["signature"]
        if "ledger_key_id" in cols:
            entry["ledger_key_id"] = r["ledger_key_id"]
        out.append(entry)
    return out


def verify_exported_ledger(entries: list[dict[str, Any]]) -> list[str]:
    """Recompute the chain of an exported ledger; returns problems (empty when intact)."""
    problems: list[str] = []
    previous: str | None = None
    for i, e in enumerate(entries):
        fields = {
            "key_ref": e["key_ref"],
            "workspace_id": uuid.UUID(e["workspace_id"]),
            "target_type": e["target_type"],
            "target_id": e["target_id"],
            "reason": e["reason"],
            "requested_by": uuid.UUID(e["requested_by"]),
            "audit_event_id": e.get("audit_event_id"),
            "destroyed_at": dt.datetime.fromisoformat(e["destroyed_at"]),
        }
        expected = chain_hash(hashed_row_fields(TOMBSTONE_CHAIN, fields), previous)
        if expected != e["content_hash"] or e.get("previous_hash") != previous:
            problems.append(f"entry {i} ({e['key_ref']}): hash mismatch")
        previous = str(e["content_hash"])
    return problems


def reconcile_tombstones(
    session: Session, entries: Iterable[Mapping[str, Any]], now: dt.datetime | None = None
) -> dict[str, Any]:
    return ledger().reconcile_tombstones(session, entries, now or dt.datetime.now(dt.UTC))


# ------------------------------------------------------------------ commands


@dataclass(frozen=True)
class RequestHardDelete(Command):
    target_type: str
    target_id: str
    reason: str
    idempotency_scope: str = "hard_delete:request"


@dataclass(frozen=True)
class ApproveHardDelete(Command):
    request_id: str
    decision: str = "APPROVE"  # APPROVE | REJECT
    reason_code: str = "REJECTED_BY_APPROVER"
    idempotency_scope: str = "hard_delete:approve"


@dataclass(frozen=True)
class CancelHardDelete(Command):
    request_id: str
    reason_code: str = "CANCELLED_BY_ADMIN"
    idempotency_scope: str = "hard_delete:cancel"


@dataclass(frozen=True)
class ExecuteHardDelete(Command):
    request_id: str
    idempotency_scope: str = "hard_delete:execute"


def waiting_period_hours(session: Session | None = None) -> int:
    """Setting ``hard_delete.waiting_period_hours`` (P4-04 settings) else env/default 24 h."""
    if session is not None:
        try:
            from server.settings.store import SettingsStore  # P4-04

            value = SettingsStore(None).value(session, "hard_delete.waiting_period_hours")
            if value is not None:
                return int(value)
        except Exception as exc:  # unknown key / settings unavailable: use the default
            log.debug("hard_delete.waiting_period_hours setting unavailable: %s", exc)
    return int(os.environ.get("AGENT_COLAB_HARD_DELETE_WAITING_HOURS", DEFAULT_WAITING_HOURS))


def _iso_ms(when: dt.datetime) -> str:
    """Event timestamp form (schemas): UTC, milliseconds, ``Z`` suffix."""
    when = when.astimezone(dt.UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def _ws(ctx: CommandContext) -> uuid.UUID:
    return uuid.UUID(ctx.workspace_id)


def event_chain_digest(session: Session, workspace_id: uuid.UUID) -> str:
    """SHA-256 over every Event row hash of the Workspace in recorded order (immutability proof)."""
    rows = session.execute(
        text(
            "SELECT event_id, content_hash, previous_hash FROM events WHERE workspace_id = :w "
            "ORDER BY recorded_seq"
        ),
        {"w": workspace_id},
    ).all()
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r[0]}|{r[1]}|{r[2]}\n".encode())
    return h.hexdigest()


def _target_exists(
    session: Session, workspace_id: uuid.UUID, target_type: str, target_id: str
) -> bool:
    if target_type == "account":
        q = "SELECT 1 FROM accounts WHERE account_id = :t AND workspace_id = :w"
    elif target_type == "conversation":
        q = "SELECT 1 FROM conversations WHERE conversation_id = :t AND workspace_id = :w"
    elif target_type == "artifact":
        q = "SELECT 1 FROM artifacts WHERE artifact_id = :t AND workspace_id = :w"
    else:
        q = "SELECT 1 FROM documents WHERE document_id = :t AND workspace_id = :w"
    return session.execute(text(q), {"t": target_id, "w": workspace_id}).first() is not None


def load_request(session: Session, request_id: str, *, lock: bool = False) -> dict[str, Any] | None:
    row = session.execute(
        text(
            "SELECT request_id, workspace_id, target_type, target_id, reason, requested_by, "  # noqa: S608
            "approval_id, status, waiting_period_hours, approved_at, executable_at, executed_at, "
            "created_at FROM hard_delete_requests WHERE request_id = :r"
            + (" FOR UPDATE" if lock else "")
        ),
        {"r": request_id},
    ).first()
    if row is None:
        return None
    keys = (
        "request_id",
        "workspace_id",
        "target_type",
        "target_id",
        "reason",
        "requested_by",
        "approval_id",
        "status",
        "waiting_period_hours",
        "approved_at",
        "executable_at",
        "executed_at",
        "created_at",
    )
    return dict(zip(keys, row, strict=True))


def request_view(session: Session, request_id: str) -> dict[str, Any] | None:
    req = load_request(session, request_id)
    if req is None:
        return None
    approvals_recorded = 0
    quorum = 2
    approval_status = None
    if req["approval_id"]:
        try:
            grant = approvals.load_grant(session, str(req["approval_id"]))
            approval_status = grant.status.value
            quorum = int(grant.quorum_required)
        except ApprovalError:
            approval_status = None
        approvals_recorded = int(
            session.execute(
                text(
                    "SELECT count(*) FROM approval_decisions WHERE approval_id = :a "
                    "AND decision = 'APPROVE'"
                ),
                {"a": req["approval_id"]},
            ).scalar_one()
        )
    tomb = session.execute(
        text(
            "SELECT executed_at, keys_destroyed, ledger_entry_hash, event_hash_before, "
            "event_hash_after FROM hard_delete_tombstones WHERE request_id = :r"
        ),
        {"r": request_id},
    ).first()
    out: dict[str, Any] = {
        **{k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in req.items()},
        "workspace_id": str(req["workspace_id"]),
        "requested_by": str(req["requested_by"]),
        "approval_status": approval_status,
        "approvals_recorded": approvals_recorded,
        "quorum_required": quorum,
    }
    if tomb is not None:
        out["tombstone"] = {
            "executed_at": tomb[0].isoformat(),
            "keys_destroyed": tomb[1],
            "ledger_entry_hash": tomb[2],
            "event_hash_before": tomb[3],
            "event_hash_after": tomb[4],
        }
    return out


def list_requests(session: Session, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
    ids = session.execute(
        text(
            "SELECT request_id FROM hard_delete_requests WHERE "
            "workspace_id = :w ORDER BY created_at DESC"
        ),
        {"w": workspace_id},
    ).all()
    return [v for r in ids if (v := request_view(session, str(r[0]))) is not None]


def _append(
    ctx: CommandContext, cmd: Command, request_id: str, event_type: str, payload: dict[str, Any]
) -> AppendResult:
    try:
        return ctx.store.append(
            AppendRequest(
                workspace_id=ctx.workspace_id,
                aggregate_type="hard_delete",
                aggregate_id=request_id,
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


def _audit(ctx: CommandContext, action: str, request_id: str, **meta: Any) -> str:
    return append_audit(
        ctx.session,
        action=action,
        target_type="hard_delete",
        target_id=request_id,
        result="OK",
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=_ws(ctx),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata=meta,
        clock=ctx.clock,
    )


def _catalog(ctx: CommandContext) -> Any:
    from server.application.approvals import _catalog as catalog_of

    return catalog_of(ctx)


def _authorizer(ctx: CommandContext) -> Any:
    from server.application.approvals import _authorizer as authorizer_of

    return authorizer_of(ctx)


def open_request(
    ctx: CommandContext, target_type: str, target_id: str, reason: str
) -> dict[str, Any]:
    """Create the request row + CRITICAL approval (quorum 2). Shared with RequestAccountDeletion."""
    if target_type not in TARGET_TYPES:
        raise CommandError("HARD_DELETE_TARGET_TYPE_INVALID", target_type, status=400)
    if not reason.strip():
        raise CommandError("HARD_DELETE_REASON_REQUIRED", target_id, status=400)
    if not _target_exists(ctx.session, _ws(ctx), target_type, target_id):
        raise CommandError("HARD_DELETE_TARGET_NOT_FOUND", f"{target_type}:{target_id}", status=404)
    dup = ctx.session.execute(
        text(
            "SELECT request_id FROM hard_delete_requests WHERE "
            "workspace_id = :w AND target_type = :t "
            "AND target_id = :i AND status IN ('PENDING_APPROVAL','APPROVED_WAITING')"
        ),
        {"w": _ws(ctx), "t": target_type, "i": target_id},
    ).first()
    if dup is not None:
        raise CommandError("HARD_DELETE_ALREADY_PENDING", str(dup[0]), status=409)
    now = ctx.clock.now()
    request_id = (
        "hd-"
        + hashlib.sha256(
            f"{ctx.workspace_id}|{target_type}|{target_id}|{ctx.idempotency_key}".encode()
        ).hexdigest()[:20]
    )
    try:
        approval = approvals.request_approval(
            ctx.session,
            ctx.store,
            _catalog(ctx),
            ctx.clock,
            workspace_uuid=_ws(ctx),
            requested_by=uuid.UUID(ctx.principal.account_uuid),
            subject=Subject("action", f"hard-delete:{request_id}"),
            action=APPROVAL_ACTION,
            correlation_id=ctx.correlation_id,
            idempotency_key=f"{ctx.idempotency_key}:approval",
            resource_scope={"target_type": target_type, "target_id": target_id},
            risk="CRITICAL",
            valid_for=dt.timedelta(hours=24 * 7),
            max_uses=1,
            requires_human_approval=True,
        )
    except ApprovalError as exc:
        raise CommandError(exc.code, exc.detail, status=getattr(exc, "status", 409)) from exc
    hours = waiting_period_hours(ctx.session)
    ctx.session.execute(
        text(
            "INSERT INTO hard_delete_requests (request_id, workspace_id, target_type, target_id, "
            "reason, requested_by, approval_id, status, "
            "waiting_period_hours, created_at, updated_at) "
            "VALUES (:r, :w, :t, :i, :reason, :by, :a, 'PENDING_APPROVAL', :h, :now, :now)"
        ),
        {
            "r": request_id,
            "w": _ws(ctx),
            "t": target_type,
            "i": target_id,
            "reason": reason.strip(),
            "by": uuid.UUID(ctx.principal.account_uuid),
            "a": approval.approval_id,
            "h": hours,
            "now": now,
        },
    )
    return {
        "request_id": request_id,
        "approval_id": approval.approval_id,
        "quorum_required": 2,
        "waiting_period_hours": hours,
    }


@handles(RequestHardDelete)
def request_hard_delete(cmd: RequestHardDelete, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.hard_delete", action="api:hard_delete_request")
    existing = ctx.session.execute(
        text(
            "SELECT event_id, aggregate_seq, content_hash, recorded_seq, aggregate_id FROM events "
            "WHERE workspace_id = :w AND aggregate_type = 'hard_delete' AND idempotency_scope = :s "
            "AND idempotency_key = :k"
        ),
        {"w": _ws(ctx), "s": cmd.idempotency_scope, "k": ctx.idempotency_key},
    ).first()
    if existing is not None:
        res = AppendResult(
            str(existing[0]), int(existing[1]), str(existing[2]), int(existing[3]), True
        )
        view = request_view(ctx.session, str(existing[4])) or {}
        return CommandResult(
            str(existing[4]), res.event_id, res.aggregate_seq, "hard_delete", True, view
        )
    opened = open_request(ctx, cmd.target_type, cmd.target_id, cmd.reason)
    res = _append(
        ctx,
        cmd,
        opened["request_id"],
        "HARD_DELETE_REQUESTED",
        {
            "request_id": opened["request_id"],
            "target_type": cmd.target_type,
            "target_id": cmd.target_id,
            "requested_by": ctx.principal.account_id,
            "approval_id": opened["approval_id"],
        },
    )
    _audit(
        ctx,
        "hard_delete.request",
        opened["request_id"],
        approval_id=opened["approval_id"],
        waiting_period_hours=opened["waiting_period_hours"],
        target_type=cmd.target_type,
        target_id=cmd.target_id,
    )
    return CommandResult(
        opened["request_id"], res.event_id, res.aggregate_seq, "hard_delete", data=opened
    )


def _require_reauth(ctx: CommandContext, action: str) -> None:
    require_recent_mfa(
        ctx.principal.account_uuid,
        now=ctx.clock.now(),
        session_id=ctx.extras.get("session_id"),
        action=action,
    )


@handles(ApproveHardDelete)
def approve_hard_delete(cmd: ApproveHardDelete, ctx: CommandContext) -> CommandResult:
    """One approval decision by a Human administrator after MFA re-authentication."""
    require_permission(ctx, "admin.hard_delete", action="api:hard_delete_approve")
    req = load_request(ctx.session, cmd.request_id, lock=True)
    if req is None:
        raise CommandError("HARD_DELETE_NOT_FOUND", cmd.request_id, status=404)
    if req["status"] != "PENDING_APPROVAL":
        raise CommandError("HARD_DELETE_STATUS_INVALID", str(req["status"]), status=409)
    _require_reauth(ctx, "hard_delete_approve")
    try:
        decision = approvals.decide_approval(
            ctx.session,
            ctx.store,
            _authorizer(ctx),
            _catalog(ctx),
            ctx.clock,
            approval_id=str(req["approval_id"]),
            approver_account_id=ctx.principal.account_id,
            decision=cmd.decision,
            credential_fingerprint=ctx.principal.credential_fingerprint,
            reauth_verified=True,
            reason_code=cmd.reason_code,
            correlation_id=ctx.correlation_id,
            idempotency_key=f"{ctx.idempotency_key}:decide",
        )
    except ApprovalError as exc:
        raise CommandError(exc.code, exc.detail, status=getattr(exc, "status", 409)) from exc
    now = ctx.clock.now()
    data: dict[str, Any] = {
        "approvals_recorded": decision.approvals_recorded,
        "quorum_required": decision.quorum_required,
        "approval_status": decision.status.value,
    }
    event: AppendResult | None = None
    if decision.status is ApprovalStatus.REJECTED:
        ctx.session.execute(
            text(
                "UPDATE hard_delete_requests SET status = 'REJECTED', "
                "updated_at = :n WHERE request_id = :r"
            ),
            {"n": now, "r": cmd.request_id},
        )
        event = _append(
            ctx,
            cmd,
            cmd.request_id,
            "HARD_DELETE_REJECTED",
            {"request_id": cmd.request_id, "reason_code": cmd.reason_code},
        )
        _audit(ctx, "hard_delete.reject", cmd.request_id, reason_code=cmd.reason_code)
    elif decision.status is ApprovalStatus.APPROVED:
        executable = now + dt.timedelta(hours=int(req["waiting_period_hours"]))
        ctx.session.execute(
            text(
                "UPDATE hard_delete_requests SET status = 'APPROVED_WAITING', approved_at = :n, "
                "executable_at = :e, updated_at = :n WHERE request_id = :r"
            ),
            {"n": now, "e": executable, "r": cmd.request_id},
        )
        approvers = [
            str(r[0])
            for r in ctx.session.execute(
                text(
                    "SELECT a.account_id FROM approval_decisions d JOIN accounts "
                    "a ON a.id = d.decided_by "
                    "WHERE d.approval_id = :a AND d.decision = 'APPROVE' ORDER BY d.decided_at"
                ),
                {"a": req["approval_id"]},
            ).all()
        ]
        event = _append(
            ctx,
            cmd,
            cmd.request_id,
            "HARD_DELETE_APPROVED",
            {
                "request_id": cmd.request_id,
                "approved_by": approvers[-1] if approvers else ctx.principal.account_id,
                "approvers": approvers,
                "executable_after": _iso_ms(executable),
            },
        )
        data["executable_at"] = executable.isoformat()
        _audit(
            ctx,
            "hard_delete.approved",
            cmd.request_id,
            approvers=approvers,
            executable_at=executable.isoformat(),
        )
    else:
        _audit(ctx, "hard_delete.approval_recorded", cmd.request_id, **data)
    return CommandResult(
        cmd.request_id,
        event.event_id if event else "",
        event.aggregate_seq if event else 0,
        "hard_delete",
        data=data,
    )


@handles(CancelHardDelete)
def cancel_hard_delete(cmd: CancelHardDelete, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.hard_delete", action="api:hard_delete_cancel")
    req = load_request(ctx.session, cmd.request_id, lock=True)
    if req is None:
        raise CommandError("HARD_DELETE_NOT_FOUND", cmd.request_id, status=404)
    if req["status"] not in ("PENDING_APPROVAL", "APPROVED_WAITING"):
        raise CommandError("HARD_DELETE_STATUS_INVALID", str(req["status"]), status=409)
    now = ctx.clock.now()
    ctx.session.execute(
        text(
            "UPDATE hard_delete_requests SET status = 'CANCELLED', "
            "updated_at = :n WHERE request_id = :r"
        ),
        {"n": now, "r": cmd.request_id},
    )
    try:
        approvals.cancel_approval(
            ctx.session,
            ctx.store,
            ctx.clock,
            approval_id=str(req["approval_id"]),
            actor_uuid=uuid.UUID(ctx.principal.account_uuid),
            reason_code=cmd.reason_code,
            correlation_id=ctx.correlation_id,
            idempotency_key=f"{ctx.idempotency_key}:cancel",
        )
    except ApprovalError as exc:  # already terminal: the request status is the authority
        log.info("hard delete %s: approval cancel skipped (%s)", cmd.request_id, exc.code)
    event = _append(
        ctx,
        cmd,
        cmd.request_id,
        "HARD_DELETE_REJECTED",
        {"request_id": cmd.request_id, "reason_code": cmd.reason_code},
    )
    _audit(ctx, "hard_delete.cancel", cmd.request_id, reason_code=cmd.reason_code)
    return CommandResult(cmd.request_id, event.event_id, event.aggregate_seq, "hard_delete")


# ------------------------------------------------------------------ execution


def _destroy_keys(
    ctx: CommandContext, key_refs: list[str], reason: str
) -> tuple[list[str], str | None]:
    """Shred each DEK and append its ledger entry; destroyed keys are counted, not re-shredded."""
    book = ledger()
    destroyed: list[str] = []
    last: str | None = None
    for key_ref in key_refs:
        target = shred_key(ctx.session, key_ref)
        if target is None:
            destroyed.append(key_ref)
            continue
        last = book.record_tombstone(
            ctx.session, target, reason, uuid.UUID(ctx.principal.account_uuid), ctx.clock.now()
        )
        destroyed.append(key_ref)
    return destroyed, last


def _keys_for_target(
    session: Session, workspace_id: uuid.UUID, target_type: str, target_id: str
) -> list[str]:
    rows = session.execute(
        text(
            "SELECT key_ref FROM sensitive_keys WHERE workspace_id = :w AND target_type = :t "
            "AND target_id = :i ORDER BY key_ref"
        ),
        {"w": workspace_id, "t": target_type, "i": target_id},
    ).all()
    return [str(r[0]) for r in rows]


def _execute_account(ctx: CommandContext, target_id: str, request_id: str) -> list[str]:
    row = ctx.session.execute(
        text(
            "SELECT id, status FROM accounts WHERE account_id = :a AND workspace_id = :w FOR UPDATE"
        ),
        {"a": target_id, "w": _ws(ctx)},
    ).first()
    if row is None:
        raise CommandError("HARD_DELETE_TARGET_NOT_FOUND", target_id, status=404)
    keys = _keys_for_target(ctx.session, _ws(ctx), "account", target_id)
    destroyed, _ = _destroy_keys(ctx, keys, "HARD_DELETE")
    now = ctx.clock.now()
    ctx.session.execute(
        text(
            "UPDATE service_credentials SET status = 'revoked', revoked_at = :n "
            "WHERE account_id = :a AND status = 'active'"
        ),
        {"n": now, "a": row[0]},
    )
    ctx.session.execute(
        text(
            "UPDATE principal_role_assignments SET revoked_at = :n WHERE account_id = :a "
            "AND revoked_at IS NULL"
        ),
        {"a": row[0], "n": now},
    )
    ctx.session.execute(
        text(
            "UPDATE accounts SET status = 'DELETED', display_name = :d, "
            "auth_subject = NULL WHERE id = :a"
        ),
        {"d": REDACTED, "a": row[0]},
    )
    try:
        ctx.store.append(
            AppendRequest(
                workspace_id=ctx.workspace_id,
                aggregate_type="account",
                aggregate_id=target_id,
                type="ACCOUNT_HARD_DELETED",
                actor_account_id=ctx.principal.account_uuid,
                correlation_id=ctx.correlation_id,
                idempotency_scope="account:hard_delete",
                idempotency_key=f"{ctx.idempotency_key}:account",
                payload={"account_id": target_id, "request_id": request_id},
            )
        )
    except EventStoreError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc
    return destroyed


def _execute_conversation(ctx: CommandContext, target_id: str) -> list[str]:
    from server.channels.retention import _append_tombstone

    rows = ctx.session.execute(
        text(
            "SELECT message_id, channel_id, body_key_ref FROM messages WHERE conversation_id = :c "
            "AND workspace_id = :w ORDER BY received_at, message_id FOR UPDATE"
        ),
        {"c": target_id, "w": _ws(ctx)},
    ).all()
    now = ctx.clock.now()
    destroyed: list[str] = []
    for message_id, channel_id, key_ref in rows:
        if key_ref:
            got, _ = _destroy_keys(ctx, [str(key_ref)], "HARD_DELETE")
            destroyed.extend(got)
        content_hash = _append_tombstone(
            ctx.session,
            message_id=str(message_id),
            channel_id=channel_id,
            reason="HARD_DELETE",
            key_ref=str(key_ref) if key_ref else None,
            deleted_at=now,
        )
        ctx.session.execute(
            text(
                "UPDATE messages SET deleted_at = COALESCE(deleted_at, :now), body_redacted = :m, "
                "tombstone_ref = :t WHERE message_id = :id"
            ),
            {"now": now, "m": REDACTED, "t": content_hash, "id": message_id},
        )
    destroyed.extend(
        _destroy_keys(
            ctx, _keys_for_target(ctx.session, _ws(ctx), "conversation", target_id), "HARD_DELETE"
        )[0]
    )
    return destroyed


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _execute_artifact(ctx: CommandContext, target_id: str) -> list[str]:
    from server.artifacts.storage import ArtifactStorage

    row = ctx.session.execute(
        text(
            "SELECT storage_uri FROM artifacts WHERE artifact_id = :a "
            "AND workspace_id = :w FOR UPDATE"
        ),
        {"a": target_id, "w": _ws(ctx)},
    ).first()
    if row is None:
        raise CommandError("HARD_DELETE_TARGET_NOT_FOUND", target_id, status=404)
    storage = ArtifactStorage()
    try:
        _unlink(storage.path_for(str(row[0])))
    except Exception as exc:
        ctx.extras.setdefault("hard_delete_warnings", []).append(
            f"artifact blob: {type(exc).__name__}"
        )
    destroyed, _ = _destroy_keys(
        ctx, _keys_for_target(ctx.session, _ws(ctx), "artifact", target_id), "HARD_DELETE"
    )
    return destroyed


def _execute_document(ctx: CommandContext, target_id: str) -> list[str]:
    from server.documents.store import DocumentStore

    rows = ctx.session.execute(
        text("SELECT storage_uri FROM document_versions WHERE document_id = :d ORDER BY version"),
        {"d": target_id},
    ).all()
    store = DocumentStore()
    for (uri,) in rows:
        try:
            path = (
                Path(str(uri).replace("file://", ""))
                if str(uri).startswith("file://")
                else store.root / str(uri)
            )
            _unlink(path)
        except Exception as exc:
            ctx.extras.setdefault("hard_delete_warnings", []).append(
                f"document version: {type(exc).__name__}"
            )
    destroyed, _ = _destroy_keys(
        ctx, _keys_for_target(ctx.session, _ws(ctx), "document", target_id), "HARD_DELETE"
    )
    return destroyed


@handles(ExecuteHardDelete)
def execute_hard_delete(cmd: ExecuteHardDelete, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.hard_delete", action="api:hard_delete_execute")
    req = load_request(ctx.session, cmd.request_id, lock=True)
    if req is None:
        raise CommandError("HARD_DELETE_NOT_FOUND", cmd.request_id, status=404)
    if req["status"] == "EXECUTED":
        view = request_view(ctx.session, cmd.request_id) or {}
        return CommandResult(cmd.request_id, "", 0, "hard_delete", replayed=True, data=view)
    if req["status"] != "APPROVED_WAITING":
        raise CommandError(
            "HARD_DELETE_NOT_APPROVED",
            f"status {req['status']}: dual approval required before execution",
            status=409,
        )
    now = ctx.clock.now()
    executable_at = req["executable_at"]
    if executable_at is None or now < executable_at:
        raise CommandError(
            "HARD_DELETE_WAITING_PERIOD",
            f"executable from {executable_at.isoformat() if executable_at else '?'}",
            status=409,
            extra={"executable_at": executable_at.isoformat() if executable_at else None},
        )
    grant = approvals.load_grant(ctx.session, str(req["approval_id"]))
    if grant.status not in (
        ApprovalStatus.APPROVED,
        ApprovalStatus.PARTIALLY_CONSUMED,
        ApprovalStatus.CONSUMED,
    ):
        raise CommandError("HARD_DELETE_NOT_APPROVED", grant.status.value, status=409)
    _require_reauth(ctx, "hard_delete_execute")
    before = event_chain_digest(ctx.session, _ws(ctx))
    target_type, target_id = str(req["target_type"]), str(req["target_id"])
    if target_type == "account":
        destroyed = _execute_account(ctx, target_id, cmd.request_id)
    elif target_type == "conversation":
        destroyed = _execute_conversation(ctx, target_id)
    elif target_type == "artifact":
        destroyed = _execute_artifact(ctx, target_id)
    else:
        destroyed = _execute_document(ctx, target_id)
    ledger_hash = last_hash(ctx.session, TOMBSTONE_CHAIN) if destroyed else None
    approvers = [
        {"account_id": str(r[0]), "decided_at": r[1].isoformat()}
        for r in ctx.session.execute(
            text(
                "SELECT a.account_id, d.decided_at FROM approval_decisions d "
                "JOIN accounts a ON a.id = d.decided_by WHERE d.approval_id "
                "= :a AND d.decision = 'APPROVE' "
                "ORDER BY d.decided_at"
            ),
            {"a": req["approval_id"]},
        ).all()
    ]
    event = _append(
        ctx,
        cmd,
        cmd.request_id,
        "HARD_DELETE_EXECUTED",
        {
            "request_id": cmd.request_id,
            "key_ref": destroyed[0] if destroyed else "",
            "tombstone_id": ledger_hash or "",
            "keys_destroyed": len(destroyed),
            "target_type": target_type,
        },
    )
    after = event_chain_digest(ctx.session, _ws(ctx))
    # the Event chain only grew (HARD_DELETE_EXECUTED / ACCOUNT_HARD_DELETED); no row changed
    session_before = before
    after_without_new = _digest_excluding(
        ctx.session,
        _ws(ctx),
        {event.event_id, *_account_event_ids(ctx, target_type, target_id, cmd.request_id)},
    )
    if after_without_new != session_before:
        raise CommandError(
            "HARD_DELETE_EVENT_CHAIN_CHANGED", "Event rows changed during execution", status=500
        )
    ctx.session.execute(
        text(
            "INSERT INTO hard_delete_tombstones (request_id, workspace_id, target_type, target_id, "
            "executed_at, executed_by, approvals, keys_destroyed, "
            "ledger_entry_hash, event_hash_before, "
            "event_hash_after, event_id) VALUES (:r, :w, :t, :i, :n, :by, CAST(:ap AS jsonb), "
            "CAST(:keys AS jsonb), :lh, :hb, :ha, :e)"
        ),
        {
            "r": cmd.request_id,
            "w": _ws(ctx),
            "t": target_type,
            "i": target_id,
            "n": now,
            "by": uuid.UUID(ctx.principal.account_uuid),
            "ap": json.dumps(approvers),
            "keys": json.dumps(destroyed),
            "lh": ledger_hash,
            "hb": before,
            "ha": after,
            "e": event.event_id,
        },
    )
    ctx.session.execute(
        text(
            "UPDATE hard_delete_requests SET status = 'EXECUTED', "
            "executed_at = :n, updated_at = :n "
            "WHERE request_id = :r"
        ),
        {"n": now, "r": cmd.request_id},
    )
    _audit(
        ctx,
        "hard_delete.execute",
        cmd.request_id,
        target_type=target_type,
        keys_destroyed=len(destroyed),
        ledger_entry_hash=ledger_hash,
        warnings=ctx.extras.get("hard_delete_warnings", []),
    )
    return CommandResult(
        cmd.request_id,
        event.event_id,
        event.aggregate_seq,
        "hard_delete",
        data={
            "keys_destroyed": destroyed,
            "ledger_entry_hash": ledger_hash,
            "event_hash_before": before,
            "event_hash_after": after,
            "approvals": approvers,
        },
    )


def _account_event_ids(
    ctx: CommandContext, target_type: str, target_id: str, request_id: str
) -> set[str]:
    if target_type != "account":
        return set()
    rows = ctx.session.execute(
        text(
            "SELECT event_id FROM events WHERE aggregate_type = 'account' AND aggregate_id = :a "
            "AND type = 'ACCOUNT_HARD_DELETED' AND payload->>'request_id' = :r"
        ),
        {"a": target_id, "r": request_id},
    ).all()
    return {str(r[0]) for r in rows}


def _digest_excluding(session: Session, workspace_id: uuid.UUID, exclude: set[str]) -> str:
    rows = session.execute(
        text(
            "SELECT event_id, content_hash, previous_hash FROM events WHERE workspace_id = :w "
            "ORDER BY recorded_seq"
        ),
        {"w": workspace_id},
    ).all()
    h = hashlib.sha256()
    for r in rows:
        if str(r[0]) in exclude:
            continue
        h.update(f"{r[0]}|{r[1]}|{r[2]}\n".encode())
    return h.hexdigest()


__all__ = [
    "ApproveHardDelete",
    "CancelHardDelete",
    "ExecuteHardDelete",
    "KeyTarget",
    "Ledger",
    "LocalLedger",
    "RequestHardDelete",
    "SignedLedger",
    "event_chain_digest",
    "export_ledger",
    "import_and_apply",
    "ledger",
    "list_requests",
    "open_request",
    "reconcile_tombstones",
    "request_view",
    "shred_key",
    "verify_exported_ledger",
    "waiting_period_hours",
]
