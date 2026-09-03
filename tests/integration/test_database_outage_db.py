"""V-P7-06: a database outage answers 503 and fails readiness, and corrupts nothing.

The outage is produced by pointing a live app at a database that is then made unreachable, which
is what a real outage looks like to the process: connections fail, writes must answer 503 rather
than 500, readiness must report unavailable, and no write may be recorded. After recovery the
Event chain of the workspace still verifies, byte for byte.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import CONNECT_TIMEOUT_S, make_engine, normalize_url
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.events.store import AppendRequest
from server.main import create_app
from tests.conftest import TEST_URL

pytestmark = pytest.mark.db
NOW = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.UTC)
READINESS_BUDGET_S = 30  # V-P7-06: readiness and writes must fail inside this window


@pytest.fixture
def outage_database() -> Iterator[tuple[str, str]]:
    """A disposable database this Test can actually take away (dropped, then recreated)."""
    base = normalize_url(TEST_URL)
    maint = create_engine(base, isolation_level="AUTOCOMMIT")
    name = f"colab_outage_{uuid.uuid4().hex[:10]}"
    with maint.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.rsplit("/", 1)[0] + f"/{name}"
    try:
        yield url, name
    finally:
        with maint.connect() as c:
            c.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        maint.dispose()


def _stop(name: str) -> None:
    """Take the database away: terminate its backends and rename it out from under the app."""
    maint = create_engine(normalize_url(TEST_URL), isolation_level="AUTOCOMMIT")
    with maint.connect() as c:
        c.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ),
            {"n": name},
        )
        c.execute(text(f'ALTER DATABASE "{name}" RENAME TO "{name}_down"'))
    maint.dispose()


def _start(name: str) -> None:
    maint = create_engine(normalize_url(TEST_URL), isolation_level="AUTOCOMMIT")
    with maint.connect() as c:
        c.execute(text(f'ALTER DATABASE "{name}_down" RENAME TO "{name}"'))
    maint.dispose()


def _chain(engine: Engine) -> list[tuple[str, str, str]]:
    with Session(engine) as s:
        return [
            (str(r[0]), str(r[1]), str(r[2] or ""))
            for r in s.execute(
                text(
                    "SELECT event_id, content_hash, coalesce(previous_hash, '') FROM events "
                    "ORDER BY recorded_seq"
                )
            ).all()
        ]


def _chain_links(engine: Engine) -> bool:
    """Per aggregate, each Event's previous_hash is the prior Event's content_hash."""
    with Session(engine) as s:
        rows = (
            s.execute(
                text(
                    "SELECT aggregate_type, aggregate_id, aggregate_seq, content_hash, "
                    "previous_hash FROM events ORDER BY aggregate_type, aggregate_id, aggregate_seq"
                )
            )
            .mappings()
            .all()
        )
    last: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (str(row["aggregate_type"]), str(row["aggregate_id"]))
        expected = last.get(key)
        if (row["previous_hash"] or None) != expected:
            return False
        last[key] = str(row["content_hash"])
    return True


def test_database_outage_answers_503_and_corrupts_nothing(
    outage_database: tuple[str, str], database_url: str
) -> None:
    url, name = outage_database
    from server.db.engine import run_migrations

    run_migrations(url)
    engine = make_engine(url)
    clock = FixedClock(NOW)
    workspace = uuid.uuid4()
    with Session(engine) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-out', 'out')"),
            {"i": workspace},
        )
        s.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-out', :w, 'human', 'out')"
            ),
            {"i": uuid.uuid4(), "w": workspace},
        )
    with Session(engine) as s, s.begin():
        actor = s.execute(
            text("SELECT id FROM accounts WHERE account_id = 'acct-out'")
        ).scalar_one()
        PostgresEventStore(s, clock=clock).append(
            AppendRequest(
                workspace_id=str(workspace),
                aggregate_type="task",
                aggregate_id="task-before-outage",
                type="TASK_CREATED",
                actor_account_id=str(actor),
                correlation_id="corr-before",
                idempotency_scope="task:create",
                idempotency_key="before-outage",
                payload={
                    "task_id": "task-before-outage",
                    "root_task_id": "task-before-outage",
                    "channel_id": "chan-out",
                    "title": "before the outage",
                    "domain": "general",
                    "risk": "LOW",
                },
            )
        )
    before = _chain(engine)
    assert before and _chain_links(engine)

    client = TestClient(create_app(Settings(database_url=url)), raise_server_exceptions=False)
    assert client.get("/readyz").status_code == 200

    _stop(name)
    engine.dispose()
    try:
        started = dt.datetime.now(dt.UTC)
        ready = client.get("/readyz")
        write = client.post(
            "/api/v1/tasks",
            json={"title": "during outage", "channel_id": "chan-out", "domain": "general"},
            headers={"Authorization": "Bearer svc-out", "Idempotency-Key": "during-outage"},
        )
        elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()

        assert ready.status_code == 503, ready.text
        assert ready.json()["status"] == "unavailable"
        # a database outage is an availability failure, never a 500 and never a success
        assert write.status_code == 503, (write.status_code, write.text[:200])
        assert write.json()["code"] == "DATABASE_UNAVAILABLE"
        assert write.headers.get("Retry-After")
        assert elapsed <= READINESS_BUDGET_S, elapsed
        assert CONNECT_TIMEOUT_S <= READINESS_BUDGET_S
    finally:
        _start(name)

    recovered = make_engine(url)
    try:
        assert client.get("/readyz").status_code == 200
        after = _chain(recovered)
        assert after == before  # zero writes during the outage, zero corruption after it
        assert _chain_links(recovered)
    finally:
        recovered.dispose()
