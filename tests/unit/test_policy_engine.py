"""Policy deny/allow/conflict matrix (V-P0-06)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from server.policy.engine import PolicyEngine
from server.policy.model import ActionRequest, Constraints, Role

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "policy" / "matrix.yaml"
MATRIX = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def _role(name: str, spec: dict[str, Any]) -> Role:
    c = spec.get("constraints", {})
    constraints = Constraints(
        domains=frozenset(c["domains"]) if "domains" in c else None,
        side_effects=c.get("side_effects", "allow"),
        requires_human_approval=frozenset(c.get("requires_human_approval", [])),
        channels=frozenset(c["channels"]) if "channels" in c else None,
        resources=frozenset(c["resources"]) if "resources" in c else None,
    )
    return Role(
        role_id=name,
        version=1,
        permissions=frozenset(spec.get("permissions", [])),
        deny=frozenset(spec.get("deny", [])),
        constraints=constraints,
        status=spec.get("status", "active"),
    )


@pytest.mark.parametrize("case", MATRIX["cases"], ids=[c["name"] for c in MATRIX["cases"]])
def test_policy_matrix(case: dict[str, Any]) -> None:
    roles = [_role(n, MATRIX["roles"][n]) for n in case["roles"]]
    engine = PolicyEngine(case.get("vocabulary"))
    request = ActionRequest(**case["request"])
    first = engine.evaluate(roles, request)
    second = engine.evaluate(list(reversed(roles)), request)
    assert first.decision == case["expect"]["decision"]
    assert first.reason == case["expect"]["reason"]
    assert second.decision == first.decision and second.reason == first.reason, "order-dependent"
    if "requires_human_approval" in case["expect"]:
        assert first.requires_human_approval is case["expect"]["requires_human_approval"]


def test_requires_human_approval_flag() -> None:
    role = _role(
        "r",
        {"permissions": ["task.*"], "constraints": {"requires_human_approval": ["task.complete"]}},
    )
    d = PolicyEngine().evaluate([role], ActionRequest("task.complete"))
    assert d.allowed and d.requires_human_approval is True
