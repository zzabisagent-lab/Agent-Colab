"""Authorizer unit tests over a fake repository (V-P1-07 logic, V-P3-02/09 semantics)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from server.domain.clock import FixedClock
from server.policy.authorization import (
    AuditRecord,
    AuthorizationDenied,
    AuthorizationRequest,
    Authorizer,
)
from server.policy.model import Role
from server.policy.repository import PrincipalInfo, role_from_version

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "policy" / "authorization-cases.yaml"
DATA = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
NOW = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)
WS = uuid.uuid4()
ACCT = uuid.uuid4()


@dataclass
class FakeRepository:
    """In-memory authority rows with the same validity semantics as the DB queries."""

    roles: dict[str, Role]
    assignments: list[dict[str, Any]]
    capabilities: frozenset[str]
    memberships: frozenset[str]
    principal_status: str = "ACTIVE"
    known: bool = True

    def principal(self, session: Any, account_id: str) -> PrincipalInfo | None:
        if not self.known:
            return None
        return PrincipalInfo(account_id, ACCT, WS, "agent", self.principal_status, "agent-x")

    def effective_roles(
        self, session: Any, principal: PrincipalInfo, now: dt.datetime
    ) -> list[Role]:
        out: list[Role] = []
        for a in self.assignments:
            if a.get("revoked"):
                continue
            vf = _ts(a.get("valid_from")) or NOW - dt.timedelta(days=1)
            vt = _ts(a.get("valid_to"))
            if vf > now or (vt is not None and vt <= now):
                continue
            role = self.roles[a["role"]]
            if role.status != "active":
                continue
            out.append(role)
        return sorted(out, key=lambda r: r.role_id)

    def capability_ids(self, session: Any, principal: PrincipalInfo) -> frozenset[str]:
        return self.capabilities

    def is_channel_member(self, session: Any, principal: PrincipalInfo, channel_id: str) -> bool:
        return channel_id in self.memberships


@dataclass
class RecordingSink:
    records: list[AuditRecord] = field(default_factory=list)

    def __call__(self, session: Any, record: AuditRecord) -> str | None:
        self.records.append(record)
        return f"aud-{len(self.records)}"


def _ts(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _roles() -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for rid, spec in DATA["roles"].items():
        version = 2 if rid == "old_v1_then_v2" else 1
        roles[rid] = role_from_version(
            rid,
            version,
            spec.get("permissions", []),
            spec.get("deny", []),
            spec.get("constraints", {}),
            spec.get("status", "active"),
        )
    return roles


def _build(case: dict[str, Any]) -> tuple[Authorizer, RecordingSink]:
    assignments = [a if isinstance(a, dict) else {"role": a} for a in case["roles"]]
    repo = FakeRepository(
        roles=_roles(),
        assignments=assignments,
        capabilities=frozenset(case.get("capabilities", [])),
        memberships=frozenset(case.get("memberships", ["chan-a", "chan-x", "chan-b", "chan-y"])),
        principal_status=case.get("principal_status", "ACTIVE"),
        known=case.get("principal", "acct-1") != "nobody",
    )
    sink = RecordingSink()
    return Authorizer(repo, clock=FixedClock(NOW), audit_sink=sink), sink


@pytest.mark.parametrize("case", DATA["cases"], ids=[c["name"] for c in DATA["cases"]])
def test_authorization_cases(case: dict[str, Any]) -> None:
    authorizer, sink = _build(case)
    request = AuthorizationRequest(**case["request"])
    result = authorizer.authorize(None, case.get("principal", "acct-1"), request)  # type: ignore[arg-type]
    expect = case["expect"]
    assert result.allowed is expect["allowed"], result
    assert result.code == expect["code"], result
    for key in ("risk", "approval", "approval_required", "human_only"):
        if key in expect:
            assert getattr(result, key) == expect[key], (key, result)
    if "audited" in expect:
        assert (len(sink.records) == 1) is expect["audited"]
    if not result.allowed:
        assert len(sink.records) == 1 and sink.records[0].code == result.code
        assert result.audit_id == "aud-1"
        # deterministic: same inputs, same output
        again, _ = _build(case)
        assert again.authorize(None, case.get("principal", "acct-1"), request).code == result.code  # type: ignore[arg-type]
    else:
        assert sink.records == []
        assert result.snapshot.policy_hash and result.snapshot.role_versions


def test_require_raises_with_audit() -> None:
    authorizer, sink = _build({"roles": [], "request": {}})
    with pytest.raises(AuthorizationDenied) as exc:
        authorizer.require(None, "acct-1", AuthorizationRequest("task.create", "tool:task_create"))  # type: ignore[arg-type]
    assert exc.value.code == "DEFAULT_DENY" and len(sink.records) == 1


def test_snapshot_pins_role_versions_and_capabilities() -> None:
    authorizer, _ = _build(
        {"roles": ["worker", "delegator"], "capabilities": ["cap-a"], "request": {}}
    )
    snap = authorizer.snapshot(None, "acct-1")  # type: ignore[arg-type]
    assert snap is not None
    assert snap.role_versions == (("delegator", 1), ("worker", 1))
    assert snap.capability_ids == ("cap-a",)
    assert len(snap.policy_hash) == 64
