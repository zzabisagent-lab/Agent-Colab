"""V-P0-07: same implementer/verifier creation is rejected at DB and API (P0-07)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from server.config import Settings
from server.db.engine import make_engine
from server.identity.service_tokens import token_hash
from server.main import create_app
from server.verification.independence import (
    Identity,
    VerificationIndependenceError,
    check_independence,
)

pytestmark = pytest.mark.db

WS = "ws-test"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as conn:
        ws = uuid.uuid4()
        conn.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:id, :w, 'Test')"),
            {"id": ws, "w": WS},
        )
        for acct, typ in (
            ("acct-impl", "agent"),
            ("acct-ver", "agent"),
            ("acct-alias", "agent"),
            ("acct-svc", "service"),
        ):
            conn.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                    "VALUES (:id, :a, :w, :t, :a)"
                ),
                {"id": uuid.uuid4(), "a": acct, "w": ws, "t": typ},
            )
        conn.execute(
            text(
                "INSERT INTO account_aliases (account_id, alias_of_account_id, reason) "
                "SELECT a.id, b.id, 'same operator' FROM accounts a, accounts b "
                "WHERE a.account_id = 'acct-alias' AND b.account_id = 'acct-impl'"
            )
        )
        conn.execute(
            text(
                "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                "SELECT :id, id, 'sha256:svc', :h FROM accounts WHERE account_id = 'acct-svc'"
            ),
            {"id": uuid.uuid4(), "h": token_hash("svc-token-test")},
        )
    yield eng
    eng.dispose()


def _ids(engine: Engine) -> dict[str, uuid.UUID]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT account_id, id FROM accounts")).all()
        ws = conn.execute(
            text("SELECT id FROM workspaces WHERE workspace_id = :w"), {"w": WS}
        ).scalar_one()
    out = {str(r[0]): uuid.UUID(str(r[1])) for r in rows}
    out["ws"] = uuid.UUID(str(ws))
    return out


def _insert_sql(
    engine: Engine,
    impl: str,
    ver: str,
    impl_fp: str,
    ver_fp: str,
    impl_agent: str | None,
    ver_agent: str | None,
) -> None:
    ids = _ids(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO verification_runs (id, verification_id, workspace_id, target_type, target_id, "
                "implementer_account_id, verifier_account_id, implementer_agent_id, verifier_agent_id, "
                "implementer_credential_fingerprint, verifier_credential_fingerprint, identity_graph_version, "
                "effective_policy_hash, criteria_version, target_commit, snapshot_hash, created_by_account_id) "
                "VALUES (:id, :vid, :ws, 'phase', 'phase-0', :impl, :ver, :ia, :va, :ifp, :vfp, 'identity-v8-001', "
                "'sha256:policy', 'v8.0', 'deadbeef', 'sha256:snap', :impl)"
            ),
            {
                "id": uuid.uuid4(),
                "vid": f"vr-{uuid.uuid4().hex[:12]}",
                "ws": ids["ws"],
                "impl": ids[impl],
                "ver": ids[ver],
                "ia": impl_agent,
                "va": ver_agent,
                "ifp": impl_fp,
                "vfp": ver_fp,
            },
        )


def test_db_rejects_same_account(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="ck_vr_distinct_accounts"):
        _insert_sql(engine, "acct-impl", "acct-impl", "fp-a", "fp-b", None, None)


def test_db_rejects_same_agent(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="ck_vr_distinct_agents"):
        _insert_sql(engine, "acct-impl", "acct-ver", "fp-a", "fp-b", "agent-x", "agent-x")


def test_db_rejects_same_credential(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="ck_vr_distinct_credentials"):
        _insert_sql(engine, "acct-impl", "acct-ver", "fp-a", "fp-a", None, None)


def test_db_accepts_independent_pair_and_snapshot_is_immutable(engine: Engine) -> None:
    _insert_sql(engine, "acct-impl", "acct-ver", "fp-a", "fp-b", "agent-a", "agent-b")
    with engine.connect() as conn:
        vid = conn.execute(
            text("SELECT verification_id FROM verification_runs LIMIT 1")
        ).scalar_one()
    with pytest.raises(Exception, match="VERIFICATION_SNAPSHOT_IMMUTABLE"):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE verification_runs SET target_commit = 'other' WHERE verification_id = :v"
                ),
                {"v": vid},
            )
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM verification_runs WHERE verification_id = :v"), {"v": vid})
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM verification_runs WHERE verification_id = :v"),
                {"v": vid},
            ).scalar_one()
            == 1
        )


def test_application_check_covers_alias_graph() -> None:
    impl = Identity("acct-impl", "fp-a", "agent-a")
    with pytest.raises(VerificationIndependenceError) as exc:
        check_independence(
            impl, Identity("acct-alias", "fp-b", "agent-b"), {"acct-alias": "acct-impl"}
        )
    assert exc.value.code == "VERIFIER_ALIAS_OF_IMPLEMENTER"
    with pytest.raises(VerificationIndependenceError) as exc2:
        check_independence(
            impl,
            Identity("acct-ver", "fp-b", "agent-b"),
            verifier_permissions=frozenset({"task.read"}),
        )
    assert exc2.value.code == "VERIFIER_NOT_ELIGIBLE"
    with pytest.raises(VerificationIndependenceError) as exc3:
        check_independence(
            impl, Identity("acct-ver", "fp-b", "agent-b"), commit_author_account_id="acct-ver"
        )
    assert exc3.value.code == "VERIFIER_IS_COMMIT_AUTHOR"
    check_independence(impl, Identity("acct-ver", "fp-b", "agent-b"), {"acct-alias": "acct-impl"})


@pytest.fixture()
def client(database_url: str) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=database_url))
    with TestClient(app) as c:
        yield c


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "workspace_id": WS,
        "target_type": "phase",
        "target_id": "phase-0",
        "implementer_account_id": "acct-impl",
        "verifier_account_id": "acct-ver",
        "implementer_credential_fingerprint": "sha256:impl",
        "verifier_credential_fingerprint": "sha256:ver",
        "target_commit": "deadbeef",
        "identity_graph_version": "identity-v8-001",
        "effective_policy_hash": "sha256:policy",
        "implementer_agent_id": "agent-claude-code",
        "verifier_agent_id": "agent-codex",
        "phase": 0,
    }
    body.update(overrides)
    return body


HEADERS = {"Authorization": "Bearer svc-token-test", "Idempotency-Key": "k1"}


def test_api_rejects_same_implementer_and_verifier(client: TestClient, engine: Engine) -> None:
    for overrides, code in (
        ({"verifier_account_id": "acct-impl"}, "VERIFIER_SAME_ACCOUNT"),
        ({"verifier_agent_id": "agent-claude-code"}, "VERIFIER_SAME_AGENT"),
        ({"verifier_credential_fingerprint": "sha256:impl"}, "VERIFIER_SAME_CREDENTIAL"),
        ({"verifier_account_id": "acct-alias"}, "VERIFIER_ALIAS_OF_IMPLEMENTER"),
    ):
        r = client.post("/api/v1/verification-runs", json=_body(**overrides), headers=HEADERS)
        assert r.status_code == 409, r.text
        assert r.headers["content-type"].startswith("application/problem+json")
        assert r.json()["code"] == code
    with engine.connect() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM verification_runs WHERE verifier_account_id = implementer_account_id"
            )
        ).scalar_one()
    assert n == 0


def test_api_requires_credential_and_creates_independent_run(client: TestClient) -> None:
    assert client.post("/api/v1/verification-runs", json=_body()).status_code == 401
    bad = client.post(
        "/api/v1/verification-runs",
        json=_body(),
        headers={**HEADERS, "Authorization": "Bearer nope"},
    )
    assert bad.status_code == 401 and bad.json()["code"] == "AUTH_INVALID"
    ok = client.post("/api/v1/verification-runs", json=_body(), headers=HEADERS)
    assert ok.status_code == 201, ok.text
    assert ok.json()["verification_id"].startswith("vr-")
