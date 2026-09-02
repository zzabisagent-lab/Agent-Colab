"""V-P1-11: Event list pagination and SSE resume with Last-Event-ID (no gaps, no duplicates)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.events.postgres_store import PostgresEventStore
from server.events.store import AppendRequest
from server.identity.principals import token_hash
from server.main import create_app

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ACTOR = uuid.uuid4()
TOKEN = "svc-events-test-token"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ev', 'ev')"),
            {"i": WS},
        )
        c.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) VALUES (:i, 'acct-ev', :w, 'service', 'ev')"
            ),
            {"i": ACTOR, "w": WS},
        )
        c.execute(
            text(
                "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) VALUES (:i, :a, 'sha256:ev', :h)"
            ),
            {"i": uuid.uuid4(), "a": ACTOR, "h": token_hash(TOKEN)},
        )
    with Session(eng) as s, s.begin():
        st = PostgresEventStore(s)
        for i in range(12):
            st.append(
                AppendRequest(
                    workspace_id=str(WS),
                    aggregate_type="task",
                    aggregate_id="task-ev-1",
                    type="TASK_PROGRESS_REPORTED",
                    actor_account_id=str(ACTOR),
                    correlation_id="corr-ev",
                    idempotency_scope="task:progress",
                    idempotency_key=f"p{i}",
                    payload={"task_id": "task-ev-1", "summary": f"s{i}"},
                    task_id="task-ev-1",
                )
            )
    yield eng
    eng.dispose()


@pytest.fixture()
def app(database_url: str, engine: Engine):  # type: ignore[no-untyped-def]
    return create_app(Settings(database_url=database_url))


HEADERS = {"Authorization": f"Bearer {TOKEN}"}


async def _collect(
    client: httpx.AsyncClient, params: dict[str, object], headers: dict[str, str]
) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    async with client.stream("GET", "/api/v1/events/stream", params=params, headers=headers) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        current_id: int | None = None
        async for line in r.aiter_lines():
            if line.startswith("id: "):
                current_id = int(line[4:])
            elif line.startswith("data: ") and current_id is not None:
                out.append((current_id, json.loads(line[6:])["event_id"]))
    return out


@pytest.mark.anyio
async def test_list_pagination_is_stable_and_bounded(app) -> None:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/events")).status_code == 401
        r = await client.get("/api/v1/events", params={"limit": 5}, headers=HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 5 and body["next_after"] == body["items"][-1]["recorded_seq"]
        assert "sensitive_payload_ciphertext" not in body["items"][0]
        r2 = await client.get("/api/v1/events", params={"limit": 500}, headers=HEADERS)
        assert r2.status_code == 422  # limit bound 100
        seen: list[str] = []
        after = 0
        while True:
            page = (
                await client.get(
                    "/api/v1/events", params={"after": after, "limit": 5}, headers=HEADERS
                )
            ).json()
            seen += [i["event_id"] for i in page["items"]]
            if page["next_after"] is None:
                break
            after = page["next_after"]
        assert len(seen) == 12 and len(set(seen)) == 12


@pytest.mark.anyio
async def test_sse_resume_without_gaps_or_duplicates(app) -> None:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await _collect(client, {"max_events": 5, "poll_seconds": 0.05}, HEADERS)
        assert len(first) == 5
        last_id = first[-1][0]
        # "disconnect" happened when the first stream ended; resume with Last-Event-ID
        rest = await _collect(
            client,
            {"max_events": 7, "poll_seconds": 0.05},
            {**HEADERS, "Last-Event-ID": str(last_id)},
        )
        assert len(rest) == 7
        ids = [i for i, _ in first + rest]
        assert ids == sorted(ids) and len(set(ids)) == 12
        events = [e for _, e in first + rest]
        assert len(set(events)) == 12
        # a Last-Event-ID beyond the end yields nothing new until events arrive (bounded by max_events)
        resp = await client.get("/api/v1/events", params={"after": last_id}, headers=HEADERS)
        assert [i["event_id"] for i in resp.json()["items"]] == events[5:]
