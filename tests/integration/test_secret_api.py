"""Sidecar/Agent HTTP contract (docs/protocol/secret-sidecar-api.md) against the real app:
metadata-only listing, grant, lease, one-time resolve, stable 403 codes, revocation feed
long-poll, cleanup acknowledgement."""

from __future__ import annotations

import base64
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.identity.principals import token_hash
from server.main import create_app
from tests.integration.secrets_seed import MASTER, T0, Seed

pytestmark = pytest.mark.db
SEED = Seed("api")
TOK_ADMIN = "svc-secret-api-admin-0001"
TOK_AGENT = "svc-secret-api-agent-0001"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    rt = SEED.runtime(eng, FixedClock(T0))
    agent = SEED.register_agent(eng, rt, "agent-api-1")
    with Session(eng) as s, s.begin():
        for acc, tok in ((SEED.admin, TOK_ADMIN), (uuid.UUID(agent.account_uuid), TOK_AGENT)):
            s.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                    "VALUES (:i, :a, :f, :h)"
                ),
                {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{tok}", "h": token_hash(tok)},
            )
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def server(database_url: str, engine: Engine) -> Iterator[str]:
    import base64 as b64

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    os.environ["AGENT_COLAB_MASTER_KEY_B64"] = b64.b64encode(MASTER.key).decode()
    os.environ["AGENT_COLAB_MASTER_KEY_ID"] = MASTER.key_id
    app = create_app(Settings(database_url=database_url, base_url=base))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
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


def _h(token: str, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": f"api-{key}"}


def test_sidecar_contract_end_to_end(server: str, engine: Engine) -> None:
    value = b"api-secret-" + uuid.uuid4().hex.encode()
    client = httpx.Client(base_url=server, timeout=15)
    r = client.post(
        "/api/v1/secrets",
        json={
            "name": "api/key",
            "value_b64": base64.b64encode(value).decode(),
            "metadata": {"env": "test"},
        },
        headers=_h(TOK_ADMIN, "reg"),
    )
    assert r.status_code == 201, r.text
    ref = r.json()["secret_ref"]
    listing = client.get("/api/v1/secrets", headers=_h(TOK_ADMIN, "list")).json()
    assert [i["secret_ref"] for i in listing["items"]] == [ref]
    assert value.decode() not in listing["items"][0].__repr__()  # metadata only
    r = client.post(
        f"/api/v1/secrets/{ref}/grants",
        json={"agent_id": "agent-api-1", "task_id": "task-api-1", "ttl_seconds": 120},
        headers=_h(TOK_ADMIN, "grant"),
    )
    assert r.status_code == 201, r.text
    grant_id = r.json()["grant_id"]
    # an Agent cannot register secrets or create grants (normalized 404 / denied)
    assert client.post(
        "/api/v1/secrets",
        json={"name": "x", "value_b64": "YQ=="},
        headers=_h(TOK_AGENT, "reg-agent"),
    ).status_code in (403, 404)
    r = client.post(
        f"/api/v1/secrets/{ref}/leases",
        json={"task_id": "task-api-1", "sidecar_instance_id": "sc-1"},
        headers=_h(TOK_AGENT, "lease"),
    )
    assert r.status_code == 201, r.text
    lease = r.json()
    handle = lease["handle"]
    # host binding and scope
    r = client.post(
        "/api/v1/secrets/resolve",
        json={"handle": handle, "sidecar_instance_id": "sc-2", "task_id": "task-api-1"},
        headers=_h(TOK_AGENT, "res-host"),
    )
    assert r.status_code == 403 and r.json()["code"] == "SECRET_HANDLE_HOST_MISMATCH"
    r = client.post(
        "/api/v1/secrets/resolve",
        json={"handle": handle, "sidecar_instance_id": "sc-1", "task_id": "task-api-1"},
        headers=_h(TOK_AGENT, "res-ok"),
    )
    assert r.status_code == 200, r.text
    assert base64.b64decode(r.json()["secret_b64"]) == value
    r = client.post(
        "/api/v1/secrets/resolve",
        json={"handle": handle, "sidecar_instance_id": "sc-1", "task_id": "task-api-1"},
        headers=_h(TOK_AGENT, "res-again"),
    )
    assert r.status_code == 403 and r.json()["code"] == "SECRET_HANDLE_USED"
    assert "secret_b64" not in r.text and value.decode() not in r.text
    # revocation feed: nothing yet, then the grant revocation appears (long-poll ≤ 5 s)
    feed = client.get(
        "/api/v1/secrets/revocations", params={"since": 0}, headers=_h(TOK_AGENT, "feed-0")
    ).json()
    assert feed["items"] == []
    r = client.post(
        f"/api/v1/secrets/grants/{grant_id}/revoke",
        json={"reason_code": "ADMIN_REVOKE"},
        headers=_h(TOK_ADMIN, "rev"),
    )
    assert r.status_code == 200, r.text
    feed = client.get(
        "/api/v1/secrets/revocations",
        params={"since": 0, "max_wait_s": 2},
        headers=_h(TOK_AGENT, "feed-1"),
    ).json()
    assert (
        feed["items"]
        and feed["items"][-1]["kind"] == "grant"
        and lease["lease_id"] in feed["items"][-1]["lease_ids"]
    )
    r = client.post(
        f"/api/v1/secrets/leases/{lease['lease_id']}/ack-cleanup", headers=_h(TOK_AGENT, "ack")
    )
    assert r.status_code == 200 and r.json()["acknowledged"] is True
    # SSE stream replays the revocation with its sequence id
    with client.stream(
        "GET",
        "/api/v1/secrets/revocations/stream",
        params={"since": 0, "max_events": 1},
        headers=_h(TOK_AGENT, "sse"),
    ) as resp:
        body = b"".join(resp.iter_bytes()).decode()
    assert "event: revocation" in body and f"id: {feed['items'][-1]['seq']}" in body
    with Session(engine) as s:  # no value anywhere in the DB text columns of this flow
        blob = s.execute(
            text("SELECT string_agg(payload::text, ' ') FROM events WHERE workspace_id = :w"),
            {"w": SEED.ws},
        ).scalar()
        assert value.decode() not in str(blob)
