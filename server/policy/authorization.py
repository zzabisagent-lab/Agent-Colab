"""Server-side authorization for command handlers (P1-03; V-P1-07).

Order: principal lookup → committed roles → ``PolicyEngine`` (explicit deny > scope > allow,
deny by default, vocabulary-bound) → capability → channel membership → risk classification and
role ``max_risk`` → approval requirement. Every DENY appends a redacted AuditEvent inside the
caller's transaction. Decisions depend only on committed authority rows and the policy catalog.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock
from server.observability.audit import append_audit
from server.policy.catalog import RISK_ORDER, PolicyCatalog, default_catalog
from server.policy.engine import PolicyEngine
from server.policy.model import ActionRequest, Decision, Role
from server.policy.repository import (
    PolicyRepository,
    PolicySnapshot,
    PostgresPolicyRepository,
    PrincipalInfo,
    snapshot_of,
)

AuditSink = Callable[[Session, "AuditRecord"], str | None]


@dataclass(frozen=True)
class AuthorizationRequest:
    permission: str
    action: str | None = None  # catalog action name, e.g. ``tool:task_delegate``
    domain: str | None = None
    channel_id: str | None = None
    resource: str | None = None
    side_effect: bool = False
    required_capability: str | None = None
    correlation_id: str = "-"
    target_type: str = "action"
    target_id: str = "-"


@dataclass(frozen=True)
class Authorization:
    allowed: bool
    code: str
    risk: str
    action_class: str
    approval: str  # none | channel_policy | human_1 | human_2
    approval_required: bool
    human_only: bool
    matched_roles: tuple[str, ...]
    snapshot: PolicySnapshot
    audit_id: str | None = None


@dataclass(frozen=True)
class AuditRecord:
    account_id: str
    account_uuid: uuid.UUID | None
    workspace_uuid: uuid.UUID | None
    code: str
    request: AuthorizationRequest
    roles: tuple[str, ...]


class AuthorizationDenied(PermissionError):  # noqa: N818 - name fixed by the package contract
    def __init__(self, authorization: Authorization) -> None:
        super().__init__(authorization.code)
        self.code = authorization.code
        self.authorization = authorization


def _db_audit_sink(session: Session, record: AuditRecord) -> str | None:
    return append_audit(
        session,
        action="policy.deny",
        target_type=record.request.target_type,
        target_id=record.request.target_id,
        result="DENY",
        actor_label=record.account_id,
        correlation_id=record.request.correlation_id,
        workspace_id=record.workspace_uuid,
        actor_account_id=record.account_uuid,
        error_code=record.code,
        metadata={
            "permission": record.request.permission,
            "action": record.request.action,
            "reason": record.code,
            "roles": list(record.roles),
            "channel_id": record.request.channel_id,
            "domain": record.request.domain,
            "required_capability": record.request.required_capability,
        },
    )


class Authorizer:
    def __init__(
        self,
        repository: PolicyRepository | None = None,
        catalog: PolicyCatalog | None = None,
        clock: Clock | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.repository: PolicyRepository = repository or PostgresPolicyRepository()
        self.catalog = catalog or default_catalog()
        self.engine: PolicyEngine = self.catalog.engine()
        self.clock = clock or SystemClock()
        self.audit_sink = audit_sink or _db_audit_sink

    # ------------------------------------------------------------------ helpers
    def _classify(self, request: AuthorizationRequest) -> tuple[str, str, str]:
        """Risk, action class, and approval for the request (catalog authority)."""
        action = request.action
        if action is None:
            actions = self.catalog.actions()
            candidates = sorted(
                a for a, s in actions.items() if s["permission"] == request.permission
            )
            action = candidates[0] if candidates else f"permission:{request.permission}"
        decision = self.catalog.risk_for(action, side_effect=request.side_effect)
        return decision.risk, decision.action_class, decision.approval

    @staticmethod
    def _roles_cover_risk(roles: list[Role], matched: tuple[str, ...], risk: str) -> bool:
        idx = RISK_ORDER.index(risk)
        for role in roles:
            if role.role_id in matched and RISK_ORDER.index(role.constraints.max_risk) >= idx:
                return True
        return False

    def _deny(
        self,
        session: Session,
        principal: PrincipalInfo | None,
        account_id: str,
        request: AuthorizationRequest,
        code: str,
        roles: list[Role],
        matched: tuple[str, ...],
        risk: str,
        action_class: str,
        approval: str,
        now: dt.datetime,
    ) -> Authorization:
        snapshot = snapshot_of(account_id, roles, frozenset(), now)
        record = AuditRecord(
            account_id=account_id,
            account_uuid=principal.account_uuid if principal else None,
            workspace_uuid=principal.workspace_uuid if principal else None,
            code=code,
            request=request,
            roles=tuple(r.role_id for r in roles),
        )
        audit_id = self.audit_sink(session, record)
        return Authorization(
            allowed=False,
            code=code,
            risk=risk,
            action_class=action_class,
            approval=approval,
            approval_required=False,
            human_only=self.catalog.human_only(risk),
            matched_roles=matched,
            snapshot=snapshot,
            audit_id=audit_id,
        )

    # ------------------------------------------------------------------ API
    def authorize(
        self, session: Session, principal_account_id: str, request: AuthorizationRequest
    ) -> Authorization:
        now = self.clock.now()
        risk, action_class, approval = self._classify(request)
        principal = self.repository.principal(session, principal_account_id)
        if principal is None or principal.status != "ACTIVE":
            code = "PRINCIPAL_UNKNOWN" if principal is None else "PRINCIPAL_INACTIVE"
            return self._deny(
                session,
                principal,
                principal_account_id,
                request,
                code,
                [],
                (),
                risk,
                action_class,
                approval,
                now,
            )
        roles = self.repository.effective_roles(session, principal, now)
        decision = self.engine.evaluate(
            roles,
            ActionRequest(
                permission=request.permission,
                domain=request.domain,
                side_effect=request.side_effect,
                channel_id=request.channel_id,
                resource=request.resource,
            ),
        )
        if decision.decision is Decision.DENY:
            return self._deny(
                session,
                principal,
                principal_account_id,
                request,
                str(decision.reason),
                roles,
                decision.matched_roles,
                risk,
                action_class,
                approval,
                now,
            )
        capabilities = self.repository.capability_ids(session, principal)
        if request.required_capability and request.required_capability not in capabilities:
            return self._deny(
                session,
                principal,
                principal_account_id,
                request,
                "CAPABILITY_MISSING",
                roles,
                decision.matched_roles,
                risk,
                action_class,
                approval,
                now,
            )
        if request.channel_id and not self.repository.is_channel_member(
            session, principal, request.channel_id
        ):
            return self._deny(
                session,
                principal,
                principal_account_id,
                request,
                "CHANNEL_NOT_MEMBER",
                roles,
                decision.matched_roles,
                risk,
                action_class,
                approval,
                now,
            )
        if not self._roles_cover_risk(roles, decision.matched_roles, risk):
            return self._deny(
                session,
                principal,
                principal_account_id,
                request,
                "ROLE_MAX_RISK_EXCEEDED",
                roles,
                decision.matched_roles,
                risk,
                action_class,
                approval,
                now,
            )
        human_only = self.catalog.human_only(risk)
        approval_required = decision.requires_human_approval or approval in ("human_1", "human_2")
        return Authorization(
            allowed=True,
            code="ALLOW",
            risk=risk,
            action_class=action_class,
            approval="human_1"
            if decision.requires_human_approval and approval == "none"
            else approval,
            approval_required=approval_required,
            human_only=human_only or decision.requires_human_approval,
            matched_roles=decision.matched_roles,
            snapshot=snapshot_of(principal_account_id, roles, capabilities, now),
        )

    def require(
        self, session: Session, principal_account_id: str, request: AuthorizationRequest
    ) -> Authorization:
        """Authorize or raise ``AuthorizationDenied`` (the audit row is already appended)."""
        authorization = self.authorize(session, principal_account_id, request)
        if not authorization.allowed:
            raise AuthorizationDenied(authorization)
        return authorization

    def snapshot(self, session: Session, principal_account_id: str) -> PolicySnapshot | None:
        principal = self.repository.principal(session, principal_account_id)
        if principal is None:
            return None
        now = self.clock.now()
        roles = self.repository.effective_roles(session, principal, now)
        return snapshot_of(
            principal_account_id, roles, self.repository.capability_ids(session, principal), now
        )


def record_to_metadata(record: AuditRecord) -> dict[str, Any]:  # pragma: no cover - helper
    return {"permission": record.request.permission, "reason": record.code}
