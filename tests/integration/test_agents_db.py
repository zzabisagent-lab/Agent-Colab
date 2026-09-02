"""P3-01 Agent Registry on the real database.

V-P3-01 register three adapter types with unique identity/status; V-P3-08 suspend/revoke block
new requests immediately; V-P3-11 heartbeat/offline timing (offline within 90 s, active/online
within 30 s of a returning heartbeat); V-P3-17 lifecycle rebuild reproduces state + hash.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.agents import registry as reg
from server.agents.adapters import contract
from server.api.dispatch import Runtime, execute_command
from server.api.errors import ApiError
from server.application import agents as ag
from server.application.authz import BusAuthorizer
from server.config import Settings
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import Principal, token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository
from server.usage.versions import activate_from_file

pytestmark = pytest.mark.db
WS = uuid.uuid4()
ADMIN, CHANNEL = uuid.uuid4(), uuid.uuid4()
TOK_ADMIN = "svc-agents-admin"
T0 = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.UTC)
ADMIN_P = Principal("acct-agents-admin", str(ADMIN), "human", "sha256:acct-agents-admin")
NO_METER = "ADAPTER_NO_METERING"  # §7C usage_unavailable reason code


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-agents', 'ag')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-agents-admin', :w, 'human', 'Admin')"
            ),
            {"i": ADMIN, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                "VALUES (:i, :a, 'sha256:acct-agents-admin', :h)"
            ),
            {"i": uuid.uuid4(), "a": ADMIN, "h": token_hash(TOK_ADMIN)},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-agents', :w, 'work', 'agents')"
            ),
            {"i": CHANNEL, "w": WS},
        )
        s.execute(
            text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
            {"c": CHANNEL, "a": ADMIN},
        )
        for cap in ("cap-agents-research", "cap-agents-code"):
            s.execute(
                text(
                    "INSERT INTO capabilities (id, capability_id, tool, domain) VALUES (:i, :c, "
                    "'task_progress', 'research') ON CONFLICT (capability_id) DO NOTHING"
                ),
                {"i": uuid.uuid4(), "c": cap},
            )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "role-agents-admin", "agents admin")
        repo.commit_role_version(
            s, "role-agents-admin", ["agent.manage", "admin.accounts", "task.*"], [], {}, ADMIN
        )
        repo.assign_role(s, ADMIN, "role-agents-admin", ADMIN, T0)
        repo.create_role(s, WS, "role-agents-worker", "agent worker")
        repo.commit_role_version(
            s, "role-agents-worker", ["agent.self", "task.*", "work.poll"], [], {}, ADMIN
        )
    with Session(eng) as s, s.begin():
        activate_from_file(s)  # §7C usage recording needs an active pricing version
    yield eng
    eng.dispose()


class _FakeAdapter:
    adapter_type = "mcp"

    def __init__(self, endpoint: dict[str, Any]) -> None:
        self.endpoint = endpoint

    def probe(self) -> contract.Probe:
        return contract.Probe(
            agent_id=str(self.endpoint.get("agent_id")),
            adapter_type="mcp",
            runtime={"product": "fake"},
            capabilities=("cap-agents-research",),
            unsupported=("browser",),
            delivery_modes=(contract.DeliveryMode.PULL,),
            limits={},
            secret_handles="supported",
            identity_hash="id-" + str(self.endpoint.get("agent_id")),
        )


@pytest.fixture(scope="module", autouse=True)
def _fake_adapter_type() -> Iterator[None]:
    previous = contract._TYPES.get("mcp")
    contract.register_adapter_type("mcp", _FakeAdapter, replace=True)
    yield
    if previous is not None:
        contract.register_adapter_type("mcp", previous, replace=True)
    else:
        contract._TYPES.pop("mcp", None)


def _rt(engine: Engine, clock: FixedClock) -> Runtime:
    return Runtime(make_session_factory(engine), BusAuthorizer(), None, clock, str(WS))


def _run(rt: Runtime, principal: Principal, cmd: Any, key: str) -> Any:
    return execute_command(rt, principal, cmd, idempotency_key=key, correlation_id="corr-agents")


def _register(rt: Runtime, agent_id: str, adapter_type: str, **extra: Any) -> Any:
    return _run(
        rt,
        ADMIN_P,
        ag.RegisterAgent(
            agent_id=agent_id,
            display_name=f"Agent {agent_id}",
            adapter_type=adapter_type,
            endpoint={"url": f"https://{agent_id}.example/hook", "signing_key_ref": "sec-k"},
            credential_ref="sec-cred-1",
            owner_account_id="acct-agents-admin",
            roles=("role-agents-worker",),
            capabilities=("cap-agents-research",),
            channel_ids=("chan-agents",),
            **extra,
        ),
        f"reg-{agent_id}",
    )


def _agent_principal(engine: Engine, agent_id: str) -> Principal:
    with Session(engine) as s:
        row = reg.load_agent(s, WS, agent_id)
        assert row is not None
        return Principal(
            row.account_public_id, str(row.account_id), "agent", f"sha256:{row.account_public_id}"
        )


def test_register_three_adapter_types_with_unique_identity(engine: Engine) -> None:
    """V-P3-01."""
    rt = _rt(engine, FixedClock(T0))
    results = [
        _register(rt, "agent-reg-mcp", "mcp"),
        _register(rt, "agent-reg-webhook", "webhook"),
        _register(rt, "agent-reg-bot", "mattermost_bot", delivery_modes=("push",)),
    ]
    assert [r.data["status"] for r in results] == ["pending"] * 3
    assert len({r.data["account_id"] for r in results}) == 3
    tokens = {r.data["service_token"] for r in results}
    assert len(tokens) == 3 and all(tok.startswith("svc-") for tok in tokens)
    with Session(engine) as s:
        rows = reg.list_agents(s, WS)
        assert [r.agent_id for r in rows] == ["agent-reg-bot", "agent-reg-mcp", "agent-reg-webhook"]
        assert [r.adapter_type for r in rows] == ["mattermost_bot", "mcp", "webhook"]
        assert all(r.credential_ref == "sec-cred-1" for r in rows)
        # the token itself is never stored, only its hash; the endpoint holds references only
        stored = s.execute(
            text(
                "SELECT token_hash FROM service_credentials sc JOIN agents a ON a.account_id = "
                "sc.account_id WHERE a.workspace_id = :w"
            ),
            {"w": WS},
        ).all()
        assert all(tok not in str(stored) for tok in tokens)
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM channel_members cm JOIN agents a ON "
                    "a.account_id = cm.account_id "
                    "WHERE cm.channel_id = :c"
                ),
                {"c": CHANNEL},
            ).scalar_one()
            == 3
        )
        assert (
            s.execute(
                text("SELECT count(*) FROM agent_capabilities WHERE agent_id LIKE 'agent-reg-%'")
            ).scalar_one()
            == 3
        )
    # idempotent replay returns the same Event; a different key for the same id is a conflict
    again = _register(rt, "agent-reg-mcp", "mcp")
    assert again.replayed and again.event_id == results[0].event_id
    with pytest.raises(ApiError) as exc:
        _run(rt, ADMIN_P, ag.RegisterAgent("agent-reg-mcp", "dup", "mcp"), "reg-dup")
    assert exc.value.code == "AGENT_ALREADY_EXISTS"
    # secret values in the endpoint are rejected before anything is written
    with pytest.raises(ApiError) as exc2:
        _run(
            rt,
            ADMIN_P,
            ag.RegisterAgent("agent-reg-bad", "bad", "webhook", endpoint={"api_key": "plain"}),
            "reg-bad",
        )
    assert exc2.value.code == "AGENT_ENDPOINT_SECRET_VALUE"
    with Session(engine) as s:
        assert (
            s.execute(text("SELECT 1 FROM agents WHERE agent_id = 'agent-reg-bad'")).first() is None
        )


def test_connection_test_activate_update_and_lifecycle(engine: Engine) -> None:
    clock = FixedClock(T0 + dt.timedelta(minutes=1))
    rt = _rt(engine, clock)
    # activation needs a passing connection test first
    with pytest.raises(ApiError) as exc:
        _run(rt, ADMIN_P, ag.ActivateAgent("agent-reg-mcp"), "act-early")
    assert exc.value.code == "AGENT_CONNECTION_TEST_REQUIRED"
    probe = _run(rt, ADMIN_P, ag.TestAgentConnection("agent-reg-mcp"), "probe-1")
    assert probe.data["ok"] and probe.data["probe"]["identity_hash"] == "id-agent-reg-mcp"
    act = _run(rt, ADMIN_P, ag.ActivateAgent("agent-reg-mcp"), "act-1")
    assert act.data["status"] == "active" and not act.data["online"]
    upd = _run(
        rt, ADMIN_P, ag.UpdateAgent("agent-reg-mcp", limits={"concurrent_tasks": 2}), "upd-1"
    )
    assert upd.data["changed_fields"] == ["limits"]
    with pytest.raises(ApiError) as exc2:
        _run(rt, ADMIN_P, ag.UpdateAgent("agent-reg-mcp", endpoint={"password": "x"}), "upd-bad")
    assert exc2.value.code == "AGENT_ENDPOINT_SECRET_VALUE"
    with Session(engine) as s:
        row = reg.load_agent(s, WS, "agent-reg-mcp")
        assert row is not None and row.limits == {"concurrent_tasks": 2}
        hist = reg.lifecycle_history(rt.store_for(s), str(WS), "agent-reg-mcp")
    assert [h["type"] for h in hist.history] == [
        "AGENT_REGISTERED",
        "AGENT_ACTIVATED",
        "AGENT_UPDATED",
    ]
    assert hist.lifecycle_hash == row.lifecycle_hash


def test_heartbeat_offline_and_returning_heartbeat_timing(engine: Engine) -> None:
    """V-P3-11: offline within 90 s of silence; active/online within 30 s of the returning beat."""
    clock = FixedClock(T0 + dt.timedelta(minutes=5))
    rt = _rt(engine, clock)
    agent = _agent_principal(engine, "agent-reg-mcp")
    with pytest.raises(ApiError) as exc:  # §7C: usage or a reason is mandatory
        _run(rt, agent, ag.RecordHeartbeat("agent-reg-mcp"), "hb-0")
    assert exc.value.code == "USAGE_REQUIRED"
    hb = _run(
        rt,
        agent,
        ag.RecordHeartbeat("agent-reg-mcp", capacity=2, usage_unavailable=NO_METER),
        "hb-1",
    )
    assert hb.data["online"] and hb.data["status"] == "active" and hb.data["returning"]
    for i in range(2):  # 30-second cadence keeps the Agent online
        clock.advance(dt.timedelta(seconds=30))
        _run(
            rt,
            agent,
            ag.RecordHeartbeat(
                "agent-reg-mcp",
                usage={
                    "model": "m",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "tool_calls": 0,
                    "wall_time_ms": 10,
                },
            ),
            f"hb-2{i}",
        )
    clock.advance(dt.timedelta(seconds=89))  # two misses: still online
    assert _run(rt, ADMIN_P, ag.SweepOffline(), "sweep-1").data["marked_offline"] == []
    with Session(engine) as s:
        row = reg.load_agent(s, WS, "agent-reg-mcp")
        assert row is not None and row.online and row.missed_heartbeats == 2
    clock.advance(dt.timedelta(seconds=1))  # 90 s without heartbeat → offline
    assert _run(rt, ADMIN_P, ag.SweepOffline(), "sweep-2").data["marked_offline"] == [
        "agent-reg-mcp"
    ]
    with Session(engine) as s:
        row = reg.load_agent(s, WS, "agent-reg-mcp")
        assert row is not None and not row.online and row.status == "offline"
    clock.advance(dt.timedelta(seconds=20))
    back = _run(
        rt,
        agent,
        ag.RecordHeartbeat(
            "agent-reg-mcp", capabilities=("cap-agents-research",), usage_unavailable=NO_METER
        ),
        "hb-back",
    )
    assert back.data["returning"] and back.data["status"] == "active" and back.data["online"]
    with Session(engine) as s:
        row = reg.load_agent(s, WS, "agent-reg-mcp")
        assert row is not None and row.capabilities_snapshot["capabilities"] == [
            "cap-agents-research"
        ]
        assert (
            s.execute(
                text("SELECT count(*) FROM agent_heartbeats WHERE agent_id = 'agent-reg-mcp'")
            ).scalar_one()
            == 4
        )
    # a heartbeat by another Agent's principal for this Agent is a policy denial
    _register(rt, "agent-reg-other", "mcp")
    other = _agent_principal(engine, "agent-reg-other")
    with pytest.raises(ApiError) as exc2:
        _run(rt, other, ag.RecordHeartbeat("agent-reg-mcp", usage_unavailable=NO_METER), "hb-spoof")
    assert exc2.value.status in (403, 404)


def test_suspend_and_revoke_block_new_requests_immediately(engine: Engine) -> None:
    """V-P3-08."""
    clock = FixedClock(T0 + dt.timedelta(minutes=30))
    rt = _rt(engine, clock)
    agent = _agent_principal(engine, "agent-reg-mcp")
    sus = _run(rt, ADMIN_P, ag.SuspendAgent("agent-reg-mcp", "SECURITY_REVIEW"), "sus-1")
    assert sus.data["status"] == "suspended" and not sus.data["online"]
    with pytest.raises(ApiError) as exc:  # the very next request by the Agent is denied
        _run(rt, agent, ag.RecordHeartbeat("agent-reg-mcp", usage_unavailable=NO_METER), "hb-sus")
    assert exc.value.status in (403, 404)
    with Session(engine) as s:
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = "
                    "'policy.deny' AND actor_label = :a"
                ),
                {"a": agent.account_id},
            ).scalar_one()
            >= 1
        )
        assert (
            s.execute(
                text("SELECT status FROM accounts WHERE id = :a"),
                {"a": uuid.UUID(agent.account_uuid)},
            ).scalar_one()
            == "SUSPENDED"
        )
    # reactivation restores service (stored probe is enough)
    act = _run(rt, ADMIN_P, ag.ActivateAgent("agent-reg-mcp"), "act-2")
    assert act.data["status"] == "active"
    _run(rt, agent, ag.RecordHeartbeat("agent-reg-mcp", usage_unavailable=NO_METER), "hb-after")
    rev = _run(
        rt, ADMIN_P, ag.RevokeAgent("agent-reg-mcp", "KEY_LEAK", security_revoke=True), "rev-1"
    )
    assert rev.data["status"] == "revoked"
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT status FROM service_credentials WHERE account_id = :a"),
                {"a": uuid.UUID(agent.account_uuid)},
            ).scalar_one()
            == "revoked"
        )
    with pytest.raises(ApiError):
        _run(rt, agent, ag.RecordHeartbeat("agent-reg-mcp", usage_unavailable=NO_METER), "hb-rev")
    for cmd, key in (
        (ag.ActivateAgent("agent-reg-mcp"), "act-3"),
        (ag.UpdateAgent("agent-reg-mcp", display_name="x"), "upd-3"),
    ):
        with pytest.raises(ApiError) as exc2:
            _run(rt, ADMIN_P, cmd, key)
        assert exc2.value.code == "AGENT_REVOKED"


def test_lifecycle_rebuild_reproduces_state_and_hash(engine: Engine) -> None:
    """V-P3-17."""
    clock = FixedClock(T0 + dt.timedelta(hours=1))
    rt = _rt(engine, clock)
    with Session(engine) as s:
        before = reg.state_hash(s, WS)
        rows = {r.agent_id: r for r in reg.list_agents(s, WS)}
    assert rows["agent-reg-mcp"].status == "revoked"
    with Session(engine) as s, s.begin():  # corrupt the projection columns on purpose
        s.execute(
            text(
                "UPDATE agents SET status = 'pending', online = true, "
                "lifecycle_hash = 'x', missed_heartbeats = 9 WHERE workspace_id = :w"
            ),
            {"w": WS},
        )
    with Session(engine) as s, s.begin():
        after = reg.rebuild(s, rt.store_for(s), str(WS), clock.now())
    assert after == before
    with Session(engine) as s:
        rebuilt = {r.agent_id: r for r in reg.list_agents(s, WS)}
    for agent_id, row in rows.items():
        assert (
            rebuilt[agent_id].status,
            rebuilt[agent_id].online,
            rebuilt[agent_id].lifecycle_hash,
        ) == (row.status, row.online, row.lifecycle_hash)
        hist = reg.lifecycle_history(rt.store_for(Session(engine)), str(WS), agent_id)
        assert hist.lifecycle_hash == row.lifecycle_hash


def test_rest_registration_and_lifecycle_endpoints(database_url: str, engine: Engine) -> None:
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    app = create_app(Settings(database_url=database_url, base_url="http://test"))
    headers = {"Authorization": f"Bearer {TOK_ADMIN}", "Idempotency-Key": "rest-reg-1"}
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/agents",
            json={
                "agent_id": "agent-rest-1",
                "display_name": "REST",
                "adapter_type": "webhook",
                "endpoint": {"url": "https://x.example", "signing_key_ref": "sec-k"},
                "limits": {"requests_per_minute": 5},
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text
        assert r.json()["service_token"].startswith("svc-")
        bad = client.post(
            "/api/v1/agents",
            json={
                "agent_id": "agent-rest-2",
                "display_name": "x",
                "adapter_type": "webhook",
                "endpoint": {"token": "v"},
            },
            headers={**headers, "Idempotency-Key": "rest-reg-2"},
        )
        assert bad.status_code == 400 and bad.json()["code"] == "AGENT_ENDPOINT_SECRET_VALUE"
        listing = client.get("/api/v1/agents", headers=headers).json()["items"]
        assert any(a["agent_id"] == "agent-rest-1" and a["status"] == "pending" for a in listing)
        assert all("service_token" not in a for a in listing)
        one = client.get("/api/v1/agents/agent-rest-1", headers=headers).json()
        assert (
            one["limits"] == {"requests_per_minute": 5}
            and one["limit_counters"]["requests_per_minute"] == 0
        )
        life = client.get("/api/v1/agents/agent-rest-1/lifecycle", headers=headers).json()
        assert [h["type"] for h in life["history"]] == ["AGENT_REGISTERED"] and life[
            "lifecycle_hash"
        ]
        assert client.get("/api/v1/agents/agent-missing", headers=headers).status_code == 404
