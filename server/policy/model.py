"""Policy objects (spec §4.3, development plan §6.9).

A Role is a policy object: a set of permissions plus constraints. Evaluation precedence is
``explicit deny > scope restriction > allow`` and deny-by-default. The permission vocabulary is
fixed by ``policy/permissions.yaml`` (P0-12); this module only models and evaluates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class DenyReason(StrEnum):
    DEFAULT_DENY = "DEFAULT_DENY"
    EXPLICIT_DENY = "EXPLICIT_DENY"
    SCOPE_DOMAIN = "SCOPE_DOMAIN"
    SCOPE_SIDE_EFFECT = "SCOPE_SIDE_EFFECT"
    SCOPE_CHANNEL = "SCOPE_CHANNEL"
    SCOPE_RESOURCE = "SCOPE_RESOURCE"
    ROLE_INACTIVE = "ROLE_INACTIVE"
    UNKNOWN_PERMISSION = "UNKNOWN_PERMISSION"


@dataclass(frozen=True)
class Constraints:
    domains: frozenset[str] | None = None  # None = unrestricted
    side_effects: str = "allow"  # allow | deny
    requires_human_approval: frozenset[str] = frozenset()
    channels: frozenset[str] | None = None
    resources: frozenset[str] | None = None
    max_risk: str = "CRITICAL"


@dataclass(frozen=True)
class Role:
    role_id: str
    version: int
    permissions: frozenset[str]
    deny: frozenset[str] = frozenset()
    constraints: Constraints = field(default_factory=Constraints)
    status: str = "active"


@dataclass(frozen=True)
class ActionRequest:
    permission: str
    domain: str | None = None
    side_effect: bool = False
    channel_id: str | None = None
    resource: str | None = None


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str
    matched_roles: tuple[str, ...] = ()
    requires_human_approval: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


def permission_matches(pattern: str, permission: str) -> bool:
    """`task.*` matches `task.create`; `*` matches everything; otherwise exact."""
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return permission.startswith(pattern[:-1])
    return pattern == permission
