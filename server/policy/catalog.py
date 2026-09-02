"""Permission and risk catalog loader (development plan §6.9, §7E; P0-12).

Loads and schema-validates ``policy/*.yaml``, exposes the permission vocabulary, action risk
classification, approval quorum defaults, and the default Roles as ``server.policy.model.Role``
objects, and builds a vocabulary-bound ``PolicyEngine``. Stable error codes:
``POLICY_PERMISSION_UNKNOWN`` and ``POLICY_ACTION_UNCLASSIFIED``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from server.policy.engine import PolicyEngine
from server.policy.model import Constraints, Role

ROOT = Path(__file__).resolve().parents[2]
POLICY_DIR = ROOT / "policy"
SCHEMA_DIR = ROOT / "schemas" / "api" / "policy"
FILES = ("permissions", "risk-rules", "default-roles", "capabilities", "verification-rules")
RISK_ORDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


class PolicyCatalogError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RiskDecision:
    action: str
    action_class: str
    risk: str
    approval: str
    unclassified: bool


def load_yaml_validated(
    name: str, policy_dir: Path = POLICY_DIR, schema_dir: Path = SCHEMA_DIR
) -> dict[str, Any]:
    """Load ``policy/<name>.yaml`` and validate it against ``schemas/api/policy/<name>.v1``."""
    schema = json.loads((schema_dir / f"{name}.v1.schema.json").read_text(encoding="utf-8"))
    data = yaml.safe_load((policy_dir / f"{name}.yaml").read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        path = "/".join(str(p) for p in errors[0].path) or "<root>"
        raise PolicyCatalogError(
            "POLICY_SCHEMA_INVALID", f"{name}.yaml {path}: {errors[0].message}"
        )
    if not isinstance(data, dict):  # pragma: no cover - schema guarantees an object
        raise PolicyCatalogError("POLICY_SCHEMA_INVALID", f"{name}.yaml is not a mapping")
    return data


def permission_in_vocabulary(pattern: str, vocabulary: frozenset[str]) -> bool:
    """A concrete permission must be listed; ``group.*`` needs at least one listed member."""
    if pattern.endswith(".*"):
        group = pattern[:-1]
        return any(p.startswith(group) for p in vocabulary)
    return pattern in vocabulary


class PolicyCatalog:
    def __init__(self, policy_dir: Path = POLICY_DIR, schema_dir: Path = SCHEMA_DIR) -> None:
        self.permissions = load_yaml_validated("permissions", policy_dir, schema_dir)
        self.risk_rules = load_yaml_validated("risk-rules", policy_dir, schema_dir)
        self.roles_raw = load_yaml_validated("default-roles", policy_dir, schema_dir)
        self.capabilities = load_yaml_validated("capabilities", policy_dir, schema_dir)
        self.verification_rules = load_yaml_validated("verification-rules", policy_dir, schema_dir)
        self._vocabulary = frozenset(self.permissions["permissions"])
        self._validate_cross_references()

    def _validate_cross_references(self) -> None:
        classes = self.risk_rules["action_classes"]
        for action, spec in self.risk_rules["actions"].items():
            if spec["class"] not in classes:
                raise PolicyCatalogError(
                    "POLICY_ACTION_UNCLASSIFIED", f"{action}: unknown class {spec['class']}"
                )
            if spec["permission"] not in self._vocabulary:
                raise PolicyCatalogError(
                    "POLICY_PERMISSION_UNKNOWN", f"{action}: {spec['permission']}"
                )
        for cap_id, cap in self.capabilities["capabilities"].items():
            if cap["permission"] not in self._vocabulary:
                raise PolicyCatalogError(
                    "POLICY_PERMISSION_UNKNOWN", f"{cap_id}: {cap['permission']}"
                )
        for role_id, role in self.roles_raw["roles"].items():
            for group in ("permissions", "deny"):
                for pattern in role[group]:
                    if not permission_in_vocabulary(pattern, self._vocabulary):
                        raise PolicyCatalogError(
                            "POLICY_PERMISSION_UNKNOWN", f"{role_id}.{group}: {pattern}"
                        )
            for pattern in role["constraints"]["requires_human_approval"]:
                if not permission_in_vocabulary(pattern, self._vocabulary):
                    raise PolicyCatalogError(
                        "POLICY_PERMISSION_UNKNOWN", f"{role_id}.constraints: {pattern}"
                    )

    def vocabulary(self) -> frozenset[str]:
        return self._vocabulary

    def minimum_vocabulary(self) -> frozenset[str]:
        return frozenset(p for p, s in self.permissions["permissions"].items() if s["minimum"])

    def actions(self) -> dict[str, dict[str, str]]:
        return dict(self.risk_rules["actions"])

    def risk_for(self, action: str, side_effect: bool = False) -> RiskDecision:
        """Classify an action; unknown actions fall back to HIGH, flagged ``unclassified``."""
        classes = self.risk_rules["action_classes"]
        spec = self.risk_rules["actions"].get(action)
        if spec is None:
            fallback = self.risk_rules["unclassified_default"]
            return RiskDecision(action, "unclassified", fallback, "human_1", unclassified=True)
        cls = classes[spec["class"]]
        risk, approval = cls["risk"], cls["approval"]
        if side_effect and RISK_ORDER.index(risk) < RISK_ORDER.index("MEDIUM"):
            risk, approval = "MEDIUM", "channel_policy"  # side_effect conflict: higher risk wins
        return RiskDecision(action, spec["class"], risk, approval, unclassified=False)

    def quorum(self, risk: str) -> int:
        if risk not in RISK_ORDER:
            raise PolicyCatalogError("POLICY_RISK_UNKNOWN", risk)
        return int(self.risk_rules["approval_defaults"]["quorum"][risk])

    def human_only(self, risk: str) -> bool:
        threshold = self.risk_rules["approval_defaults"]["human_only_from"]
        return RISK_ORDER.index(risk) >= RISK_ORDER.index(threshold)

    def default_roles(self) -> list[Role]:
        roles: list[Role] = []
        for role_id, spec in self.roles_raw["roles"].items():
            c = spec["constraints"]
            roles.append(
                Role(
                    role_id=role_id,
                    version=1,
                    permissions=frozenset(spec["permissions"]),
                    deny=frozenset(spec["deny"]),
                    constraints=Constraints(
                        domains=frozenset(c["domains"]) if "domains" in c else None,
                        side_effects=c["side_effects"],
                        requires_human_approval=frozenset(c["requires_human_approval"]),
                        channels=frozenset(c["channels"]) if "channels" in c else None,
                        max_risk=c["max_risk"],
                    ),
                )
            )
        return roles

    def role(self, role_id: str) -> Role:
        for r in self.default_roles():
            if r.role_id == role_id:
                return r
        raise PolicyCatalogError("POLICY_ROLE_UNKNOWN", role_id)

    def engine(self) -> PolicyEngine:
        return PolicyEngine(self._vocabulary)


@lru_cache(maxsize=1)
def default_catalog() -> PolicyCatalog:
    return PolicyCatalog()
