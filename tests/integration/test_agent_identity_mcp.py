"""V-P2-28 through the production path (F-P2-003): an Agent utterance originates as an MCP
``task_progress`` call, becomes a card thread reply in the outbox and is delivered by the real
Mattermost delivery provider. Override mode sets the exact display name, prefix mode falls back to
``[agent-name] ``; identity fields injected in the MCP tool input are ignored and audited."""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.channels.mattermost.client import FakeMattermostClient
from server.channels.mattermost.delivery import MattermostChannelProvider
from server.channels.mattermost.provider import load_instance
from server.channels.outbox import drain_channels
from server.channels.task_cards import bind_delivered_cards
from server.config import Settings
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.identity.principals import token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db
WS, HUMAN, AGENT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
TOKEN_H, TOKEN_A = "svc-idm-human", "svc-idm-agent"
AGENT_NAME = "Research Agent"
CHANNEL_UUIDS: dict[str, uuid.UUID] = {}
MODES = {
    "override": ("mm:idm:override", "chan-idm-o", "ext-o"),
    "prefix": ("mm:idm:prefix", "chan-idm-p", "ext-p"),
}


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-idm', 'idm')"),
            {"i": WS},
        )
        for acc, name, typ, tok, disp in (
            (HUMAN, "acct-idm-h", "human", TOKEN_H, "Helen"),
            (AGENT, "acct-idm-a", "agent", TOKEN_A, AGENT_NAME),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, "
                    "account_type, display_name) "
                    "VALUES (:i, :a, :w, :t, :d)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ, "d": disp},
            )
            s.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                    "VALUES (:i, :a, :f, :h)"
                ),
                {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{name}", "h": token_hash(tok)},
            )
        for mode, (pi, chan, ext) in MODES.items():
            pi_uuid = uuid.uuid4()
            s.execute(
                text(
                    "INSERT INTO provider_instances (id, provider_instance_id, "
                    "workspace_id, provider, "
                    "base_url, team_or_bot_ref, identity_display) "
                    "VALUES (:i, :p, :w, 'mattermost', 'http://mm', 'team-idm', :m)"
                ),
                {"i": pi_uuid, "p": pi, "w": WS, "m": mode},
            )
            chan_uuid = uuid.uuid4()
            CHANNEL_UUIDS[mode] = chan_uuid
            s.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, "
                    "channel_type, display_name, "
                    "provider_instance_id, external_channel_id) "
                    "VALUES (:i, :c, :w, 'work', :c, :p, :e)"
                ),
                {"i": chan_uuid, "c": chan, "w": WS, "p": pi_uuid, "e": ext},
            )
            for acc in (HUMAN, AGENT):
                s.execute(
                    text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                    {"c": chan_uuid, "a": acc},
                )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "idm-worker", "identity worker")
        repo.commit_role_version(
            s, "idm-worker", ["task.*", "artifact.*", "work.poll"], [], {}, HUMAN
        )
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        for acc in (HUMAN, AGENT):
            repo.assign_role(s, acc, "idm-worker", HUMAN, now)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def server(database_url: str, engine: Engine) -> Iterator[str]:
    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"  # the test drains the outbox itself
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
    return dict(json.loads(result.content[0].text))  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_mcp_agent_utterance_display_identity_and_injection(
    server: str, engine: Engine
) -> None:
    posts_by_mode: dict[str, list[Any]] = {}
    async with mcp_session(server, TOKEN_H) as human, mcp_session(server, TOKEN_A) as agent:
        for mode, (pi, _chan, _ext) in MODES.items():
            created = await call(
                human,
                "task_create",
                title=f"identity {mode}",
                channel_id=str(CHANNEL_UUIDS[mode]),
                domain="research",
                risk="LOW",
                criteria=[
                    {"statement": "report attached", "check_type": "evidence", "required": True}
                ],
                idempotency_key=f"idm-create-{mode}",
            )
            assert "error" not in created, created
            task_id = created["resource_id"]
            delegated = await call(
                human,
                "task_delegate",
                task_id=task_id,
                assignee_account_id="acct-idm-a",
                idempotency_key=f"idm-delegate-{mode}",
            )
            assert "error" not in delegated, delegated
            accepted = await call(
                agent, "task_accept", task_id=task_id, idempotency_key=f"idm-accept-{mode}"
            )
            assert "error" not in accepted, accepted
            started = await call(
                agent, "task_start", task_id=task_id, idempotency_key=f"idm-start-{mode}"
            )
            assert "error" not in started, started
            # the Agent utterance, with identity fields injected in the tool input
            progress = await call(
                agent,
                "task_progress",
                task_id=task_id,
                summary=f"step one ({mode})",
                override_username="evil-admin",
                display_name="Site Admin",
                idempotency_key=f"idm-progress-{mode}",
            )
            assert "error" not in progress, progress
            assert progress[
                "event_id"
            ]  # the utterance itself is not blocked, only its identity claims
            # deliver through the real Mattermost delivery provider (fake HTTP client)
            fake = FakeMattermostClient()
            with Session(engine) as s, s.begin():
                instance = load_instance(s, pi)
                assert instance is not None and instance.identity_display == mode
                provider = MattermostChannelProvider(fake, instance)
                clock = FixedClock(dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1))
                for _ in range(3):  # card first, then the thread reply once the root is bound
                    drain_channels(s, {"mattermost": provider}, clock, str(WS))
                    bind_delivered_cards(s)
            posts_by_mode[mode] = list(fake.posts.values())
    for mode, posts in posts_by_mode.items():
        replies = [p for p in posts if "step one" in p.message]
        with Session(engine) as dbg:
            outbox = dbg.execute(
                text(
                    "SELECT kind, status, dedupe_key, last_error, "
                    "next_attempt_at, payload->>'root_id' "
                    "FROM delivery_outbox WHERE workspace_id = :w ORDER BY id"
                ),
                {"w": WS},
            ).all()
        if len(replies) != 1:  # diagnostics for the verifier: full outbox state of this workspace
            dump = Path(os.environ.get("AGENT_COLAB_TEST_DUMP", "/tmp")) / "identity-outbox.txt"
            dump.write_text(
                "\n".join(repr(r) for r in outbox) + "\n" + repr([p.message[:40] for p in posts])
            )
        assert len(replies) == 1, ([p.message[:60] for p in posts], len(outbox))
        reply = replies[0]
        if mode == "override":
            assert reply.props["override_username"] == AGENT_NAME  # exact server-known name
            assert not reply.message.startswith("[")
        else:
            assert reply.message.startswith(f"[{AGENT_NAME}] ")  # prefix fallback
            assert "override_username" not in (reply.props or {})
        for p in posts:  # injected values never reach the channel, on any post
            assert "evil-admin" not in str(p.props) and "Site Admin" not in p.message
            assert (p.props or {}).get("override_username") in (None, AGENT_NAME)
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT redacted_metadata->>'removed_keys' FROM audit_events WHERE action = "
                "'agent.identity_injection_ignored' AND actor_label = "
                "'acct-idm-a' AND workspace_id = :w"
            ),
            {"w": WS},
        ).all()
    # one audit entry per injected call (metadata values are redacted by the audit layer)
    assert len(rows) >= 2
