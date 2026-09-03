"""P4-02 audit explorer (V-P4-23): search by period/actor/action with stable cursor pagination and
CSV/JSONL export; secret redaction is preserved end to end."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.main import create_app
from server.observability.audit import append_audit
from server.ops import audit_search as audit
from tests.integration.phase4_admin_seed import T0, Seed, seed

pytestmark = pytest.mark.db
SECRET_VALUE = "hunter2-canary-should-never-appear"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def sd(engine: Engine) -> Seed:
    s = seed(engine, "aud")
    with Session(engine) as session, session.begin():
        for i in range(7):
            append_audit(
                session,
                action="account.update" if i % 2 else "secret.grant",
                target_type="account",
                target_id=f"acct-aud-target-{i}",
                result="OK",
                actor_label="acct-aud-admin1" if i < 5 else "acct-aud-admin2",
                correlation_id=f"corr-aud-{i}",
                workspace_id=s.ws,
                actor_account_id=s.accounts["admin1"],
                metadata={"token": SECRET_VALUE, "note": f"n{i}"},
                clock=FixedClock(T0 + dt.timedelta(minutes=i)),
            )
    return s


def test_search_filters_cursor_and_redaction(engine: Engine, sd: Seed) -> None:
    with Session(engine) as s:
        page1 = audit.search(s, audit.AuditQuery(sd.ws, actor="acct-aud-admin1", limit=2))
        assert [i["target_id"] for i in page1["items"]] == [
            "acct-aud-target-0",
            "acct-aud-target-1",
        ]
        assert page1["next_cursor"]
        page2 = audit.search(
            s,
            audit.AuditQuery(sd.ws, actor="acct-aud-admin1", limit=2, cursor=page1["next_cursor"]),
        )
        assert [i["target_id"] for i in page2["items"]] == [
            "acct-aud-target-2",
            "acct-aud-target-3",
        ]
        page3 = audit.search(
            s,
            audit.AuditQuery(sd.ws, actor="acct-aud-admin1", limit=2, cursor=page2["next_cursor"]),
        )
        assert [i["target_id"] for i in page3["items"]] == ["acct-aud-target-4"] and page3[
            "next_cursor"
        ] is None
        by_action = audit.search(s, audit.AuditQuery(sd.ws, action="secret.*"))
        assert {i["action"] for i in by_action["items"]} == {"secret.grant"}
        window = audit.search(
            s,
            audit.AuditQuery(
                sd.ws, since=T0 + dt.timedelta(minutes=5), until=T0 + dt.timedelta(minutes=7)
            ),
        )
        assert [i["target_id"] for i in window["items"]] == [
            "acct-aud-target-5",
            "acct-aud-target-6",
        ]
        for item in page1["items"] + by_action["items"]:
            assert item["redacted_metadata"]["token"] == "<redacted>"
            assert item["redacted_metadata"]["note"].startswith("n")
        assert audit.search(s, audit.AuditQuery(sd.ws, limit=1000))["limit"] == audit.MAX_LIMIT


def test_api_search_and_export_preserve_redaction(database_url: str, sd: Seed) -> None:
    app = create_app(
        Settings(database_url=database_url, base_url="http://t", master_key_b64=sd.master_key_b64)
    )
    with TestClient(app) as client:
        h = sd.headers("admin1", "r")
        r = client.get(
            "/api/v1/audit", params={"actor": "acct-aud-admin2", "from": T0.isoformat()}, headers=h
        )
        assert r.status_code == 200, r.text
        assert [i["target_id"] for i in r.json()["items"]] == [
            "acct-aud-target-5",
            "acct-aud-target-6",
        ]
        assert client.get("/api/v1/audit", params={"limit": 101}, headers=h).status_code == 422
        assert (
            client.get("/api/v1/audit", params={"from": "yesterday"}, headers=h).status_code == 400
        )
        assert client.get("/api/v1/audit", headers=sd.headers("member", "r")).status_code == 404
        for fmt, ctype in (("jsonl", "application/x-ndjson"), ("csv", "text/csv")):
            r = client.get(
                "/api/v1/audit/export", params={"format": fmt, "action": "secret.grant"}, headers=h
            )
            assert r.status_code == 200 and r.headers["content-type"].startswith(ctype)
            body = r.text
            assert SECRET_VALUE not in body and "<redacted>" in body
            assert body.count("acct-aud-target-") == 4  # 4 secret.grant rows (i in 0,2,4,6)
    assert uuid.UUID(str(sd.ws))
