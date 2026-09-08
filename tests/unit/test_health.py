from __future__ import annotations

from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from server.config import PRODUCT_NAME, Settings
from server.main import create_app


class _Session:
    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object) -> None:
        return None


def test_healthz_reports_product_name() -> None:
    client = TestClient(create_app(Settings(database_url=None)))
    body = client.get("/healthz").json()
    assert body == {"status": "ok", "product": "Agent-Colab"}
    assert PRODUCT_NAME == "Agent-Colab"


def test_readyz_without_database_fails_closed_without_leaking_url() -> None:
    settings = Settings(database_url=None)
    client = TestClient(create_app(settings))
    response = client.get("/readyz")
    body = response.json()
    assert response.status_code == 503
    assert body["database_configured"] is False
    assert body["database_reachable"] is False
    assert "secret" not in str(body)
    assert "secret" not in repr(settings)


def test_readyz_reports_database_reachable_without_leaking_url() -> None:
    settings = Settings(database_url="postgresql://user:***@db/x")
    app = create_app(settings)
    app.state.session_factory = lambda: _Session()
    client = TestClient(app)
    response = client.get("/readyz")
    body = response.json()
    assert response.status_code == 200
    assert body["database_configured"] is True
    assert body["database_reachable"] is True
    assert "secret" not in str(body)
    assert "secret" not in repr(settings)


def test_readyz_database_outage_fails_closed() -> None:
    settings = Settings(database_url="postgresql://user:***@db/x")
    app = create_app(settings)
    outage = OperationalError("SELECT 1", {}, RuntimeError("synthetic outage"))
    app.state.session_factory = Mock(side_effect=outage)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/readyz")
    body = response.json()
    assert response.status_code == 503
    assert body["database_configured"] is True
    assert body["database_reachable"] is False
    assert "synthetic outage" not in str(body)
