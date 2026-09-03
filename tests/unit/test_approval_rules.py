"""Approval state table, subject registry, eligibility matrix, and quorum (P1-08, unit)."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy.orm import Session

from server.approvals import eligibility as elig
from server.approvals.model import (
    CONSUMABLE,
    TERMINAL,
    TRANSITIONS,
    ApprovalError,
    ApprovalStatus,
    Grant,
    Subject,
    default_expiry,
    next_status,
    reminder_at,
    status_after_consumption,
)
from server.policy.authorization import Authorization, Authorizer
from server.policy.catalog import default_catalog
from server.policy.model import Constraints, Role
from server.policy.repository import PolicySnapshot, PrincipalInfo

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "approvals" / "eligibility-cases.yaml"
CASES = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
NOW = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
WS = uuid.uuid4()


# ------------------------------------------------------------------ states
def test_transition_table_matches_spec_8_4() -> None:
    assert next_status(ApprovalStatus.PENDING, "APPROVAL_GRANTED") is ApprovalStatus.APPROVED
    assert (
        next_status(ApprovalStatus.APPROVED, "APPROVAL_CONSUMED")
        is ApprovalStatus.PARTIALLY_CONSUMED
    )
    for terminal in TERMINAL:
        with pytest.raises(ApprovalError) as exc:
            next_status(terminal, "APPROVAL_GRANTED")
        assert exc.value.code == "APPROVAL_TERMINAL"
    with pytest.raises(ApprovalError) as exc2:
        next_status(ApprovalStatus.PENDING, "APPROVAL_CONSUMED")
    assert exc2.value.code == "APPROVAL_TRANSITION_INVALID"
    sources = {s for s, _ in TRANSITIONS}
    assert sources == {
        ApprovalStatus.PENDING,
        ApprovalStatus.APPROVED,
        ApprovalStatus.PARTIALLY_CONSUMED,
    }
    assert CONSUMABLE == {ApprovalStatus.APPROVED, ApprovalStatus.PARTIALLY_CONSUMED}
    assert status_after_consumption(1, 1) is ApprovalStatus.CONSUMED
    assert status_after_consumption(1, 3) is ApprovalStatus.PARTIALLY_CONSUMED
    assert status_after_consumption(5, None) is ApprovalStatus.PARTIALLY_CONSUMED


def test_expiry_and_reminder_defaults() -> None:
    assert default_expiry(NOW) == NOW + dt.timedelta(hours=24)
    assert reminder_at(NOW, NOW + dt.timedelta(hours=24)) == NOW + dt.timedelta(hours=12)


# ------------------------------------------------------------------ subjects
class _NoTask:
    def execute(self, *_: Any, **__: Any) -> Any:
        class _R:
            def first(self) -> None:
                return None

        return _R()


def test_subject_registry_phase_activation() -> None:
    from server.approvals.model import validate_subject

    session: Session = _NoTask()  # type: ignore[assignment]
    for st in ("schedule", "run"):  # active from Phase 5: the id must exist in this Workspace
        with pytest.raises(ApprovalError) as exc:
            validate_subject(session, WS, Subject(st, "x"))
        assert exc.value.code == "SUBJECT_NOT_FOUND"
    with pytest.raises(ApprovalError) as exc2:
        validate_subject(session, WS, Subject("bogus", "x"))
    assert exc2.value.code == "SUBJECT_TYPE_UNKNOWN"
    with pytest.raises(ApprovalError) as exc3:
        validate_subject(session, WS, Subject("task", ""))
    assert exc3.value.code == "SUBJECT_ID_REQUIRED"
    with pytest.raises(ApprovalError) as exc4:
        validate_subject(session, WS, Subject("task", "task-missing"))
    assert exc4.value.code == "SUBJECT_NOT_FOUND"
    validate_subject(session, WS, Subject("action", "external_send"))  # no lookup needed


# ------------------------------------------------------------------ eligibility (fake repo/authorizer)  # noqa: E501
class _Repo:
    def __init__(
        self, accounts: dict[str, PrincipalInfo], roles: dict[str, list[Role]], members: set[str]
    ):
        self.accounts, self.roles, self.members = accounts, roles, members

    def principal(self, session: Session, account_id: str) -> PrincipalInfo | None:
        return self.accounts.get(account_id)

    def effective_roles(
        self, session: Session, principal: PrincipalInfo, now: dt.datetime
    ) -> list[Role]:
        return self.roles.get(principal.account_id, [])

    def capability_ids(self, session: Session, principal: PrincipalInfo) -> frozenset[str]:
        return frozenset()

    def is_channel_member(
        self, session: Session, principal: PrincipalInfo, channel_id: str
    ) -> bool:
        return principal.account_id in self.members


class _Session:
    """Fake session: alias graph rows and a recording audit sink."""

    def __init__(self, aliases: dict[str, str], ids: dict[str, uuid.UUID]) -> None:
        self.rows = [(ids[a], ids[b]) for a, b in aliases.items()]

    def execute(self, *_: Any, **__: Any) -> Any:
        rows = self.rows

        class _R:
            def all(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
                return rows

        return _R()


def _grant(case: dict[str, Any], ids: dict[str, uuid.UUID]) -> Grant:
    g = case["grant"]
    return Grant(
        approval_id="apr-unit",
        workspace_uuid=WS,
        subject=Subject("task", "task-1"),
        action="external_send",
        risk=g["risk"],
        status=ApprovalStatus.PENDING,
        requested_by=ids[g["requester"]],
        implementing_agent_account=ids[g["implementing_agent"]]
        if g.get("implementing_agent")
        else None,
        channel_uuid=uuid.uuid4(),
        valid_from=NOW,
        expires_at=NOW + dt.timedelta(hours=24),
        max_uses=None,
        quorum_required=1,
        aggregate_seq=1,
        requires_human_approval=bool(g.get("requires_human_approval")),
    )


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_eligibility_matrix(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    a = case["approver"]
    names = {
        a["account"],
        case["grant"]["requester"],
        case["grant"].get("implementing_agent"),
        *case.get("prior", []),
        *case.get("aliases", {}).keys(),
        *case.get("aliases", {}).values(),
    }
    ids = {n: uuid.uuid4() for n in names if n}
    accounts = {
        n: PrincipalInfo(
            n,
            ids[n],
            WS,
            "agent" if n == a["account"] and a["type"] == "agent" else "human",
            a.get("status", "ACTIVE") if n == a["account"] else "ACTIVE",
            None,
        )
        for n in ids
    }
    role = Role(
        "role-x",
        1,
        frozenset({"approval.decide"}) if a["has_permission"] else frozenset(),
        constraints=Constraints(max_risk=a["role_max_risk"]),
    )
    repo = _Repo(accounts, {a["account"]: [role]}, {a["account"]} if a["member"] else set())
    recorded: list[str] = []

    def _sink(_s: Any, r: Any) -> str:
        recorded.append(r.code)
        return "aud"

    def _audit(*_a: Any, **kw: Any) -> str:
        recorded.append(str(kw["error_code"]))
        return "aud"

    authorizer = Authorizer(repo, default_catalog(), audit_sink=_sink)
    monkeypatch.setattr(elig, "audit_independently", _audit)
    session: Session = _Session(case.get("aliases", {}), ids)  # type: ignore[assignment]
    result = elig.check_eligibility(
        session,
        authorizer,
        default_catalog(),
        a["account"],
        _grant(case, ids),
        frozenset(ids[p] for p in case.get("prior", [])),
        bool(case.get("reauth", False)),
        NOW,
    )
    assert result.code == case["expect"], case["name"]
    assert result.eligible is (case["expect"] == "ELIGIBLE")
    if not result.eligible:
        assert recorded, "every rejection is audited"


def test_quorum_defaults_match_7e() -> None:
    catalog = default_catalog()
    for risk, q in CASES["quorum"].items():
        assert catalog.quorum(risk) == q
    assert (
        catalog.human_only("HIGH")
        and catalog.human_only("CRITICAL")
        and not catalog.human_only("MEDIUM")
    )


def test_authorization_type_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    # sanity: Authorization/PolicySnapshot importable for typed fakes
    snap = PolicySnapshot("a", (), (), "0" * 64, "t")
    assert Authorization(True, "ALLOW", "LOW", "read_query", "none", False, False, (), snap).allowed
