"""V-P3-22 / V-P3-06 (P3-11): webhook push through the outbox — endpoint 5xx retried per
backoff, exactly one side effect per receipt, redelivery after the ack timeout; signed inbound
callbacks — tampered signature, reused nonce, stale timestamp, body-hash mismatch rejected;
service-token routes; duplicate results ignored and audited."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.agents import webhook_signing as ws
from server.agents.adapters.contract import Adapter
from server.agents.adapters.webhook import WebhookAdapter
from server.agents.signing_keys import StaticSigningKeyResolver, set_default_resolver
from server.agents.webhook_delivery import WebhookDeliveryChannel, drain_webhooks
from server.config import Settings
from server.db.engine import make_engine
from server.domain.clock import FixedClock, SystemClock
from server.events.postgres_store import PostgresEventStore
from server.identity.principals import token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository
from server.work import inbox, timeouts
from server.work.state import WorkItemState

pytestmark = pytest.mark.db
WS, SERVICE, AGENT_ACC, OTHER_ACC = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
AGENT, OTHER = "agent-hook-a", "agent-hook-b"
TOKEN_A, TOKEN_B = "svc-hook-agent-a", "svc-hook-agent-b"
REF = "sec-hook-a@v1"
KEY = b"0123456789abcdef0123456789abcdef"
T0 = dt.datetime(2026, 6, 1, 8, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-hook', 'hook')"),
            {"i": WS},
        )
        for acc, name, typ, tok in (
            (SERVICE, "acct-hook-service", "service", None),
            (AGENT_ACC, "acct-hook-a", "agent", TOKEN_A),
            (OTHER_ACC, "acct-hook-b", "agent", TOKEN_B),
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
                        "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                        "VALUES (:i, :a, :f, :h)"
                    ),
                    {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{name}", "h": token_hash(tok)},
                )
        for agent, acc in ((AGENT, AGENT_ACC), (OTHER, OTHER_ACC)):
            s.execute(
                text(
                    "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, "
                    "status, display_name, endpoint, credential_ref, delivery_modes) VALUES "
                    "(:i, :g, :w, :a, 'webhook', 'active', :g, CAST(:e AS jsonb), :r, "
                    "'[\"push\"]')"
                ),
                {
                    "i": uuid.uuid4(),
                    "g": agent,
                    "w": WS,
                    "a": acc,
                    "e": json.dumps({"url": f"https://{agent}.example.test/colab"}),
                    "r": REF,
                },
            )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "hook-agent", "webhook agent")
        repo.commit_role_version(s, "hook-agent", ["work.poll", "task.read"], [], {}, SERVICE)
        for acc in (AGENT_ACC, OTHER_ACC):
            repo.assign_role(s, acc, "hook-agent", SERVICE, T0)
    set_default_resolver(StaticSigningKeyResolver({REF: KEY}))
    yield eng
    set_default_resolver(None)
    eng.dispose()


class Endpoint:
    """Fake Agent endpoint: verifies signatures, idempotent per work item, injectable 5xx."""

    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.nonces = ws.InMemoryNonceStore()
        self.posts: list[dict[str, Any]] = []
        self.side_effects: dict[str, int] = {}
        self.fail_times = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        headers = {k: v for k, v in request.headers.items()}
        try:
            ws.verify(
                KEY,
                {
                    ws.HEADER_TIMESTAMP: headers.get("x-colab-timestamp", ""),
                    ws.HEADER_NONCE: headers.get("x-colab-nonce", ""),
                    ws.HEADER_SIGNATURE: headers.get("x-colab-signature", ""),
                },
                request.content,
                self.clock,
                self.nonces,
            )
        except ws.WebhookError as exc:
            return httpx.Response(401, json={"error": exc.code})
        body = json.loads(request.content)
        self.posts.append(body)
        if self.fail_times > 0:
            self.fail_times -= 1
            return httpx.Response(503, json={"error": "down"})
        wid = body["work_item_id"]
        self.side_effects.setdefault(wid, 0)
        if self.side_effects[wid] == 0:
            self.side_effects[wid] = 1  # the single side effect per work item
        return httpx.Response(
            202,
            json={
                "schema_id": "colab.delivery-receipt.v1",
                "work_item_id": wid,
                "correlation_id": body["correlation_id"],
                "accepted_at": "2026-06-01T08:00:01.000Z",
            },
        )


def _enqueue(s: Session, clock: FixedClock, key: str, agent: str = AGENT) -> inbox.WorkItem:
    return inbox.enqueue(
        s,
        PostgresEventStore(s, clock=clock),
        workspace_id=str(WS),
        kind="task_assignment",
        agent_id=agent,
        payload={"title": "hook"},
        deadline=clock.now() + dt.timedelta(hours=4),
        expected_result_schema="colab.work-result.v1",
        correlation_id=f"corr-{key}",
        idempotency_key=key,
        actor_account_id=str(SERVICE),
        clock=clock,
        task_id="task-hook-1",
    )


def _drain(s: Session, clock: FixedClock, endpoint: Endpoint) -> Any:
    def factory(agent_id: str, endpoint_cfg: dict[str, Any]) -> Adapter:
        return WebhookAdapter(
            {**endpoint_cfg, "agent_id": agent_id},
            clock=clock,
            transport=httpx.MockTransport(endpoint.handler),
        )

    return drain_webhooks(
        s,
        PostgresEventStore(s, clock=clock),
        clock,
        str(WS),
        actor_account_id=str(SERVICE),
        adapter_factory=factory,
    )


def test_push_retries_per_backoff_with_one_side_effect_per_receipt(engine: Engine) -> None:
    clock = FixedClock(T0)
    endpoint = Endpoint(clock)
    endpoint.fail_times = 2
    channel = WebhookDeliveryChannel(actor_account_id=str(SERVICE))
    with Session(engine) as s, s.begin():
        item = _enqueue(s, clock, "hook-push-1")
        assert channel.deliver(s, PostgresEventStore(s, clock=clock), [item], clock=clock)
        r1 = _drain(s, clock, endpoint)  # 503 → retry in 1 s
        assert r1.failed == 1 and inbox.load(s, item.work_item_id).status is WorkItemState.QUEUED
        clock.advance(dt.timedelta(seconds=2))
        r2 = _drain(s, clock, endpoint)  # 503 → retry in 5 s
        assert r2.failed == 1
        clock.advance(dt.timedelta(seconds=3))
        assert _drain(s, clock, endpoint).sent == 0  # backoff not elapsed: no POST
        clock.advance(dt.timedelta(seconds=3))
        r3 = _drain(s, clock, endpoint)
        assert r3.sent == 1
        loaded = inbox.load(s, item.work_item_id)
        assert loaded.status is WorkItemState.DELIVERED and loaded.delivery_count == 1
        assert len(endpoint.posts) == 3 and endpoint.side_effects[item.work_item_id] == 1
        for _ in range(3):  # further drains: nothing to send, nothing re-posted
            assert _drain(s, clock, endpoint).sent == 0
        assert len(endpoint.posts) == 3
        receipts = s.execute(
            text(
                "SELECT receipt_kind, delivery_no, detail->>'receipt_id' FROM work_item_receipts "
                "WHERE work_item_id = :w ORDER BY id"
            ),
            {"w": item.work_item_id},
        ).all()
        assert [r[0] for r in receipts] == ["delivery"] and receipts[0][2]
        # no ACK within 60 s → the sweep re-queues; the channel pushes generation 2 exactly once
        clock.advance(dt.timedelta(seconds=61))
        timeouts.sweep(
            s,
            PostgresEventStore(s, clock=clock),
            clock=clock,
            actor_account_id=str(SERVICE),
            agent_id=AGENT,
        )
        requeued = inbox.load(s, item.work_item_id)
        assert requeued.status is WorkItemState.QUEUED and requeued.delivery_count == 1
        channel.deliver(s, PostgresEventStore(s, clock=clock), [requeued], clock=clock)
        assert _drain(s, clock, endpoint).sent == 1
        assert inbox.load(s, item.work_item_id).delivery_count == 2
        assert len(endpoint.posts) == 4 and endpoint.side_effects[item.work_item_id] == 1
        events = s.execute(
            text(
                "SELECT count(*) FROM events WHERE aggregate_id = :w AND type = "
                "'WORK_ITEM_DELIVERED'"
            ),
            {"w": item.work_item_id},
        ).scalar_one()
        assert events == 2
        # the signing key never lands in the database
        assert (
            s.execute(
                text("SELECT count(*) FROM delivery_outbox WHERE payload::text LIKE :k"),
                {"k": f"%{KEY.decode()}%"},
            ).scalar_one()
            == 0
        )


@pytest.fixture(scope="module")
def client(database_url: str, engine: Engine) -> Iterator[TestClient]:
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    app = create_app(Settings(database_url=database_url, base_url="http://testserver"))
    with TestClient(app) as tc:
        yield tc


def _signed(
    body: dict[str, Any],
    *,
    key: bytes = KEY,
    nonce: str | None = None,
    clock: Any = None,
    key_ref: str = REF,
) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    headers = ws.sign(key, raw, clock or SystemClock(), key_ref=key_ref, nonce=nonce)
    return raw, {**headers, "Content-Type": "application/json"}


def test_signed_callbacks_and_service_token_routes(engine: Engine, client: TestClient) -> None:
    clock = FixedClock(dt.datetime.now(dt.UTC))
    with Session(engine) as s, s.begin():
        item = _enqueue(s, clock, "hook-cb-1")
        WebhookDeliveryChannel(actor_account_id=str(SERVICE)).deliver(
            s, PostgresEventStore(s, clock=clock), [item], clock=clock
        )
        _drain(s, clock, Endpoint(clock))
    wid = item.work_item_id
    url = f"/api/v1/agents/{AGENT}/webhook/callbacks"
    # ack by signed callback
    raw, headers = _signed({"op": "ack", "work_item_id": wid})
    r = client.post(url, content=raw, headers=headers)
    assert r.status_code == 200, r.text
    # tampered signature
    raw, headers = _signed({"op": "ack", "work_item_id": wid}, key=b"wrong" * 8)
    assert (
        client.post(url, content=raw, headers=headers).json()["code"] == "WEBHOOK_SIGNATURE_INVALID"
    )
    # stale timestamp (10 minutes old)
    raw, headers = _signed(
        {"op": "ack", "work_item_id": wid},
        clock=FixedClock(dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)),
    )
    assert (
        client.post(url, content=raw, headers=headers).json()["code"] == "WEBHOOK_TIMESTAMP_EXPIRED"
    )
    # body hash claim mismatch
    raw, headers = _signed({"op": "ack", "work_item_id": wid})
    headers["X-Colab-Body-Sha256"] = "0" * 64
    assert (
        client.post(url, content=raw, headers=headers).json()["code"]
        == "WEBHOOK_BODY_HASH_MISMATCH"
    )
    # unknown key reference
    raw, headers = _signed({"op": "ack", "work_item_id": wid}, key_ref="sec-other")
    assert client.post(url, content=raw, headers=headers).status_code == 401
    # result by signed callback, then the exact same request replayed (nonce reuse)
    result_doc = {
        "schema_id": "colab.work-result.v1",
        "work_item_id": wid,
        "correlation_id": "corr-hook-cb-1",
        "status": "SUCCEEDED",
        "result": {"ok": True},
        "events": [],
        "artifacts": [],
        "usage": {
            "model": "m",
            "input_tokens": 1,
            "output_tokens": 1,
            "tool_calls": 0,
            "wall_time_ms": 1,
        },
    }
    raw, headers = _signed({"op": "result", "work_item_id": wid, "result": result_doc})
    first = client.post(url, content=raw, headers=headers)
    assert first.status_code == 200 and first.json()["code"] == "RESULT_ACCEPTED", first.text
    replay = client.post(url, content=raw, headers=headers)
    assert replay.status_code == 401 and replay.json()["code"] == "WEBHOOK_NONCE_REUSED"
    # a second result over the service-token route is ignored and audited
    auth = {"Authorization": f"Bearer {TOKEN_A}", "Idempotency-Key": "hook-res-2"}
    dup = client.post(
        f"/api/v1/work/{wid}/result", json={**result_doc, "result": {"ok": 2}}, headers=auth
    )
    assert dup.status_code == 200 and dup.json()["code"] == "DUPLICATE_RESULT_IGNORED", dup.text
    with Session(engine) as s:
        kinds = [
            r[0]
            for r in s.execute(
                text(
                    "SELECT receipt_kind FROM work_item_receipts WHERE "
                    "work_item_id = :w ORDER BY id"
                ),
                {"w": wid},
            ).all()
        ]
        assert kinds.count("result") == 1 and "duplicate_result" in kinds
        assert (
            s.execute(
                text("SELECT count(*) FROM webhook_nonces WHERE agent_id = :a"), {"a": AGENT}
            ).scalar_one()
            == 2  # only verified callbacks (ack, result) consume a nonce
        )
    # owner read, non-owner normalized 404, reject route with a stable code
    me = client.get(f"/api/v1/work/{wid}", headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert me.status_code == 200 and me.json()["status"] == "RESULT_RECEIVED"
    other = client.get(f"/api/v1/work/{wid}", headers={"Authorization": f"Bearer {TOKEN_B}"})
    assert other.status_code == 404
    with Session(engine) as s, s.begin():
        item2 = _enqueue(s, clock, "hook-cb-2")
        WebhookDeliveryChannel(actor_account_id=str(SERVICE)).deliver(
            s, PostgresEventStore(s, clock=clock), [item2], clock=clock
        )
        _drain(s, clock, Endpoint(clock))
    rej = client.post(
        f"/api/v1/work/{item2.work_item_id}/reject",
        json={"reason_code": "CAPACITY"},
        headers={"Authorization": f"Bearer {TOKEN_A}", "Idempotency-Key": "hook-rej-1"},
    )
    assert rej.status_code == 200 and rej.json()["status"] == "REJECTED", rej.text
    bad = client.post(
        f"/api/v1/work/{item2.work_item_id}/reject",
        json={"reason_code": "NOPE"},
        headers={"Authorization": f"Bearer {TOKEN_A}", "Idempotency-Key": "hook-rej-2"},
    )
    assert bad.status_code == 422
