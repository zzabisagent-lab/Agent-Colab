"""P3-02 Role/Capability administration.

V-P3-02 the first authorization after a RoleVersion commit follows the latest version (zero
stale allows); V-P3-09 explicit deny beats allow across Roles; V-P3-16 Roles bind to Human/Agent/
service Accounts alike and alias/shared-credential verifiers stay excluded.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.api.errors import ApiError
from server.application import roles as rl
from server.application.authz import BusAuthorizer
from server.config import Settings
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import Principal, token_hash
from server.main import create_app
from server.policy.authorization import AuthorizationRequest, Authorizer
from server.policy.repository import PostgresPolicyRepository
from server.verification.independence import (
    Identity,
    VerificationIndependenceError,
    check_independence,
)

pytestmark = pytest.mark.db
WS = uuid.uuid4()
ADMIN, HUMAN, AGENT, SERVICE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
TOK_ADMIN, TOK_HUMAN = "svc-roles-admin", "svc-roles-human"
T0 = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC)
ADMIN_P = Principal("acct-roles-admin", str(ADMIN), "human", "sha256:acct-roles-admin")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-roles', 'r')"),
            {"i": WS},
        )
        for acc, name, typ, tok in (
            (ADMIN, "acct-roles-admin", "human", TOK_ADMIN),
            (HUMAN, "acct-roles-human", "human", TOK_HUMAN),
            (AGENT, "acct-roles-agent", "agent", None),
            (SERVICE, "acct-roles-service", "service", None),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
            if tok:
                s.execute(
                    text(
                        "INSERT INTO service_credentials (id, account_id, fingerprint, "
                        "token_hash) VALUES (:i, :a, :f, :h)"
                    ),
                    {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{name}", "h": token_hash(tok)},
                )
        s.execute(
            text(
                "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, "
                "status, display_name) VALUES (:i, 'agent-roles-1', :w, :a, 'mcp', 'active', 'A')"
            ),
            {"i": uuid.uuid4(), "w": WS, "a": AGENT},
        )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "role-roles-admin", "roles admin")
        repo.commit_role_version(
            s, "role-roles-admin", ["admin.accounts", "agent.manage"], [], {}, ADMIN
        )
        repo.assign_role(s, ADMIN, "role-roles-admin", ADMIN, T0)
    yield eng
    eng.dispose()


def _rt(engine: Engine, clock: FixedClock) -> Runtime:
    return Runtime(make_session_factory(engine), BusAuthorizer(), None, clock, str(WS))


def _run(rt: Runtime, cmd: Any, key: str) -> Any:
    return execute_command(rt, ADMIN_P, cmd, idempotency_key=key, correlation_id="corr-roles")


def _authorize(
    engine: Engine, clock: FixedClock, account: str, permission: str, **scope: Any
) -> Any:
    with Session(engine) as s, s.begin():
        return Authorizer(clock=clock).authorize(
            s,
            account,
            AuthorizationRequest(permission=permission, correlation_id="corr-roles", **scope),
        )


def test_role_create_commit_and_first_authorization_follows_latest_version(engine: Engine) -> None:
    """V-P3-02."""
    clock = FixedClock(T0)
    rt = _rt(engine, clock)
    created = _run(
        rt,
        rl.CreateRole(
            "role-roles-reviewer",
            "Reviewer",
            ("task.read", "verification.submit"),
            (),
            {"domains": ["research"]},
        ),
        "role-create-1",
    )
    assert created.data["role_version"] == 1 and created.event_id
    assert _run(
        rt, rl.CreateRole("role-roles-reviewer", "Reviewer", ("task.read",)), "role-create-1"
    ).replayed
    with pytest.raises(ApiError) as exc:
        _run(rt, rl.CreateRole("role-roles-bad", "x", ("task.fly",)), "role-create-bad")
    assert exc.value.code == "PERMISSION_UNKNOWN"
    _run(rt, rl.AssignRole("acct-roles-human", "role-roles-reviewer"), "assign-1")
    clock.advance(dt.timedelta(seconds=1))
    allowed = _authorize(
        engine, clock, "acct-roles-human", "verification.submit", domain="research"
    )
    assert allowed.allowed and allowed.snapshot.role_versions == (("role-roles-reviewer", 1),)
    out_of_scope = _authorize(
        engine, clock, "acct-roles-human", "verification.submit", domain="ops"
    )
    assert not out_of_scope.allowed and out_of_scope.code == "SCOPE_DOMAIN"
    # commit v2 without verification.submit: the very next authorization must deny
    v2 = _run(
        rt,
        rl.CommitRoleVersion("role-roles-reviewer", ("task.read",), (), {"domains": ["research"]}),
        "commit-2",
    )
    assert v2.data["role_version"] == 2
    denied = _authorize(engine, clock, "acct-roles-human", "verification.submit", domain="research")
    assert not denied.allowed and denied.code == "DEFAULT_DENY"
    still = _authorize(engine, clock, "acct-roles-human", "task.read", domain="research")
    assert still.allowed and still.snapshot.role_versions == (("role-roles-reviewer", 2),)
    with Session(engine) as s:
        view = rl.role_view(s, WS, "role-roles-reviewer")
        assert view is not None and [v["version"] for v in view["versions"]] == [1, 2]
        assert all(v["event_id"] for v in view["versions"])
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM events WHERE aggregate_type = 'role' "
                    "AND aggregate_id = 'role-roles-reviewer'"
                )
            ).scalar_one()
            == 2
        )


def test_explicit_deny_wins_and_preview_explains(engine: Engine) -> None:
    """V-P3-09 + effective preview."""
    clock = FixedClock(T0 + dt.timedelta(minutes=5))
    rt = _rt(engine, clock)
    _run(rt, rl.CreateRole("role-roles-allow", "allow", ("task.*",)), "role-allow")
    _run(rt, rl.CreateRole("role-roles-deny", "deny", (), ("task.complete",)), "role-deny")
    _run(rt, rl.AssignRole("acct-roles-agent", "role-roles-allow"), "assign-allow")
    clock.advance(dt.timedelta(seconds=1))
    assert _authorize(engine, clock, "acct-roles-agent", "task.complete").allowed
    _run(rt, rl.AssignRole("acct-roles-agent", "role-roles-deny"), "assign-deny")
    clock.advance(dt.timedelta(seconds=1))
    decision = _authorize(engine, clock, "acct-roles-agent", "task.complete")
    assert not decision.allowed and decision.code == "EXPLICIT_DENY"
    assert _authorize(engine, clock, "acct-roles-agent", "task.read").allowed
    with Session(engine) as s:
        preview = rl.effective_preview(
            s, WS, "acct-roles-agent", clock.now(), permission="task.complete"
        )
    assert preview["decision"] == {
        "permission": "task.complete",
        "allowed": False,
        "reason": "EXPLICIT_DENY",
        "matched_roles": ["role-roles-deny"],
        "requires_human_approval": False,
        "precedence": "explicit deny > scope restriction > allow",
    }
    assert [r["role_id"] for r in preview["roles"]] == ["role-roles-allow", "role-roles-deny"]
    assert preview["effective_deny"] == ["task.complete"] and preview["account_type"] == "agent"
    # revoke the deny Role: allow again, with history in Events
    _run(rt, rl.RevokeRole("acct-roles-agent", "role-roles-deny", "REVIEW_DONE"), "revoke-deny")
    clock.advance(dt.timedelta(seconds=1))
    assert _authorize(engine, clock, "acct-roles-agent", "task.complete").allowed
    with pytest.raises(ApiError) as exc:
        _run(rt, rl.RevokeRole("acct-roles-agent", "role-roles-deny"), "revoke-again")
    assert exc.value.code == "ROLE_NOT_ASSIGNED"
    with Session(engine) as s:
        types = [
            str(r[0])
            for r in s.execute(
                text(
                    "SELECT type FROM events WHERE aggregate_type = 'account' "
                    "AND aggregate_id = 'acct-roles-agent' ORDER BY aggregate_seq"
                )
            ).all()
        ]
    assert types == ["PRINCIPAL_ROLE_ASSIGNED", "PRINCIPAL_ROLE_ASSIGNED", "PRINCIPAL_ROLE_REVOKED"]


def test_roles_apply_to_every_principal_type_and_aliases_stay_excluded(engine: Engine) -> None:
    """V-P3-16."""
    clock = FixedClock(T0 + dt.timedelta(minutes=10))
    rt = _rt(engine, clock)
    _run(
        rt,
        rl.CreateRole("role-roles-verifier", "verifier", ("verification.submit", "task.read")),
        "role-verifier",
    )
    for acct, key in (
        ("acct-roles-human", "v-h"),
        ("acct-roles-agent", "v-a"),
        ("acct-roles-service", "v-s"),
    ):
        _run(rt, rl.AssignRole(acct, "role-roles-verifier"), key)
    clock.advance(dt.timedelta(seconds=1))
    decisions = {
        acct: _authorize(engine, clock, acct, "verification.submit").allowed
        for acct in ("acct-roles-human", "acct-roles-agent", "acct-roles-service")
    }
    assert decisions == {
        "acct-roles-human": True,
        "acct-roles-agent": True,
        "acct-roles-service": True,
    }
    with Session(engine) as s:
        previews = {
            acct: rl.effective_preview(s, WS, acct, clock.now(), permission="verification.submit")[
                "decision"
            ]["allowed"]
            for acct in decisions
        }
    assert previews == decisions
    # the same Account-based policy: an alias credential of the implementer is never a verifier
    implementer = Identity("acct-roles-human", "sha256:acct-roles-human", None)
    alias = Identity("acct-roles-service", "sha256:acct-roles-service", None)
    graph = {"acct-roles-service": "acct-roles-human"}  # service credential aliases the human
    with pytest.raises(VerificationIndependenceError) as exc:
        check_independence(implementer, alias, graph, frozenset({"verification.submit"}))
    assert exc.value.code == "VERIFIER_ALIAS_OF_IMPLEMENTER"
    shared = Identity("acct-roles-agent", "sha256:acct-roles-human", "agent-roles-1")
    with pytest.raises(VerificationIndependenceError) as exc2:
        check_independence(implementer, shared, {}, frozenset({"verification.submit"}))
    assert exc2.value.code == "VERIFIER_SAME_CREDENTIAL"
    independent = Identity("acct-roles-agent", "sha256:acct-roles-agent", "agent-roles-1")
    check_independence(implementer, independent, graph, frozenset({"verification.submit"}))


def test_roles_rest_surface(database_url: str, engine: Engine) -> None:
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    app = create_app(Settings(database_url=database_url, base_url="http://test"))
    admin = {"Authorization": f"Bearer {TOK_ADMIN}", "Idempotency-Key": "rest-role-1"}
    human = {"Authorization": f"Bearer {TOK_HUMAN}", "Idempotency-Key": "rest-role-h"}
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/roles",
            json={
                "role_id": "role-roles-rest",
                "display_name": "REST",
                "permissions": ["task.read"],
                "deny": ["task.complete"],
            },
            headers=admin,
        )
        assert r.status_code == 201 and r.json()["role_version"] == 1, r.text
        assert (
            client.post(
                "/api/v1/roles",
                json={
                    "role_id": "role-roles-rest2",
                    "display_name": "x",
                    "permissions": ["task.read"],
                },
                headers=human,
            ).status_code
            == 404
        )
        assert client.get("/api/v1/roles", headers=human).status_code == 404  # normalized denial
        assert (
            client.post(
                "/api/v1/roles/role-roles-rest/assign",
                json={"account_id": "acct-roles-service"},
                headers={**admin, "Idempotency-Key": "rest-assign-1"},
            ).status_code
            == 200
        )
        eff = client.get(
            "/api/v1/roles/effective",
            params={"account_id": "acct-roles-service", "permission": "task.complete"},
            headers=admin,
        )
        assert eff.status_code == 200 and eff.json()["decision"]["reason"] == "EXPLICIT_DENY"
        assert "role-roles-rest" in [
            x["role_id"] for x in client.get("/api/v1/roles", headers=admin).json()["items"]
        ]
        v2 = client.post(
            "/api/v1/roles/role-roles-rest/versions",
            json={"permissions": ["task.read", "task.complete"]},
            headers={**admin, "Idempotency-Key": "rest-v2"},
        )
        assert v2.status_code == 201 and v2.json()["role_version"] == 2
        eff2 = client.get(
            "/api/v1/roles/effective",
            params={"account_id": "acct-roles-service", "permission": "task.complete"},
            headers=admin,
        ).json()
        assert eff2["decision"]["allowed"] is True
        assert (
            client.get(
                "/api/v1/roles/effective", params={"account_id": "acct-nobody"}, headers=admin
            ).status_code
            == 404
        )
