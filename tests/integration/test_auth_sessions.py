from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from server.config import Settings
from server.db.engine import make_engine
from server.identity.principals import token_hash
from server.main import create_app

pytestmark = pytest.mark.db
WS, HUMAN, SVC = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-auth', 'a')"),
            {"i": WS},
        )
        for acc, name, typ, tok in (
            (HUMAN, "acct-auth-h", "human", "svc-auth-human-token"),
            (SVC, "acct-auth-s", "service", "svc-auth-service-token"),
        ):
            c.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
            c.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                    "VALUES (:i, :a, :f, :h)"
                ),
                {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{name}", "h": token_hash(tok)},
            )
    yield eng
    eng.dispose()


def test_session_cookie_lifecycle(database_url: str, engine: Engine) -> None:
    app = create_app(Settings(database_url=database_url))
    with TestClient(app) as c:
        assert c.get("/api/v1/auth/me").status_code == 401
        bad = c.post("/api/v1/auth/sessions", json={"service_token": "svc-auth-service-token"})
        assert bad.status_code == 401  # only human accounts get sessions
        ok = c.post("/api/v1/auth/sessions", json={"service_token": "svc-auth-human-token"})
        assert ok.status_code == 201 and "agent_colab_session" in c.cookies
        assert "httponly" in ok.headers["set-cookie"].lower()
        me = c.get("/api/v1/auth/me")
        assert me.status_code == 200 and me.json()["credential_kind"] == "session"
        assert c.delete("/api/v1/auth/sessions").status_code == 204
        assert c.get("/api/v1/auth/me").status_code == 401
