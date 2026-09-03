"""P4-13 maintenance mode (V-P4-32): non-admin writes 503 + Retry-After; reads and admin writes
continue; the outbox drain keeps delivering; the scheduler hook pauses; enter/exit are audited and
announced."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.identity.principals import token_hash
from server.main import create_app
from server.maintenance import mode
from server.notifications.outbox import StubProvider, drain
from server.policy.repository import PostgresPolicyRepository
from server.secrets.envelope import new_master_key
from tests.integration.setup_harness import install_fake_reauth

pytestmark = pytest.mark.db
WS, CHANNEL, ADMIN, MEMBER, SERVICE = (uuid.uuid4() for _ in range(5))
TOK_ADMIN, TOK_MEMBER = "svc-maint-admin-0001", "svc-maint-member-0001"
T0 = dt.datetime(2026, 1, 6, 9, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-maint', 'm')"),
            {"i": WS},
        )
        for acc, name, typ, tok in (
            (ADMIN, "acct-maint-admin", "human", TOK_ADMIN),
            (MEMBER, "acct-maint-member", "human", TOK_MEMBER),
            (SERVICE, "acct-maint-system", "service", None),
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
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, "
                "display_name) VALUES (:i, 'chan-maint', :w, 'work', 'm')"
            ),
            {"i": CHANNEL, "w": WS},
        )
        for acc in (ADMIN, MEMBER):
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHANNEL, "a": acc},
            )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "maint-admin", "admin")
        repo.commit_role_version(
            s, "maint-admin", ["admin.settings", "ops.manage", "task.*"], [], {}, ADMIN
        )
        repo.create_role(s, WS, "maint-member", "member")
        repo.commit_role_version(s, "maint-member", ["task.*"], [], {}, ADMIN)
        repo.assign_role(s, ADMIN, "maint-admin", ADMIN, dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
        repo.assign_role(s, MEMBER, "maint-member", ADMIN, dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def app_client(database_url: str, engine: Engine) -> Iterator[tuple[TestClient, FixedClock]]:
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    app = create_app(Settings(database_url=database_url, master_key_b64=new_master_key()))
    clock = FixedClock(T0)
    app.state.runtime.clock = clock
    mode.reset_cache()
    with TestClient(app) as c:
        yield c, clock


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": uuid.uuid4().hex}


def _task_body() -> dict[str, object]:
    return {
        "title": "during maintenance",
        "channel_id": str(CHANNEL),
        "domain": "research",
        "risk": "LOW",
        "criteria": [{"statement": "done", "check_type": "evidence", "required": True}],
    }


def test_maintenance_mode_end_to_end(
    app_client: tuple[TestClient, FixedClock], engine: Engine
) -> None:
    client, clock = app_client
    admin, member = _h(TOK_ADMIN), _h(TOK_MEMBER)
    r = client.put("/api/v1/settings/ops.channel_id", json={"value": "ops-room"}, headers=admin)
    assert r.status_code == 200, r.text
    # entering needs a fresh MFA re-authentication
    r = client.post(
        "/api/v1/maintenance/enter",
        json={"reason": "db upgrade", "retry_after_s": 120},
        headers=admin,
    )
    assert r.status_code == 401 and r.json()["code"] == "REAUTH_REQUIRED", r.text
    restore = install_fake_reauth(clock)
    try:
        r = client.post(
            "/api/v1/maintenance/enter",
            json={"reason": "db upgrade", "retry_after_s": 120},
            headers=admin,
        )
        assert (
            r.status_code == 200 and r.json()["active"] is True and r.json()["announcement_id"]
        ), r.text
        r = client.post("/api/v1/maintenance/enter", json={"reason": "again"}, headers=member)
        assert r.status_code == 404  # non-admin cannot toggle maintenance
        mode.reset_cache()
        # non-admin writes: 503 + Retry-After, zero side effects
        r = client.post("/api/v1/tasks", json=_task_body(), headers=member)
        assert (
            r.status_code == 503
            and r.headers["retry-after"] == "120"
            and r.json()["code"] == "MAINTENANCE_MODE"
        )
        with Session(engine) as s:
            assert (
                s.execute(
                    text("SELECT count(*) FROM tasks_projection WHERE workspace_id = :w"), {"w": WS}
                ).scalar_one()
                == 0
            )
            assert mode.scheduler_paused(s) is True  # zero due Run claims
            # the outbox drain continues: a pending notification is delivered during maintenance
            from server.events.postgres_store import PostgresEventStore

            provider = StubProvider()
            result = drain(
                s, provider, PostgresEventStore(s, clock=clock), clock, str(SERVICE), str(WS)
            )
            s.commit()
        assert result.sent >= 1 and any(d == "mattermost:ops-room" for d, _ in provider.sent)
        assert any(p.get("event_type") == "MAINTENANCE_MODE_ENTERED" for _, p in provider.sent)
        # reads and administrative writes continue
        assert client.get("/api/v1/maintenance", headers=member).status_code == 404  # read, not 503
        assert client.get("/api/v1/maintenance", headers=admin).json()["active"] is True
        r = client.put(
            "/api/v1/settings/scheduler.poll_interval_s", json={"value": 9}, headers=admin
        )
        assert r.status_code == 200, r.text
        # exit: audited and announced; member writes work again
        r = client.post("/api/v1/maintenance/exit", headers=admin)
        assert r.status_code == 200 and r.json()["active"] is False and r.json()["announcement_id"]
        mode.reset_cache()
        r = client.post("/api/v1/tasks", json=_task_body(), headers=member)
        assert r.status_code == 201, r.text
        with Session(engine) as s:
            actions = [
                a
                for (a,) in s.execute(
                    text(
                        "SELECT action FROM audit_events WHERE workspace_id = :w AND action "
                        "LIKE 'maintenance.%' ORDER BY id"
                    ),
                    {"w": WS},
                ).all()
            ]
            assert actions == ["maintenance.enter", "maintenance.exit"]
            assert mode.scheduler_paused(s) is False
            announcements = s.execute(
                text(
                    "SELECT count(*) FROM delivery_outbox WHERE workspace_id = :w AND "
                    "dedupe_key LIKE 'maintenance|%'"
                ),
                {"w": WS},
            ).scalar_one()
        assert announcements == 2
    finally:
        restore()
