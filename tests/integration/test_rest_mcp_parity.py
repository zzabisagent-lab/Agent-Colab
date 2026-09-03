"""V-P1-26: REST and MCP execute the same command handlers with identical Policy result, stable
errors, Event shapes, idempotency and expected-seq behaviour; zero bypass side effects."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.identity.principals import token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

WS = uuid.uuid4()
CHANNEL = uuid.uuid4()
HUMAN = uuid.uuid4()
AGENT = uuid.uuid4()
TOKEN_H = "svc-parity-human"
TOKEN_A = "svc-parity-agent"
ENGINE: dict[str, Engine] = {}


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    ENGINE["engine"] = eng
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-par', 'par')"),
            {"i": WS},
        )
        for acc, name, typ, tok in (
            (HUMAN, "acct-par-h", "human", TOKEN_H),
            (AGENT, "acct-par-a", "agent", TOKEN_A),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, account_id, workspace_id, account_type, display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
            s.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                    "VALUES (:i, :a, :f, :h)"
                ),
                {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{name}", "h": token_hash(tok)},
            )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-par', :w, 'work', 'par')"
            ),
            {"i": CHANNEL, "w": WS},
        )
        for acc in (HUMAN, AGENT):
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHANNEL, "a": acc},
            )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "par-worker", "Parity worker")
        repo.commit_role_version(
            s, "par-worker", ["task.*", "artifact.*", "approval.*", "work.poll"], [], {}, HUMAN
        )
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        for acc in (HUMAN, AGENT):
            repo.assign_role(s, acc, "par-worker", HUMAN, now)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def server(database_url: str, engine: Engine) -> Iterator[str]:
    """Run the real app on a loopback port so MCP (Streamable HTTP) and REST share one process."""
    import socket
    import threading
    import time

    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = create_app(Settings(database_url=database_url, base_url=base))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    assert srv.started
    yield base
    srv.should_exit = True
    thread.join(timeout=10)


def _h(token: str, key: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key, **extra}


@asynccontextmanager
async def mcp_session(base: str, token: str) -> AsyncIterator[ClientSession]:
    async with (
        httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, *_),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def call(session: ClientSession, tool: str, **args: Any) -> dict[str, Any]:
    result = await session.call_tool(tool, args)
    if result.structured_content:
        return dict(result.structured_content)
    text_out = result.content[0].text  # type: ignore[union-attr]
    try:
        return dict(json.loads(text_out))
    except json.JSONDecodeError:
        return {"error": {"code": "MCP_TOOL_ERROR", "status": 500, "detail": text_out}}


@pytest.mark.anyio
async def test_rest_and_mcp_share_handlers_errors_and_idempotency(server: str) -> None:
    async with (
        httpx.AsyncClient(base_url=server) as rest,
        mcp_session(server, TOKEN_H) as mcp,
    ):
        body = {
            "title": "Parity task",
            "channel_id": str(CHANNEL),
            "domain": "research",
            "risk": "LOW",
            "criteria": [
                {"statement": "report attached", "check_type": "evidence", "required": True}
            ],
        }
        r1 = await rest.post("/api/v1/tasks", json=body, headers=_h(TOKEN_H, "par-create-1"))
        assert r1.status_code == 201, r1.text
        rest_created = r1.json()
        with Session(ENGINE["engine"]) as s:
            n_crit = s.execute(
                text("SELECT count(*) FROM task_acceptance_criteria WHERE task_id = :t"),
                {"t": rest_created["resource_id"]},
            ).scalar_one()
        assert n_crit == 1, (n_crit, rest_created)
        r1b = await rest.post("/api/v1/tasks", json=body, headers=_h(TOKEN_H, "par-create-1"))
        assert r1b.json()["event_id"] == rest_created["event_id"] and r1b.json()["replayed"] is True
        changed = {**body, "title": "changed"}
        r1c = await rest.post("/api/v1/tasks", json=changed, headers=_h(TOKEN_H, "par-create-1"))
        assert r1c.status_code == 409 and r1c.json()["code"] == "IDEMPOTENCY_CONFLICT"

        m1 = await call(mcp, "task_create", **body, idempotency_key="par-create-mcp-1")
        assert "error" not in m1, m1
        m1b = await call(mcp, "task_create", **body, idempotency_key="par-create-mcp-1")
        assert m1b["event_id"] == m1["event_id"] and m1b["replayed"] is True
        m1c = await call(mcp, "task_create", **changed, idempotency_key="par-create-mcp-1")
        assert m1c["error"]["code"] == "IDEMPOTENCY_CONFLICT" and m1c["error"]["status"] == 409
        assert set(rest_created) == set(m1) - {"schema_id"}

        # the same delegate command through both transports: identical Event shape and seq
        rd = await rest.post(
            f"/api/v1/tasks/{rest_created['resource_id']}/delegate",
            json={"assignee_account_id": "acct-par-a"},
            headers=_h(TOKEN_H, "par-del-1", **{"If-Match": "2"}),
        )
        assert rd.status_code == 200, rd.text
        md = await call(
            mcp,
            "task_delegate",
            task_id=m1["resource_id"],
            assignee_account_id="acct-par-a",
            idempotency_key="par-del-mcp-1",
            expected_seq=2,
        )
        assert "error" not in md and md["aggregate_seq"] == rd.json()["aggregate_seq"] == 2

        # optimistic concurrency and not-found behave identically
        rbad = await rest.post(
            f"/api/v1/tasks/{rest_created['resource_id']}/progress",
            json={"summary": "x"},
            headers=_h(TOKEN_H, "par-prog-1", **{"If-Match": "9"}),
        )
        mbad = await call(
            mcp,
            "task_progress",
            task_id=m1["resource_id"],
            summary="x",
            idempotency_key="par-prog-mcp-1",
            expected_seq=9,
        )
        assert rbad.status_code == 409 and mbad["error"]["status"] == 409
        assert rbad.json()["code"] == mbad["error"]["code"]
        rnf = await rest.post("/api/v1/tasks/task-missing/accept", headers=_h(TOKEN_A, "par-acc-x"))
        mnf = await call(
            mcp, "task_accept", task_id="task-missing", idempotency_key="par-acc-mcp-x"
        )
        assert rnf.status_code == 404 and mnf["error"]["status"] == 404
        assert rnf.json()["code"] == mnf["error"]["code"]

        # policy applied identically: an actor with an explicit deny is refused on both paths
        with Session(ENGINE["engine"]) as s, s.begin():
            repo = PostgresPolicyRepository()
            repo.create_role(s, WS, "par-reader", "Reader")
            repo.commit_role_version(s, "par-reader", ["task.read"], ["task.delegate"], {}, HUMAN)
            repo.assign_role(s, AGENT, "par-reader", HUMAN, dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
        async with mcp_session(server, TOKEN_A) as mcp_agent:
            rden = await rest.post(
                f"/api/v1/tasks/{rest_created['resource_id']}/delegate",
                json={"assignee_account_id": "acct-par-h"},
                headers=_h(TOKEN_A, "par-del-den"),
            )
            mden = await call(
                mcp_agent,
                "task_delegate",
                task_id=m1["resource_id"],
                assignee_account_id="acct-par-h",
                idempotency_key="par-del-den-mcp",
            )
        assert rden.status_code == 404 and mden["error"]["status"] == 404  # normalized forbidden
        assert rden.json()["code"] == mden["error"]["code"] == "EXPLICIT_DENY"

    # the same approval request through both transports (V-P1-26: create/delegate/approval)
    async with httpx.AsyncClient(base_url=server) as rest, mcp_session(server, TOKEN_H) as mcp:
        req = {
            "subject_type": "task",
            "subject_id": rest_created["resource_id"],
            "action": "external_send",
        }
        ra = await rest.post("/api/v1/approvals", json=req, headers=_h(TOKEN_H, "par-apr-1"))
        assert ra.status_code == 201, ra.text
        ma = await call(
            mcp,
            "approval_request",
            **{**req, "subject_id": m1["resource_id"]},
            idempotency_key="par-apr-mcp-1",
        )
        assert "error" not in ma, ma
        assert set(ra.json()) == set(ma) - {"schema_id"}
        got = await rest.get(
            f"/api/v1/approvals/{ra.json()['resource_id']}", headers=_h(TOKEN_H, "x")
        )
        assert got.status_code == 200 and got.json()["status"] == "PENDING"
        # subject handlers answer identically on both paths (schedule is active from Phase 5)
        rs = await rest.post(
            "/api/v1/approvals",
            json={**req, "subject_type": "schedule", "subject_id": "sch-1"},
            headers=_h(TOKEN_H, "par-apr-2"),
        )
        ms = await call(
            mcp,
            "approval_request",
            **{**req, "subject_type": "schedule", "subject_id": "sch-1"},
            idempotency_key="par-apr-mcp-2",
        )
        assert rs.json()["code"] == ms["error"]["code"] == "SUBJECT_NOT_FOUND"
        assert rs.status_code == ms["error"]["status"]

    # zero bypass side effects: every Event came through the bus with a known idempotency scope
    with Session(ENGINE["engine"]) as s:
        rows = s.execute(
            text("SELECT idempotency_scope FROM events WHERE workspace_id = :w"), {"w": WS}
        ).all()
    assert rows and all(r[0].startswith(("task:", "approval:")) for r in rows)
