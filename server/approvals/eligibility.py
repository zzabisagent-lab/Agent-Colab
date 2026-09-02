"""Approver eligibility and quorum (development plan §7E, spec §8.4).

eligible = ``approval.decide`` permission ∧ membership in the target channel ∧ Role
``max_risk ≥ action risk`` ∧ not the requester, the implementing Agent, or their aliases.
Human-only for risk HIGH and above or ``requires_human_approval``; HIGH and above need a fresh
re-authentication; CRITICAL needs two different Humans. Every rejection is audited (redacted).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.approvals.model import Grant
from server.observability.audit import append_audit
from server.policy.authorization import (
    AuditRecord,
    AuthorizationRequest,
    Authorizer,
    _db_audit_sink,
)
from server.policy.catalog import RISK_ORDER, PolicyCatalog
from server.policy.repository import PrincipalInfo
from server.verification.independence import effective_principal

DECIDE_ACTION = "command:approve.grant"


def audit_independently(session: Session, **kwargs: Any) -> str | None:
    """Append an audit row in its own transaction so that a rejected command (whose transaction
    rolls back) still leaves the redacted denial record (spec §15.17, V-P1-32)."""
    bind = session.get_bind()
    with Session(bind) as own, own.begin():
        return append_audit(own, **kwargs)


def independent_audit_sink(session: Session, record: AuditRecord) -> str | None:
    """Audit sink for the Authorizer used on decision paths (same content as the default sink)."""
    bind = session.get_bind()
    with Session(bind) as own, own.begin():
        return _db_audit_sink(own, record)


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    code: str
    approver: PrincipalInfo | None = None


def alias_graph(session: Session, workspace_uuid: uuid.UUID) -> dict[str, str]:
    rows = session.execute(
        text(
            "SELECT a.id, b.id FROM account_aliases al "
            "JOIN accounts a ON a.id = al.account_id "
            "JOIN accounts b ON b.id = al.alias_of_account_id WHERE a.workspace_id = :ws"
        ),
        {"ws": workspace_uuid},
    ).all()
    return {str(r[0]): str(r[1]) for r in rows}


def _same_principal(a: uuid.UUID | None, b: uuid.UUID | None, graph: dict[str, str]) -> bool:
    if a is None or b is None:
        return False
    return effective_principal(str(a), graph) == effective_principal(str(b), graph)


def quorum_for(catalog: PolicyCatalog, risk: str) -> int:
    return catalog.quorum(risk)


def check_eligibility(
    session: Session,
    authorizer: Authorizer,
    catalog: PolicyCatalog,
    approver_account_id: str,
    grant: Grant,
    prior_deciders: frozenset[uuid.UUID],
    reauth_verified: bool,
    now: dt.datetime,
    correlation_id: str = "-",
) -> Eligibility:
    approver = authorizer.repository.principal(session, approver_account_id)
    if approver is None or approver.status != "ACTIVE":
        return _audit(
            session, None, approver_account_id, grant, "APPROVER_NOT_ELIGIBLE", correlation_id
        )
    graph = alias_graph(session, grant.workspace_uuid)
    if _same_principal(approver.account_uuid, grant.requested_by, graph) or _same_principal(
        approver.account_uuid, grant.implementing_agent_account, graph
    ):
        return _audit(
            session, approver, approver_account_id, grant, "SELF_APPROVAL_FORBIDDEN", correlation_id
        )
    if approver.account_uuid in prior_deciders:
        return _audit(
            session, approver, approver_account_id, grant, "APPROVER_DUPLICATE", correlation_id
        )
    human_only = catalog.human_only(grant.risk) or grant.requires_human_approval
    if human_only and approver.account_type != "human":
        return _audit(
            session, approver, approver_account_id, grant, "HUMAN_APPROVER_REQUIRED", correlation_id
        )
    channel_ref = str(grant.channel_uuid) if grant.channel_uuid else None
    authorization = authorizer.authorize(
        session,
        approver_account_id,
        AuthorizationRequest(
            "approval.decide",
            DECIDE_ACTION,
            channel_id=channel_ref,
            correlation_id=correlation_id,
            target_type="approval",
            target_id=grant.approval_id,
        ),
    )
    if not authorization.allowed:
        mapped = {
            "CHANNEL_NOT_MEMBER": "APPROVER_NOT_CHANNEL_MEMBER",
            "ROLE_MAX_RISK_EXCEEDED": "APPROVER_ROLE_RISK_TOO_LOW",
        }.get(authorization.code, "APPROVER_NOT_ELIGIBLE")
        # the authorizer already appended the policy.deny audit row
        return Eligibility(False, mapped, approver)
    roles = authorizer.repository.effective_roles(session, approver, now)
    idx = RISK_ORDER.index(grant.risk)
    covering = [
        r
        for r in roles
        if r.role_id in authorization.matched_roles
        and RISK_ORDER.index(r.constraints.max_risk) >= idx
    ]
    if not covering:
        return _audit(
            session,
            approver,
            approver_account_id,
            grant,
            "APPROVER_ROLE_RISK_TOO_LOW",
            correlation_id,
        )
    if RISK_ORDER.index(grant.risk) >= RISK_ORDER.index("HIGH") and not reauth_verified:
        return _audit(
            session, approver, approver_account_id, grant, "REAUTH_REQUIRED", correlation_id
        )
    return Eligibility(True, "ELIGIBLE", approver)


def _audit(
    session: Session,
    approver: PrincipalInfo | None,
    approver_account_id: str,
    grant: Grant,
    code: str,
    correlation_id: str,
) -> Eligibility:
    audit_independently(
        session,
        action="approval.deny",
        target_type="approval",
        target_id=grant.approval_id,
        result="DENY",
        actor_label=approver_account_id,
        correlation_id=correlation_id,
        workspace_id=grant.workspace_uuid,
        actor_account_id=approver.account_uuid if approver else None,
        error_code=code,
        metadata={"risk": grant.risk, "subject_type": grant.subject.subject_type, "reason": code},
    )
    return Eligibility(False, code, approver)
