"""Approval Core services (P1-08). Every function runs inside the caller's transaction and appends
exactly one Event per state change through the Event store; ``approval_grants`` is the command
authority, ``approval_decisions``/``approval_consumptions`` are append-only ledgers, and
``approvals_projection`` is updated read-after-write but never consulted for decisions."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.approvals.eligibility import check_eligibility
from server.approvals.model import (
    CONSUMABLE,
    ApprovalError,
    ApprovalStatus,
    Grant,
    Subject,
    default_expiry,
    next_status,
    status_after_consumption,
    validate_subject,
)
from server.domain.clock import Clock, isoformat_utc
from server.events.store import AppendRequest, AppendResult, EventStore
from server.policy.authorization import Authorizer
from server.policy.catalog import RISK_ORDER, PolicyCatalog


def _load_grant(session: Session, approval_id: str, for_update: bool = False) -> Grant:
    row = session.execute(
        text(
            "SELECT approval_id, workspace_id, subject_type, subject_id, action, risk, "
            "status, requested_by, implementing_agent_account_id, channel_id, valid_from, "
            "expires_at, max_uses, quorum_required, aggregate_seq, resource_scope "
            "FROM approval_grants WHERE approval_id = :a" + (" FOR UPDATE" if for_update else "")
        ),
        {"a": approval_id},
    ).first()
    if row is None:
        raise ApprovalError("APPROVAL_NOT_FOUND", approval_id, status=404)
    scope = dict(row[15] or {})
    return Grant(
        approval_id=str(row[0]),
        workspace_uuid=uuid.UUID(str(row[1])),
        subject=Subject(str(row[2]), str(row[3])),
        action=str(row[4]),
        risk=str(row[5]),
        status=ApprovalStatus(str(row[6])),
        requested_by=uuid.UUID(str(row[7])),
        implementing_agent_account=uuid.UUID(str(row[8])) if row[8] else None,
        channel_uuid=uuid.UUID(str(row[9])) if row[9] else None,
        valid_from=row[10],
        expires_at=row[11],
        max_uses=int(row[12]) if row[12] is not None else None,
        quorum_required=int(row[13]),
        aggregate_seq=int(row[14]),
        requires_human_approval=bool(scope.get("requires_human_approval", False)),
    )


def load_grant(session: Session, approval_id: str) -> Grant:
    return _load_grant(session, approval_id)


def _append(
    store: EventStore,
    grant: Grant,
    event_type: str,
    actor_uuid: uuid.UUID,
    correlation_id: str,
    scope: str,
    key: str,
    payload: dict[str, Any],
) -> AppendResult:
    return store.append(
        AppendRequest(
            workspace_id=str(grant.workspace_uuid),
            aggregate_type="approval",
            aggregate_id=grant.approval_id,
            type=event_type,
            actor_account_id=str(actor_uuid),
            correlation_id=correlation_id,
            idempotency_scope=f"approval:{scope}",
            idempotency_key=key,
            payload=payload,
            channel_id=str(grant.channel_uuid) if grant.channel_uuid else None,
            task_id=grant.subject.subject_id if grant.subject.subject_type == "task" else None,
            expected_seq=grant.aggregate_seq + 1,
        )
    )


def _set_status(session: Session, approval_id: str, status: ApprovalStatus, seq: int) -> None:
    session.execute(
        text(
            "UPDATE approval_grants SET status = :s, aggregate_seq = :q, updated_at = now() "
            "WHERE approval_id = :a"
        ),
        {"s": status.value, "q": seq, "a": approval_id},
    )


def _project(
    session: Session, grant: Grant, status: ApprovalStatus, event_id: str, now: dt.datetime
) -> None:
    used = session.execute(
        text("SELECT count(*) FROM approval_consumptions WHERE approval_id = :a"),
        {"a": grant.approval_id},
    ).scalar_one()
    deciders = [
        str(r[0])
        for r in session.execute(
            text("SELECT decided_by FROM approval_decisions WHERE approval_id = :a ORDER BY id"),
            {"a": grant.approval_id},
        ).all()
    ]
    session.execute(
        text(
            "INSERT INTO approvals_projection (approval_id, workspace_id, subject_type, "
            "subject_id, action, risk, status, used_count, max_uses, expires_at, requested_by, "
            "decided_by, "
            "last_event_id, updated_at) VALUES (:a, :ws, :st, :si, :ac, :r, :s, :u, :m, :e, :rb, "
            "CAST(:d AS jsonb), :ev, :now) ON CONFLICT (approval_id) DO UPDATE SET status = "
            "EXCLUDED.status, used_count = EXCLUDED.used_count, decided_by = EXCLUDED.decided_by, "
            "last_event_id = EXCLUDED.last_event_id, updated_at = EXCLUDED.updated_at"
        ),
        {
            "a": grant.approval_id,
            "ws": grant.workspace_uuid,
            "st": grant.subject.subject_type,
            "si": grant.subject.subject_id,
            "ac": grant.action,
            "r": grant.risk,
            "s": status.value,
            "u": int(used),
            "m": grant.max_uses,
            "e": grant.expires_at,
            "rb": grant.requested_by,
            "d": json.dumps(deciders),
            "ev": event_id,
            "now": now,
        },
    )


# ---------------------------------------------------------------------------- request
@dataclass(frozen=True)
class RequestResult:
    approval_id: str
    risk: str
    quorum_required: int
    expires_at: dt.datetime
    event: AppendResult


def request_approval(
    session: Session,
    store: EventStore,
    catalog: PolicyCatalog,
    clock: Clock,
    *,
    workspace_uuid: uuid.UUID,
    requested_by: uuid.UUID,
    subject: Subject,
    action: str,
    correlation_id: str,
    idempotency_key: str,
    resource_scope: dict[str, Any] | None = None,
    risk: str | None = None,
    valid_for: dt.timedelta | None = None,
    max_uses: int | None = None,
    implementing_agent_account: uuid.UUID | None = None,
    channel_uuid: uuid.UUID | None = None,
    requires_human_approval: bool = False,
) -> RequestResult:
    validate_subject(session, workspace_uuid, subject)
    catalog_risk = catalog.risk_for(action).risk
    if risk is not None and risk not in RISK_ORDER:
        raise ApprovalError("RISK_UNKNOWN", risk, status=400)
    effective_risk = (
        risk if risk and RISK_ORDER.index(risk) > RISK_ORDER.index(catalog_risk) else catalog_risk
    )
    if max_uses is not None and max_uses <= 0:
        raise ApprovalError("MAX_USES_INVALID", str(max_uses), status=400)
    now = clock.now()
    expires_at = now + valid_for if valid_for else default_expiry(now)
    if expires_at <= now:
        raise ApprovalError("VALIDITY_INVALID", "expires_at must be after valid_from", status=400)
    quorum = catalog.quorum(effective_risk)
    if requires_human_approval and quorum == 0:
        quorum = 1
    # idempotent re-request: the same key from the same requester returns the existing grant
    existing = session.execute(
        text(
            "SELECT approval_id FROM approval_grants WHERE workspace_id = :ws "
            "AND requested_by = :rb AND resource_scope->>'idempotency_key' = :k"
        ),
        {"ws": workspace_uuid, "rb": requested_by, "k": idempotency_key},
    ).first()
    if existing is not None:
        grant = _load_grant(session, str(existing[0]))
        prior = store.stream(str(workspace_uuid), "approval", grant.approval_id)[0]
        return RequestResult(
            grant.approval_id,
            grant.risk,
            grant.quorum_required,
            grant.expires_at,
            AppendResult(
                prior["event_id"], 1, prior["content_hash"], int(prior["recorded_seq"]), True
            ),
        )
    approval_id = "apr-" + uuid.uuid4().hex[:16]
    scope = {
        **(resource_scope or {}),
        "idempotency_key": idempotency_key,
        "requires_human_approval": requires_human_approval,
    }
    session.execute(
        text(
            "INSERT INTO approval_grants (id, approval_id, workspace_id, subject_type, "
            "subject_id, action, resource_scope, risk, status, requested_by, "
            "implementing_agent_account_id, channel_id, valid_from, expires_at, max_uses, "
            "quorum_required, aggregate_seq) VALUES (:id, :a, :ws, :st, :si, :ac, "
            "CAST(:scope AS jsonb), :r, 'PENDING', :rb, :ia, :ch, :vf, :ea, :mu, :q, 0)"
        ),
        {
            "id": uuid.uuid4(),
            "a": approval_id,
            "ws": workspace_uuid,
            "st": subject.subject_type,
            "si": subject.subject_id,
            "ac": action,
            "scope": json.dumps(scope),
            "r": effective_risk,
            "rb": requested_by,
            "ia": implementing_agent_account,
            "ch": channel_uuid,
            "vf": now,
            "ea": expires_at,
            "mu": max_uses,
            "q": quorum,
        },
    )
    grant = _load_grant(session, approval_id)
    result = _append(
        store,
        grant,
        "APPROVAL_REQUESTED",
        requested_by,
        correlation_id,
        "request",
        idempotency_key,
        {
            "approval_id": approval_id,
            "subject_type": subject.subject_type,
            "subject_id": subject.subject_id,
            "action": action,
            "risk": effective_risk,
            "expires_at": isoformat_utc(expires_at),
            "quorum_required": quorum,
            "max_uses": max_uses,
        },
    )
    _set_status(session, approval_id, ApprovalStatus.PENDING, result.aggregate_seq)
    _project(session, grant, ApprovalStatus.PENDING, result.event_id, now)
    return RequestResult(approval_id, effective_risk, quorum, expires_at, result)


# ---------------------------------------------------------------------------- decide
@dataclass(frozen=True)
class DecisionResult:
    approval_id: str
    status: ApprovalStatus
    approvals_recorded: int
    quorum_required: int
    event: AppendResult | None


def decide_approval(
    session: Session,
    store: EventStore,
    authorizer: Authorizer,
    catalog: PolicyCatalog,
    clock: Clock,
    *,
    approval_id: str,
    approver_account_id: str,
    decision: str,
    credential_fingerprint: str,
    reauth_verified: bool,
    correlation_id: str,
    idempotency_key: str,
    reason_code: str = "REJECTED_BY_APPROVER",
) -> DecisionResult:
    if decision not in ("APPROVE", "REJECT"):
        raise ApprovalError("DECISION_INVALID", decision, status=400)
    now = clock.now()
    grant = _load_grant(session, approval_id, for_update=True)
    if grant.status is not ApprovalStatus.PENDING:
        raise ApprovalError("APPROVAL_NOT_PENDING", grant.status.value)
    if now >= grant.expires_at:
        raise ApprovalError("APPROVAL_NOT_USABLE", "expired")
    prior = frozenset(
        uuid.UUID(str(r[0]))
        for r in session.execute(
            text("SELECT decided_by FROM approval_decisions WHERE approval_id = :a"),
            {"a": approval_id},
        ).all()
    )
    elig = check_eligibility(
        session,
        authorizer,
        catalog,
        approver_account_id,
        grant,
        prior,
        reauth_verified,
        now,
        correlation_id,
    )
    if not elig.eligible or elig.approver is None:
        raise ApprovalError(
            elig.code, f"{approver_account_id} cannot decide {approval_id}", status=403
        )
    approver = elig.approver
    if decision == "REJECT":
        result = _append(
            store,
            grant,
            "APPROVAL_REJECTED",
            approver.account_uuid,
            correlation_id,
            "decide",
            idempotency_key,
            {
                "approval_id": approval_id,
                "decided_by": approver.account_id,
                "reason_code": reason_code,
            },
        )
        _record_decision(
            session,
            approval_id,
            approver.account_uuid,
            "REJECT",
            credential_fingerprint,
            reauth_verified,
            result.event_id,
        )
        status = next_status(grant.status, "APPROVAL_REJECTED")
        _set_status(session, approval_id, status, result.aggregate_seq)
        _project(session, grant, status, result.event_id, now)
        return DecisionResult(approval_id, status, len(prior), grant.quorum_required, result)
    approvals = len(prior) + 1
    if approvals >= grant.quorum_required:
        result = _append(
            store,
            grant,
            "APPROVAL_GRANTED",
            approver.account_uuid,
            correlation_id,
            "decide",
            idempotency_key,
            {
                "approval_id": approval_id,
                "decided_by": approver.account_id,
                "quorum_count": approvals,
                "deciders": sorted(str(p) for p in prior | {approver.account_uuid}),
            },
        )
        _record_decision(
            session,
            approval_id,
            approver.account_uuid,
            "APPROVE",
            credential_fingerprint,
            reauth_verified,
            result.event_id,
        )
        status = next_status(grant.status, "APPROVAL_GRANTED")
        _set_status(session, approval_id, status, result.aggregate_seq)
        _project(session, grant, status, result.event_id, now)
        return DecisionResult(approval_id, status, approvals, grant.quorum_required, result)
    # quorum not yet met: the decision is recorded in the append-only ledger; status stays PENDING
    last = store.stream(str(grant.workspace_uuid), "approval", approval_id)[-1]
    _record_decision(
        session,
        approval_id,
        approver.account_uuid,
        "APPROVE",
        credential_fingerprint,
        reauth_verified,
        last["event_id"],
    )
    _project(session, grant, ApprovalStatus.PENDING, last["event_id"], now)
    return DecisionResult(
        approval_id, ApprovalStatus.PENDING, approvals, grant.quorum_required, None
    )


def _record_decision(
    session: Session,
    approval_id: str,
    decided_by: uuid.UUID,
    decision: str,
    fingerprint: str,
    reauth: bool,
    event_id: str,
) -> None:
    session.execute(
        text(
            "INSERT INTO approval_decisions (approval_id, decided_by, decision, "
            "credential_fingerprint, reauth_verified, event_id) "
            "VALUES (:a, :d, :dec, :fp, :re, :e)"
        ),
        {
            "a": approval_id,
            "d": decided_by,
            "dec": decision,
            "fp": fingerprint,
            "re": reauth,
            "e": event_id,
        },
    )


# ------------------------------------------------------------- cancel / revoke / expire
def _terminate(
    session: Session,
    store: EventStore,
    clock: Clock,
    approval_id: str,
    event_type: str,
    actor_uuid: uuid.UUID,
    correlation_id: str,
    idempotency_key: str,
    reason_code: str,
    scope: str,
) -> AppendResult:
    grant = _load_grant(session, approval_id, for_update=True)
    status = next_status(grant.status, event_type)
    payload: dict[str, Any] = {"approval_id": approval_id}
    if event_type != "APPROVAL_EXPIRED":
        payload["reason_code"] = reason_code
    result = _append(
        store, grant, event_type, actor_uuid, correlation_id, scope, idempotency_key, payload
    )
    _set_status(session, approval_id, status, result.aggregate_seq)
    _project(session, grant, status, result.event_id, clock.now())
    return result


def cancel_approval(
    session: Session,
    store: EventStore,
    clock: Clock,
    *,
    approval_id: str,
    actor_uuid: uuid.UUID,
    correlation_id: str,
    idempotency_key: str,
    reason_code: str = "CANCELLED_BY_REQUESTER",
) -> AppendResult:
    grant = _load_grant(session, approval_id)
    if grant.requested_by != actor_uuid and not _is_admin_actor(session, actor_uuid):
        raise ApprovalError(
            "APPROVAL_CANCEL_FORBIDDEN", "only the requester or an administrator", status=403
        )
    return _terminate(
        session,
        store,
        clock,
        approval_id,
        "APPROVAL_CANCELLED",
        actor_uuid,
        correlation_id,
        idempotency_key,
        reason_code,
        "cancel",
    )


def revoke_approval(
    session: Session,
    store: EventStore,
    clock: Clock,
    *,
    approval_id: str,
    actor_uuid: uuid.UUID,
    correlation_id: str,
    idempotency_key: str,
    reason_code: str = "REVOKED",
) -> AppendResult:
    return _terminate(
        session,
        store,
        clock,
        approval_id,
        "APPROVAL_REVOKED",
        actor_uuid,
        correlation_id,
        idempotency_key,
        reason_code,
        "revoke",
    )


def _is_admin_actor(session: Session, actor_uuid: uuid.UUID) -> bool:
    row = session.execute(
        text(
            "SELECT 1 FROM principal_role_assignments pra JOIN roles r ON r.role_id = pra.role_id "
            "JOIN role_versions rv ON rv.role_id = r.role_id AND rv.version = r.current_version "
            "WHERE pra.account_id = :a AND pra.revoked_at IS NULL AND r.status = 'active' "
            "AND (rv.permissions ? 'admin.accounts' OR rv.permissions ? 'admin.*' "
            "OR rv.permissions ? '*') LIMIT 1"
        ),
        {"a": actor_uuid},
    ).first()
    return row is not None


@dataclass(frozen=True)
class ExpiryResult:
    expired: list[str]
    escalated_to: str


def expire_approvals(
    session: Session,
    store: EventStore,
    catalog: PolicyCatalog,
    clock: Clock,
    *,
    actor_uuid: uuid.UUID,
    correlation_id: str,
    workspace_uuid: uuid.UUID | None = None,
) -> ExpiryResult:
    """Expire every non-terminal grant whose validity ended; each expiry is escalated (§7E)."""
    now = clock.now()
    rows = session.execute(
        text(
            "SELECT approval_id FROM approval_grants WHERE status IN ('PENDING','APPROVED',"
            "'PARTIALLY_CONSUMED') AND expires_at <= :now "
            "AND (CAST(:ws AS uuid) IS NULL OR workspace_id = CAST(:ws AS uuid)) "
            "ORDER BY approval_id FOR UPDATE"
        ),
        {"now": now, "ws": workspace_uuid},
    ).all()
    escalated_to = str(
        catalog.risk_rules["approval_defaults"].get("escalation_role", "role-administrator")
    )
    expired: list[str] = []
    for (approval_id,) in rows:
        aid = str(approval_id)
        _terminate(
            session,
            store,
            clock,
            aid,
            "APPROVAL_EXPIRED",
            actor_uuid,
            correlation_id,
            f"expire:{aid}",
            "EXPIRED",
            "expire",
        )
        grant = _load_grant(session, aid)
        result = _append(
            store,
            grant,
            "APPROVAL_ESCALATED",
            actor_uuid,
            correlation_id,
            "escalate",
            f"escalate:{aid}",
            {"approval_id": aid, "escalated_to_role": escalated_to},
        )
        _set_status(session, aid, ApprovalStatus.EXPIRED, result.aggregate_seq)
        _project(session, grant, ApprovalStatus.EXPIRED, result.event_id, now)
        expired.append(aid)
    return ExpiryResult(expired, escalated_to)


# ------------------------------------------------------------- consume (bounded, atomic)
@dataclass(frozen=True)
class ConsumeResult:
    approval_id: str
    consumption_key: str
    used_count: int
    status: ApprovalStatus
    event_id: str
    replayed: bool = False


def consume_approval(
    session: Session,
    store: EventStore,
    clock: Clock,
    *,
    approval_id: str,
    consumption_key: str,
    consumed_by: uuid.UUID,
    consumed_for: Subject,
    correlation_id: str,
) -> ConsumeResult:
    """Bounded atomic consume (development plan §6.7): lock the grant row, check validity/expiry
    with the injected Clock, count the ledger, insert the consumption, append APPROVAL_CONSUMED and
    advance the status — all in the caller's transaction. Never reads the projection."""
    now = clock.now()
    grant = _load_grant(session, approval_id, for_update=True)
    existing = session.execute(
        text(
            "SELECT event_id FROM approval_consumptions "
            "WHERE approval_id = :a AND consumption_key = :k"
        ),
        {"a": approval_id, "k": consumption_key},
    ).first()
    if existing is not None:  # idempotent retry of the same consumption
        used = int(
            session.execute(
                text("SELECT count(*) FROM approval_consumptions WHERE approval_id = :a"),
                {"a": approval_id},
            ).scalar_one()
        )
        return ConsumeResult(
            approval_id, consumption_key, used, grant.status, str(existing[0]), True
        )
    if grant.status is ApprovalStatus.CONSUMED:
        raise ApprovalError("APPROVAL_EXHAUSTED", f"{grant.max_uses}/{grant.max_uses} uses")
    if grant.status not in CONSUMABLE:
        raise ApprovalError("APPROVAL_NOT_USABLE", grant.status.value)
    if not (grant.valid_from <= now < grant.expires_at):
        raise ApprovalError("APPROVAL_NOT_USABLE", "outside validity window")
    if consumed_for != grant.subject:
        raise ApprovalError(
            "APPROVAL_SCOPE_MISMATCH",
            f"approval is for {grant.subject.subject_type}:{grant.subject.subject_id}",
        )
    used = int(
        session.execute(
            text("SELECT count(*) FROM approval_consumptions WHERE approval_id = :a"),
            {"a": approval_id},
        ).scalar_one()
    )
    if grant.max_uses is not None and used >= grant.max_uses:
        raise ApprovalError("APPROVAL_EXHAUSTED", f"{used}/{grant.max_uses} uses")
    result = _append(
        store,
        grant,
        "APPROVAL_CONSUMED",
        consumed_by,
        correlation_id,
        "consume",
        f"{approval_id}:{consumption_key}",
        {
            "approval_id": approval_id,
            "consumption_key": consumption_key,
            "used_count": used + 1,
            "consumed_for_type": consumed_for.subject_type,
            "consumed_for_id": consumed_for.subject_id,
        },
    )
    session.execute(
        text(
            "INSERT INTO approval_consumptions (approval_id, consumption_key, consumed_by, "
            "consumed_for_type, "
            "consumed_for_id, event_id) VALUES (:a, :k, :b, :t, :i, :e)"
        ),
        {
            "a": approval_id,
            "k": consumption_key,
            "b": consumed_by,
            "t": consumed_for.subject_type,
            "i": consumed_for.subject_id,
            "e": result.event_id,
        },
    )
    status = status_after_consumption(used + 1, grant.max_uses)
    _set_status(session, approval_id, status, result.aggregate_seq)
    _project(session, grant, status, result.event_id, now)
    return ConsumeResult(approval_id, consumption_key, used + 1, status, result.event_id)
