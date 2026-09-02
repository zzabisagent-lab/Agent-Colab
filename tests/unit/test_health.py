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
    body = client.get("/readyz").json()
    assert body["database_configured"] is True
    assert "secret" not in str(body)
    assert "secret" not in repr(settings)
