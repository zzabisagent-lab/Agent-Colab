"""P4-02 operations dashboard (V-P4-16 dashboard truth): an injected dependency failure is
reflected accurately within the 60 s probe staleness window; recovery is reflected on refresh."""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.main import create_app
from server.ops import dashboard, probes
from tests.integration.phase4_admin_seed import T0, Seed, seed

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def sd(engine: Engine) -> Seed:
    return seed(engine, "ops")


@pytest.fixture(autouse=True)
def _storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_COLAB_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("AGENT_COLAB_MATTERMOST_URL", raising=False)


def _dep(over: dict[str, Any], name: str) -> dict[str, Any]:
    return next(d for d in over["dependencies"] if d["name"] == name)


def test_injected_failure_visible_within_60s_and_recovery_on_refresh(
    engine: Engine, sd: Seed
) -> None:
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        over = dashboard.overview(s, sd.ws, clock)
    assert _dep(over, "postgres")["status"] == "ok"
    assert _dep(over, "storage")["status"] == "ok"
    assert _dep(over, "mattermost")["status"] == "unconfigured" and over["alerts"] == []
    assert set(over) >= {"tasks", "agents", "outbox", "last_backup", "maintenance", "alerts"}
    # inject: storage becomes unwritable
    probes.set_prober("storage", lambda _s: (False, "disk full (injected)"))
    try:
        with Session(engine) as s, s.begin():
            cached = dashboard.overview(s, sd.ws, clock)  # within the window: cached result
        assert _dep(cached, "storage")["status"] == "ok"
        clock.advance(dt.timedelta(seconds=probes.STALE_S + 1))
        with Session(engine) as s, s.begin():
            failed = dashboard.overview(s, sd.ws, clock)
        assert _dep(failed, "storage")["status"] == "failed"
        assert _dep(failed, "storage")["detail"] == "disk full (injected)"
        assert [a["dependency"] for a in failed["alerts"]] == ["storage"]
        assert failed["alerts"][0]["severity"] == "warning"
        probes.set_prober("postgres", lambda _s: (False, "connection refused (injected)"))
        with Session(engine) as s, s.begin():
            forced = dashboard.overview(s, sd.ws, clock, refresh=True)
        assert {a["dependency"]: a["severity"] for a in forced["alerts"]} == {
            "storage": "warning",
            "postgres": "critical",
        }
    finally:
        probes.set_prober("storage", None)
        probes.set_prober("postgres", None)
    with Session(engine) as s, s.begin():
        recovered = dashboard.overview(s, sd.ws, clock, refresh=True)
    assert recovered["alerts"] == [] and _dep(recovered, "storage")["status"] == "ok"


def test_overview_and_dependencies_api_require_admin(database_url: str, sd: Seed) -> None:
    app = create_app(
        Settings(database_url=database_url, base_url="http://t", master_key_b64=sd.master_key_b64)
    )
    with TestClient(app) as client:
        r = client.get("/api/v1/ops/overview", headers=sd.headers("admin1", "r"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert {d["name"] for d in body["dependencies"]} == set(probes.PROBE_NAMES)
        assert body["maintenance"]["active"] is False
        r = client.get("/api/v1/ops/dependencies?refresh=1", headers=sd.headers("admin1", "r"))
        assert r.status_code == 200 and len(r.json()["items"]) == len(probes.PROBE_NAMES)
        r = client.get("/api/v1/ops/backups", headers=sd.headers("admin1", "r"))
        assert r.status_code == 200 and r.json()["items"] == []
        assert (
            client.get("/api/v1/ops/overview", headers=sd.headers("member", "r")).status_code == 404
        )
    assert os.environ["AGENT_COLAB_ARTIFACT_ROOT"]
