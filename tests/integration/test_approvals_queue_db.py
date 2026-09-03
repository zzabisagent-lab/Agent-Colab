"""V-P4-33 (P4-14): HIGH approval rejected without re-auth and APPROVED after a server-side MFA
proof (a body/header claim never counts); CRITICAL shows quorum 2 accurately; the same Human twice
is rejected; Agents never approve."""

from __future__ import annotations

import datetime as dt
import urllib.parse
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.security import totp
from tests.integration.phase4_security_seed import Seed, make_app, seed

pytestmark = pytest.mark.db
T0 = dt.datetime(2026, 6, 4, 9, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def world(database_url: str, engine: Engine) -> Iterator[tuple[TestClient, Seed, FixedClock]]:
    app = make_app(database_url)
    clock = FixedClock(T0)
    app.state.clock = clock
    app.state.runtime.clock = clock
    with TestClient(app) as client:
        yield client, seed(engine, "apq"), clock


def _mfa(client: TestClient, s: Seed, who: str, clock: FixedClock) -> str:
    h = s.bearer(who)
    uri = client.post("/api/v1/auth/mfa/enroll", headers=h).json()["otpauth_uri"]
    secret = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)["secret"][0]
    assert (
        client.post(
            "/api/v1/auth/mfa/confirm", json={"code": totp.totp(secret, clock.now())}, headers=h
        ).status_code
        == 200
    )
    return secret


def _task(client: TestClient, s: Seed, key: str) -> str:
    r = client.post(
        "/api/v1/tasks",
        json={
            "title": f"queue {key}",
            "channel_id": str(s.channel),
            "domain": "research",
            "risk": "LOW",
            "criteria": [{"statement": "done", "check_type": "evidence", "required": True}],
        },
        headers={**s.bearer("member"), "Idempotency-Key": f"apq-task-{key}"},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["resource_id"])


def _request(client: TestClient, s: Seed, task_id: str, action: str, key: str) -> str:
    r = client.post(
        "/api/v1/approvals",
        json={"subject_type": "task", "subject_id": task_id, "action": action},
        headers={**s.bearer("member"), "Idempotency-Key": f"apq-req-{key}"},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["resource_id"])


def test_queue_reauth_quorum_and_human_only(world: tuple[TestClient, Seed, FixedClock]) -> None:
    client, s, clock = world
    task = _task(client, s, "1")
    high = _request(client, s, task, "api:external_send", "high")
    crit = _request(client, s, task, "api:llm_exposure", "crit")
    qr = client.get("/api/v1/approvals/queue", headers=s.bearer("owner"))
    assert qr.status_code == 200, qr.text
    q = qr.json()
    by_id = {i["approval_id"]: i for i in q["items"]}
    assert (
        by_id[high]["risk"] == "HIGH"
        and by_id[high]["reauth_required"]
        and by_id[high]["quorum_required"] == 1
    )
    assert (
        by_id[crit]["risk"] == "CRITICAL"
        and by_id[crit]["quorum_required"] == 2
        and by_id[crit]["quorum_remaining"] == 2
    )
    assert q["reauth_verified"] is False
    # HIGH without re-auth: rejected even with a forged body claim
    r = client.post(
        f"/api/v1/approvals/{high}/queue-decide",
        json={"decision": "APPROVE", "reauth_verified": True},
        headers={**s.bearer("owner"), "Idempotency-Key": "apq-d1"},
    )
    assert r.status_code == 403 and r.json()["code"] == "REAUTH_REQUIRED", r.text
    r = client.post(
        f"/api/v1/approvals/{high}/decide",
        json={"decision": "APPROVE", "reauth_verified": True},
        headers={**s.bearer("owner"), "Idempotency-Key": "apq-d1b"},
    )
    assert r.status_code in (403, 404) and r.json()["code"] == "REAUTH_REQUIRED", r.text
    # after a real MFA verification the same request is APPROVED
    secret = _mfa(client, s, "owner", clock)
    assert (
        client.post(
            "/api/v1/auth/mfa/verify",
            json={"code": totp.totp(secret, clock.now())},
            headers=s.bearer("owner"),
        ).status_code
        == 200
    )
    r = client.post(
        f"/api/v1/approvals/{high}/queue-decide",
        json={"decision": "APPROVE"},
        headers={**s.bearer("owner"), "Idempotency-Key": "apq-d2"},
    )
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    # CRITICAL: Agent never; same Human twice rejected; second distinct Human completes quorum 2
    r = client.post(
        f"/api/v1/approvals/{crit}/queue-decide",
        json={"decision": "APPROVE"},
        headers={**s.bearer("agent"), "Idempotency-Key": "apq-d3"},
    )
    assert r.status_code == 403 and r.json()["code"] == "APPROVAL_HUMAN_ONLY"
    r = client.post(
        f"/api/v1/approvals/{crit}/queue-decide",
        json={"decision": "APPROVE"},
        headers={**s.bearer("owner"), "Idempotency-Key": "apq-d4"},
    )
    assert (
        r.status_code == 200
        and r.json()["status"] == "PENDING"
        and r.json()["approvals_recorded"] == 1
    )
    item = {
        i["approval_id"]: i
        for i in client.get("/api/v1/approvals/queue", headers=s.bearer("owner")).json()["items"]
    }[crit]
    assert (
        item["quorum_current"] == 1
        and item["quorum_remaining"] == 1
        and item["already_decided_by_me"]
    )
    r = client.post(
        f"/api/v1/approvals/{crit}/queue-decide",
        json={"decision": "APPROVE"},
        headers={**s.bearer("owner"), "Idempotency-Key": "apq-d5"},
    )
    assert r.status_code == 403 and r.json()["code"] == "APPROVAL_DUPLICATE_APPROVER"
    secret2 = _mfa(client, s, "member2", clock)
    assert (
        client.post(
            "/api/v1/auth/mfa/verify",
            json={"code": totp.totp(secret2, clock.now())},
            headers=s.bearer("member2"),
        ).status_code
        == 200
    )
    r = client.post(
        f"/api/v1/approvals/{crit}/queue-decide",
        json={"decision": "APPROVE"},
        headers={**s.bearer("member2"), "Idempotency-Key": "apq-d6"},
    )
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    assert crit not in {
        i["approval_id"]
        for i in client.get("/api/v1/approvals/queue", headers=s.bearer("owner")).json()["items"]
    }
