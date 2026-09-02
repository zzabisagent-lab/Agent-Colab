"""Deterministic policy evaluation (V-P0-06, V-P1-07).

Order: (1) inactive roles are ignored, (2) any explicit deny wins, (3) scope restrictions of the
roles that would allow must be satisfied, (4) allow only if at least one active role grants the
permission; otherwise deny by default. The result depends only on the inputs (no clock, no I/O).
"""

from __future__ import annotations

from collections.abc import Iterable

from server.policy.model import (
    ActionRequest,
    Decision,
    DenyReason,
    PolicyDecision,
    Role,
    permission_matches,
)


class PolicyEngine:
    def __init__(self, vocabulary: Iterable[str] | None = None) -> None:
        self._vocabulary = frozenset(vocabulary) if vocabulary is not None else None

    def evaluate(self, roles: Iterable[Role], request: ActionRequest) -> PolicyDecision:
        if self._vocabulary is not None and request.permission not in self._vocabulary:
            return PolicyDecision(Decision.DENY, DenyReason.UNKNOWN_PERMISSION)
        active = [r for r in roles if r.status == "active"]
        for role in sorted(active, key=lambda r: r.role_id):
            if any(permission_matches(p, request.permission) for p in role.deny):
                return PolicyDecision(Decision.DENY, DenyReason.EXPLICIT_DENY, (role.role_id,))
        granting = [
            r
            for r in sorted(active, key=lambda r: r.role_id)
            if any(permission_matches(p, request.permission) for p in r.permissions)
        ]
        if not granting:
            return PolicyDecision(Decision.DENY, DenyReason.DEFAULT_DENY)
        in_scope: list[Role] = []
        first_scope_reason: str | None = None
        for role in granting:
            reason = self._scope_violation(role, request)
            if reason is None:
                in_scope.append(role)
            elif first_scope_reason is None:
                first_scope_reason = reason
        if not in_scope:
            return PolicyDecision(
                Decision.DENY,
                first_scope_reason or DenyReason.SCOPE_RESOURCE,
                tuple(r.role_id for r in granting),
            )
        needs_human = any(
            request.permission in r.constraints.requires_human_approval
            or any(
                permission_matches(p, request.permission)
                for p in r.constraints.requires_human_approval
            )
            for r in in_scope
        )
        return PolicyDecision(
            Decision.ALLOW,
            "ALLOW",
            tuple(r.role_id for r in in_scope),
            requires_human_approval=needs_human,
        )

    @staticmethod
    def _scope_violation(role: Role, request: ActionRequest) -> str | None:
        c = role.constraints
        if c.domains is not None and (request.domain is None or request.domain not in c.domains):
            return DenyReason.SCOPE_DOMAIN
        if c.side_effects == "deny" and request.side_effect:
            return DenyReason.SCOPE_SIDE_EFFECT
        if c.channels is not None and (
            request.channel_id is None or request.channel_id not in c.channels
        ):
            return DenyReason.SCOPE_CHANNEL
        if c.resources is not None and (
            request.resource is None or request.resource not in c.resources
        ):
            return DenyReason.SCOPE_RESOURCE
        return None
