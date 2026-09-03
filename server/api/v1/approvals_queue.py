"""Web Approvals queue (development plan §7E, §11.1; P4-14): ``/api/v1/approvals/queue``.

``GET /api/v1/approvals/queue`` lists pending requests the caller may decide with risk, quorum
required/current, decision path and escalation state. ``POST /api/v1/approvals/{id}/queue-decide``
decides with the **server-side** re-authentication proof (``require_recent_mfa``): a client can
never claim re-authentication. HIGH and above require a recent MFA proof; CRITICAL needs two
distinct Humans; Agents cannot approve; the same Human cannot approve twice.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from server.api.deps import correlation_id_of, current_principal
from server.api.dispatch import dispatch
from server.api.errors import ApiError
from server.application import approvals as a
from server.application.bus import CommandError
from server.db.engine import session_scope
from server.domain.clock import SystemClock
from server.identity.principals import SESSION_COOKIE, Principal
from server.security import mfa
from server.security import policy as secpolicy
from server.security.reauth import require_recent_mfa

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]
RISK_ORDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
CODE_MAP = {
    "APPROVER_DUPLICATE": "APPROVAL_DUPLICATE_APPROVER",
    "HUMAN_APPROVER_REQUIRED": "APPROVAL_HUMAN_ONLY",
}


class QueueDecideBody(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT)$")
    reason: str = Field(default="", max_length=2000)
    reason_code: str = Field(default="REJECTED_BY_APPROVER", pattern="^[A-Z][A-Z0-9_]{1,63}$")


def _clock(request: Request) -> Any:
    return (
        getattr(request.app.state, "clock", None)
        or request.app.state.runtime.clock
        or SystemClock()
    )


def reauth_verified(request: Request, principal: Principal) -> bool:
    """Server-side proof only: a recent MFA verification bound to this session / API client."""
    runtime = request.app.state.runtime
    now = _clock(request).now()
    with session_scope(runtime.session_factory) as session:
        session_uuid = (
            mfa.session_uuid_for(session, request.cookies.get(SESSION_COOKIE))
            if principal.credential_kind == "session"
            else None
        )
    try:
        require_recent_mfa(
            principal.account_uuid,
            now=now,
            session_id=session_uuid,
            max_age_s=secpolicy.int_value("security.reauth_max_age_s"),
            action="approval_decide",
        )
    except CommandError:
        return False
    return True


@router.get("/queue")
def queue(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    now = _clock(request).now()
    with session_scope(runtime.session_factory) as session:
        ws = uuid.UUID(str(runtime.resolve_workspace(session, principal.account_uuid)))
        rows = session.execute(
            text(
                "SELECT g.approval_id, g.subject_type, g.subject_id, g.action, g.risk, g.status, "
                "g.expires_at, g.quorum_required, g.requested_by, "
                "(SELECT count(*) FROM approval_decisions d WHERE d.approval_id = g.approval_id "
                " AND d.decision = 'APPROVE') AS approvals, "
                "(SELECT count(*) FROM approval_decisions d WHERE d.approval_id = g.approval_id "
                " AND d.decided_by = :me) AS mine, "
                "(SELECT e.payload->>'escalated_to_role' FROM events e WHERE e.aggregate_type = "
                " 'approval' AND e.aggregate_id = g.approval_id AND e.type = 'APPROVAL_ESCALATED' "
                " ORDER BY e.aggregate_seq DESC LIMIT 1) AS escalated_to "
                "FROM approval_grants g WHERE g.workspace_id = :w AND g.status = 'PENDING' "
                "AND g.expires_at > :now ORDER BY g.expires_at, g.approval_id"
            ),
            {"w": ws, "now": now, "me": uuid.UUID(principal.account_uuid)},
        ).all()
        verified = reauth_verified(request, principal)
        items = []
        for r in rows:
            risk = str(r[4])
            needs_reauth = RISK_ORDER.index(risk) >= RISK_ORDER.index("HIGH")
            items.append(
                {
                    "approval_id": r[0],
                    "subject_type": r[1],
                    "subject_id": r[2],
                    "action": r[3],
                    "risk": risk,
                    "status": r[5],
                    "expires_at": r[6].isoformat(),
                    "quorum_required": int(r[7]),
                    "quorum_current": int(r[9]),
                    "quorum_remaining": max(int(r[7]) - int(r[9]), 0),
                    "requested_by_me": str(r[8]) == principal.account_uuid,
                    "already_decided_by_me": int(r[10]) > 0,
                    "decision_path": "web_console_mfa_reauth"
                    if needs_reauth
                    else "mattermost_button",
                    "reauth_required": needs_reauth,
                    "reauth_satisfied": verified or not needs_reauth,
                    "escalated_to_role": r[11],
                    "human_only": needs_reauth,
                }
            )
    return {"items": items, "reauth_verified": verified}


@router.post("/{approval_id}/queue-decide")
def queue_decide(
    approval_id: str, body: QueueDecideBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    if principal.account_type != "human":
        raise ApiError(403, "APPROVAL_HUMAN_ONLY", "Agents and services cannot decide approvals")
    verified = reauth_verified(request, principal)
    cmd = a.DecideApproval(
        approval_id=approval_id, decision=body.decision, reason_code=body.reason_code
    )
    try:
        result = dispatch(request, principal, cmd, reauth_verified=verified)
    except ApiError as exc:
        code = CODE_MAP.get(exc.code, exc.code)
        # the queue already lists the item for this caller: a missing re-auth proof is a 403,
        # not a normalized 404 (nothing new is disclosed)
        status = 403 if code in ("REAUTH_REQUIRED", "APPROVAL_DUPLICATE_APPROVER") else exc.status
        raise ApiError(status, code, exc.detail, exc.extra) from exc
    result["correlation_id"] = correlation_id_of(request)
    return result
