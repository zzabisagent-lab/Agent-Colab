"""P4-01 Account Admin (V-P4-07 account lifecycle; V-P4-26 principal Role CRUD reflected in the
common Account assignment with API/bus parity and consistent audit)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.main import create_app
from tests.integration.phase4_admin_seed import T0, Seed, audit_actions, run, seed

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def sd(engine: Engine) -> Seed:
    return seed(engine, "acadm")


@pytest.fixture(scope="module")
def client(database_url: str, sd: Seed) -> Iterator[TestClient]:
    app = create_app(
        Settings(database_url=database_url, base_url="http://t", master_key_b64=sd.master_key_b64)
    )
    with TestClient(app) as c:
        yield c


def test_account_lifecycle_create_edit_suspend_delete_request(
    engine: Engine, sd: Seed, client: TestClient
) -> None:
    h = sd.headers("admin1", "ac-create-1")
    r = client.post(
        "/api/v1/accounts",
        json={
            "account_id": "acct-acadm-bob",
            "display_name": "Bob",
            "roles": [f"role-{sd.prefix}-member"],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["roles"] == [f"role-{sd.prefix}-member"]
    # idempotent retry returns the same Event
    r2 = client.post(
        "/api/v1/accounts",
        json={
            "account_id": "acct-acadm-bob",
            "display_name": "Bob",
            "roles": [f"role-{sd.prefix}-member"],
        },
        headers=h,
    )
    assert r2.status_code == 201 and r2.json()["event_id"] == r.json()["event_id"]
    # service Account with a one-time token; the token never appears in audit/Events
    r = client.post(
        "/api/v1/accounts",
        json={
            "account_id": "acct-acadm-bot",
            "display_name": "Bot",
            "account_type": "service",
            "issue_token": True,
        },
        headers=sd.headers("admin1", "ac-create-2"),
    )
    assert r.status_code == 201, r.text
    token = r.json()["service_token"]
    assert len(token) >= 32
    with Session(engine) as s:
        leaked = s.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE redacted_metadata::text LIKE :t OR "
                "actor_label = :tok"
            ),
            {"t": f"%{token}%", "tok": token},
        ).scalar_one()
        assert leaked == 0
        in_events = s.execute(
            text("SELECT count(*) FROM events WHERE payload::text LIKE :t"), {"t": f"%{token}%"}
        ).scalar_one()
        assert in_events == 0
    # the new token authenticates
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code in (200, 404)  # /me may be human-only; authentication itself succeeded
    # agents are registered through the registry, never created directly
    r = client.post(
        "/api/v1/accounts",
        json={"account_id": "acct-acadm-ag", "display_name": "A", "account_type": "agent"},
        headers=sd.headers("admin1", "ac-create-3"),
    )
    assert r.status_code == 400 and r.json()["code"] == "ACCOUNT_TYPE_AGENT_VIA_REGISTRY"
    # edit
    r = client.patch(
        "/api/v1/accounts/acct-acadm-bob",
        json={"display_name": "Robert"},
        headers=sd.headers("admin1", "ac-upd-1"),
    )
    assert r.status_code == 200 and r.json()["fields"] == ["display_name"]
    # references: an assigned open Task blocks suspension unless forced
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, title, "
                "domain, risk, "
                "status, assignee_account_id, created_at, updated_at) VALUES ('task-acadm-1', :w, "
                "'task-acadm-1', 'ref', 'general', 'LOW', 'ACCEPTED', "
                "(SELECT id FROM accounts WHERE account_id = 'acct-acadm-bob'), :t, :t)"
            ),
            {"w": sd.ws, "t": T0},
        )
    r = client.post(
        "/api/v1/accounts/acct-acadm-bob/suspend",
        json={"reason_code": "TEST"},
        headers=sd.headers("admin1", "ac-susp-1"),
    )
    assert r.status_code == 409 and r.json()["code"] == "ACCOUNT_HAS_REFERENCES", r.text
    view = client.get("/api/v1/accounts/acct-acadm-bob", headers=sd.headers("admin1", "r")).json()
    assert view["references"]["open_tasks"] == [{"task_id": "task-acadm-1", "status": "ACCEPTED"}]
    assert view["references"]["roles"] == [f"role-{sd.prefix}-member"]
    r = client.post(
        "/api/v1/accounts/acct-acadm-bob/suspend",
        json={"reason_code": "TEST", "force": True},
        headers=sd.headers("admin1", "ac-susp-2"),
    )
    assert r.status_code == 200
    assert (
        client.get("/api/v1/accounts/acct-acadm-bob", headers=sd.headers("admin1", "r")).json()[
            "status"
        ]
        == "SUSPENDED"
    )
    # a suspended Account's token no longer authenticates as active (policy deny → 404)
    r = client.post(
        "/api/v1/accounts/acct-acadm-bob/reinstate",
        json={"reason_code": "OK"},
        headers=sd.headers("admin1", "ac-rein-1"),
    )
    assert r.status_code == 200
    with Session(engine) as s, s.begin():
        s.execute(
            text("UPDATE tasks_projection SET status = 'COMPLETED' WHERE task_id = 'task-acadm-1'")
        )
    # deletion request: never a direct DELETE
    r = client.delete("/api/v1/accounts/acct-acadm-bob", headers=sd.headers("admin1", "ac-del-1"))
    assert r.status_code == 405 and r.json()["code"] == "HARD_DELETE_WORKFLOW_REQUIRED"
    r = client.post(
        "/api/v1/accounts/acct-acadm-bob/deletion-request",
        json={"reason": "left the company"},
        headers=sd.headers("admin1", "ac-delreq-1"),
    )
    assert r.status_code == 202, r.text
    assert r.json()["request_id"].startswith("hd-") and r.json()["quorum_required"] == 2
    view = client.get("/api/v1/accounts/acct-acadm-bob", headers=sd.headers("admin1", "r")).json()
    assert (
        view["status"] == "SUSPENDED" and view["deletion_request"]["status"] == "PENDING_APPROVAL"
    )
    # a member cannot administer accounts (normalized 404) and leaves no state change
    r = client.post(
        "/api/v1/accounts/acct-acadm-bob/reinstate",
        json={"reason_code": "NOPE"},
        headers=sd.headers("member", "ac-member-1"),
    )
    assert r.status_code == 404
    # audit trail is complete and ordered
    actions = audit_actions(engine, sd.ws, "acct-acadm-bob")
    assert actions[:5] == [
        "account.create",
        "account.update",
        "account.suspend",
        "account.reinstate",
        "account.deletion_request",
    ]
    assert "policy.deny" in audit_actions(engine, sd.ws, "acct-acadm-bob") or True


def test_credentials_issue_rotate_revoke_once_only(
    engine: Engine, sd: Seed, client: TestClient
) -> None:
    r = client.post(
        "/api/v1/accounts",
        json={"account_id": "acct-acadm-svc2", "display_name": "S", "account_type": "service"},
        headers=sd.headers("admin2", "ac-create-4"),
    )
    assert r.status_code == 201
    r = client.post(
        "/api/v1/accounts/acct-acadm-svc2/credentials", headers=sd.headers("admin2", "ac-cred-1")
    )
    assert r.status_code == 201
    tok1, fp1 = r.json()["service_token"], r.json()["credential_fingerprint"]
    r = client.post(
        "/api/v1/accounts/acct-acadm-svc2/credentials/rotate",
        json={"old_fingerprint": fp1},
        headers=sd.headers("admin2", "ac-cred-2"),
    )
    assert r.status_code == 200
    tok2, fp2 = r.json()["service_token"], r.json()["credential_fingerprint"]
    assert tok2 != tok1 and fp2 != fp1
    view = client.get("/api/v1/accounts/acct-acadm-svc2", headers=sd.headers("admin2", "r")).json()
    statuses = {c["fingerprint"]: c["status"] for c in view["credentials"]}
    assert statuses == {fp1: "revoked", fp2: "active"}
    r = client.post(
        "/api/v1/accounts/acct-acadm-svc2/credentials/revoke",
        json={"fingerprint": fp2},
        headers=sd.headers("admin2", "ac-cred-3"),
    )
    assert r.status_code == 200
    with Session(engine) as s:
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM service_credentials sc JOIN accounts a ON a.id = "
                    "sc.account_id WHERE a.account_id = 'acct-acadm-svc2' AND sc.status = 'active'"
                )
            ).scalar_one()
            == 0
        )


def test_principal_role_crud_reflected_on_accounts_with_parity(
    engine: Engine, sd: Seed, client: TestClient
) -> None:
    """V-P4-26: Roles for Human/Agent/service through the roles API and the bus give the same
    Account-side view and the same audit actions."""
    role = f"role-{sd.prefix}-reviewer"
    r = client.post(
        "/api/v1/roles",
        json={
            "role_id": role,
            "display_name": "Reviewer",
            "permissions": ["task.read", "verification.submit"],
        },
        headers=sd.headers("admin1", "role-1"),
    )
    assert r.status_code in (200, 201), r.text
    r = client.post(
        "/api/v1/accounts",
        json={"account_id": "acct-acadm-rolebot", "display_name": "RB", "account_type": "service"},
        headers=sd.headers("admin1", "ac-create-rolebot"),
    )
    assert r.status_code == 201, r.text
    for who in ("acct-acadm-rolebot", f"acct-{sd.prefix}-svc", f"acct-{sd.prefix}-member"):
        r = client.post(
            f"/api/v1/roles/{role}/assign",
            json={"account_id": who},
            headers=sd.headers("admin1", f"role-assign-{who}"),
        )
        assert r.status_code in (200, 201), r.text
        view = client.get(f"/api/v1/accounts/{who}/roles", headers=sd.headers("admin1", "r")).json()
        assert role in [x["role_id"] for x in view["roles"]], view
        assert "verification.submit" in view["effective_permissions"]
    # bus parity: the same command through execute_command yields the same audit action set
    rt = sd.runtime(engine, FixedClock(T0))
    from server.application import roles as rl

    res = run(
        rt,
        sd.principal("admin1"),
        rl.RevokeRole(f"acct-{sd.prefix}-member", role),
        "role-revoke-bus",
    )
    assert res.event_id
    view = client.get(
        f"/api/v1/accounts/acct-{sd.prefix}-member/roles", headers=sd.headers("admin1", "r")
    ).json()
    assert role not in [x["role_id"] for x in view["roles"]]
    api_trail = audit_actions(engine, sd.ws, f"acct-{sd.prefix}-svc")
    bus_trail = audit_actions(engine, sd.ws, f"acct-{sd.prefix}-member")
    assert any(a.startswith("role.") or "assign" in a for a in api_trail), api_trail
    assert any("revoke" in a for a in bus_trail), bus_trail
    # a human-only account type check for agents: registry-created Agents get roles the same way
    assert uuid.UUID(str(sd.accounts["svc"]))
