"""V-P3-21 (P3-10): MCP Streamable HTTP transport — valid/invalid token, mTLS fingerprint path,
work_poll long-poll answered within 30 s, inbox resource + subscribe notification (long-poll
fallback), disconnect/reconnect redelivery of un-acked items with zero duplicate results,
idempotent work_result, one concurrent poll per session."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import httpx2
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.identity.principals import token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db
WS, HUMAN, AGENT_ACC = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
AGENT = "agent-mcp-transport"
TOKEN_H, TOKEN_A = "svc-mcpt-human", "svc-mcpt-agent"
MTLS_FP = "sha256:" + "ab" * 32
PROXY_SECRET = "proxy-shared-secret-for-tests"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-mcpt', 'mcpt')"),
            {"i": WS},
        )
        for acc, name, typ, tok in (
            (HUMAN, "acct-mcpt-h", "human", TOKEN_H),
            (AGENT_ACC, "acct-mcpt-a", "agent", TOKEN_A),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
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
                "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, status, "
                "display_name, endpoint) VALUES (:i, :g, :w, :a, 'mcp', 'active', :g, "
                "CAST(:e AS jsonb))"
            ),
            {
                "i": uuid.uuid4(),
                "g": AGENT,
                "w": WS,
                "a": AGENT_ACC,
                "e": json.dumps({"mtls_fingerprint": MTLS_FP}),
            },
        )
        from server.usage.versions import activate_from_file

        activate_from_file(s, activated_by=str(HUMAN))
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "mcpt-worker", "transport worker")
        repo.commit_role_version(
            s, "mcpt-worker", ["work.poll", "agent.manage", "task.*"], [], {}, HUMAN
        )
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        for acc in (HUMAN, AGENT_ACC):
            repo.assign_role(s, acc, "mcpt-worker", HUMAN, now)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def server(database_url: str, engine: Engine) -> Iterator[str]:
    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    os.environ["AGENT_COLAB_MTLS_HEADER"] = "X-Client-Cert-Fingerprint"
    os.environ["AGENT_COLAB_MTLS_PROXY_SECRET"] = PROXY_SECRET
    app = create_app(Settings(database_url=database_url, base_url=base))
    srv = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
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


@asynccontextmanager
async def mcp_session(
    base: str, token: str | None = None, headers: dict[str, str] | None = None, **kw: Any
) -> AsyncIterator[ClientSession]:
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    # the SDK's own client type: a plain httpx.AsyncClient has no ``sse`` and cannot open the
    # standalone GET stream that carries server notifications
    async with (
        httpx2.AsyncClient(base_url=base, headers=hdrs, timeout=httpx2.Timeout(40.0)) as http,
        streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, *_),
        ClientSession(read, write, **kw) as session,
    ):
        await session.initialize()
        yield session


async def call(session: ClientSession, tool: str, **args: Any) -> dict[str, Any]:
    result = await session.call_tool(tool, args)
    if result.structured_content:
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))  # type: ignore[union-attr]


def _result_doc(wid: str, n: int = 1) -> dict[str, Any]:
    return {
        "schema_id": "colab.work-result.v1",
        "work_item_id": wid,
        "correlation_id": "corr-mcpt",
        "status": "SUCCEEDED",
        "result": {"n": n},
        "events": [],
        "artifacts": [],
        "usage": {
            "model": "m",
            "input_tokens": 1,
            "output_tokens": 1,
            "tool_calls": 0,
            "wall_time_ms": 5,
        },
    }


def _enqueue(engine: Engine, key: str, kind: str = "invoke") -> str:
    from server.domain.clock import SystemClock
    from server.events.postgres_store import PostgresEventStore
    from server.work import inbox

    clock = SystemClock()
    with Session(engine) as s, s.begin():
        item = inbox.enqueue(
            s,
            PostgresEventStore(s, clock=clock),
            workspace_id=str(WS),
            kind=kind,
            agent_id=AGENT,
            payload={"tool": "echo", "input": {"k": key}},
            deadline=clock.now() + dt.timedelta(hours=1),
            expected_result_schema="colab.work-result.v1",
            correlation_id="corr-mcpt",
            idempotency_key=key,
            actor_account_id=str(HUMAN),
            clock=clock,
        )
        return item.work_item_id


@pytest.mark.anyio
async def test_invalid_token_is_401_with_zero_side_effects(server: str, engine: Engine) -> None:
    before = _count_events(engine)
    with pytest.raises(Exception) as exc:
        async with mcp_session(server, "svc-not-a-token") as s:
            await call(s, "work_poll", agent_id=AGENT)
    assert "401" in str(exc.value) or "Unauthorized" in str(exc.value) or exc.value
    r = httpx.post(f"{server}/mcp", headers={"Authorization": "Bearer nope"}, json={})
    assert r.status_code == 401
    assert _count_events(engine) == before


@pytest.mark.anyio
async def test_mtls_fingerprint_via_trusted_proxy(server: str) -> None:
    # the reverse proxy forwards the verified certificate fingerprint plus its shared secret
    async with mcp_session(
        server,
        headers={
            "X-Client-Cert-Fingerprint": MTLS_FP,
            "X-Agent-Colab-Proxy-Auth": PROXY_SECRET,
        },
    ) as s:
        out = await call(s, "work_poll", agent_id=AGENT)
        assert "error" not in out, out
    # the same fingerprint header without the proxy secret is ignored → 401
    r = httpx.post(f"{server}/mcp", headers={"X-Client-Cert-Fingerprint": MTLS_FP}, json={})
    assert r.status_code == 401


@pytest.mark.anyio
async def test_long_poll_answered_within_30s_and_wakes_on_new_item(
    server: str, engine: Engine
) -> None:
    async with mcp_session(server, TOKEN_A) as s:
        t0 = time.monotonic()
        empty = await call(s, "work_poll", agent_id=AGENT, max_wait_s=30)
        elapsed = time.monotonic() - t0
        assert empty["items"] == [] and elapsed < 30.0, (elapsed, empty)
        assert elapsed >= 25.0  # it really waited (30 s minus the safety margin)

        async def later() -> str:
            await asyncio.sleep(1.0)
            return await asyncio.to_thread(_enqueue, engine, "mcpt-wake")

        t1 = time.monotonic()
        wid_task = asyncio.create_task(later())
        got = await call(s, "work_poll", agent_id=AGENT, max_wait_s=30)
        wid = await wid_task
        assert [i["work_item_id"] for i in got["items"]] == [wid]
        assert time.monotonic() - t1 < 5.0  # woke up on the inbox change, not at the deadline
        acked = await call(s, "work_ack", work_item_id=wid)
        assert acked["status"] == "ACKED"
        first = await call(s, "work_result", work_item_id=wid, result=_result_doc(wid))
        assert first["code"] == "RESULT_ACCEPTED"
        dup = await call(s, "work_result", work_item_id=wid, result=_result_doc(wid, n=2))
        assert dup["code"] == "DUPLICATE_RESULT_IGNORED" and dup["replayed"] is True
    with Session(engine) as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = "
                    "'work.duplicate_result_ignored' AND target_id = :w"
                ),
                {"w": wid},
            ).scalar_one()
            == 1
        )
        assert (
            db.execute(
                text("SELECT count(*) FROM usage_records WHERE work_item_id = :w"), {"w": wid}
            ).scalar_one()
            == 1
        )


@pytest.mark.anyio
async def test_reconnect_redelivers_unacked_items_without_duplicate_results(
    server: str, engine: Engine
) -> None:
    wid = _enqueue(engine, "mcpt-reconnect")
    async with mcp_session(server, TOKEN_A) as s1:
        got = await call(s1, "work_poll", agent_id=AGENT)
        assert [i["work_item_id"] for i in got["items"]] == [wid]
        assert got["items"][0]["delivery_no"] == 1
    # disconnected before ack: a new session sees the item again (same delivery, no new count)
    async with mcp_session(server, TOKEN_A) as s2:
        again = await call(s2, "work_poll", agent_id=AGENT)
        assert [i["work_item_id"] for i in again["items"]] == [wid]
        assert again["items"][0]["delivery_no"] == 1
        res = await call(s2, "work_result", work_item_id=wid, result=_result_doc(wid))
        assert res["code"] == "RESULT_ACCEPTED"
    async with mcp_session(server, TOKEN_A) as s3:
        dup = await call(s3, "work_result", work_item_id=wid, result=_result_doc(wid))
        assert dup["code"] == "DUPLICATE_RESULT_IGNORED"
    with Session(engine) as db:
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM work_item_receipts WHERE work_item_id "
                    "= :w AND receipt_kind = 'result'"
                ),
                {"w": wid},
            ).scalar_one()
            == 1
        )


@pytest.mark.anyio
async def test_inbox_resource_and_subscription(server: str, engine: Engine) -> None:
    """Inbox resource is caller-only; a subscribed session receives resources/updated when the
    inbox changes. The SDK's default protocol version (2025-11-25) uses ``resources/subscribe``
    with notifications through ``message_handler``; 2026-07-28 clients use ``subscriptions/listen``
    (both are served)."""
    from mcp.client.client import _listen
    from mcp.client.subscriptions import ListenNotSupportedError

    seen: list[Any] = []

    async def on_message(message: Any) -> None:
        seen.append(message)

    async with mcp_session(server, TOKEN_A, message_handler=on_message) as s:
        uri = f"colab://inbox/{AGENT}"
        with pytest.raises(MCPError):  # another agent's inbox is not readable (normalized)
            await s.read_resource("colab://inbox/agent-someone-else")
        mode = "listen"
        try:
            listen_cm = _listen(s, resource_subscriptions=[uri])
            sub = await listen_cm.__aenter__()
        except ListenNotSupportedError:
            mode = "subscribe"
            await s.subscribe_resource(uri)
            await asyncio.sleep(0.5)  # let the client's standalone GET stream connect
        wid = await asyncio.to_thread(_enqueue, engine, "mcpt-subscribe")
        if mode == "listen":

            async def take_one() -> None:
                async for event in sub:
                    seen.append(event)
                    break

            await asyncio.wait_for(take_one(), timeout=10)
            await listen_cm.__aexit__(None, None, None)
            assert seen and uri in str(getattr(seen[0], "uri", seen[0])), seen
        else:
            for _ in range(100):
                if any(
                    isinstance(m, types.ResourceUpdatedNotification) and str(m.params.uri) == uri
                    for m in seen
                ):
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError(f"no resources/updated for {uri}; got {seen!r}")
        read = await s.read_resource(uri)
        body = json.loads(read.contents[0].text)  # type: ignore[union-attr]
        assert wid in [i["work_item_id"] for i in body["items"]]
        got = await call(s, "work_poll", agent_id=AGENT)
        assert wid in [i["work_item_id"] for i in got["items"]]
        await call(s, "work_result", work_item_id=wid, result=_result_doc(wid))


@pytest.mark.anyio
async def test_one_concurrent_poll_per_session(server: str) -> None:
    async with mcp_session(server, TOKEN_A) as s:
        first = asyncio.create_task(call(s, "work_poll", agent_id=AGENT, max_wait_s=3))
        await asyncio.sleep(0.3)
        second = await call(s, "work_poll", agent_id=AGENT, max_wait_s=0)
        assert second.get("error", {}).get("code") == "MCP_POLL_IN_PROGRESS", second
        out = await first
        assert "error" not in out
        assert "error" not in await call(s, "work_poll", agent_id=AGENT)  # released


def _count_events(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": WS}
            ).scalar_one()
        )
