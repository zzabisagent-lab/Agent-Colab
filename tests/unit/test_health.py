from fastapi.testclient import TestClient

from server.config import PRODUCT_NAME, Settings
from server.main import create_app


def test_healthz_reports_product_name() -> None:
    client = TestClient(create_app(Settings(database_url=None)))
    body = client.get("/healthz").json()
    assert body == {"status": "ok", "product": "Agent-Colab"}
    assert PRODUCT_NAME == "Agent-Colab"


def test_readyz_reports_database_configuration_without_leaking_url() -> None:
    settings = Settings(database_url="postgresql://user:secret@db/x")
    client = TestClient(create_app(settings))
    response = client.get("/readyz")
    body = response.json()
    assert body["database_configured"] is True
    # an unreachable database is not readiness: the instance cannot serve writes (V-P7-06)
    assert response.status_code == 503 and body["status"] == "unavailable"
    assert "secret" not in str(body)
    assert "secret" not in repr(settings)


def test_readyz_without_a_database_is_unavailable() -> None:
    response = TestClient(create_app(Settings(database_url=None))).get("/readyz")
    assert response.status_code == 503
    assert response.json()["database_configured"] is False
