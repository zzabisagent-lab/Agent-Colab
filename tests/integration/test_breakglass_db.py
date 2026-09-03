"""V-P4-21 (P4-10): activation needs Owner + recovery code + TOTP; actions are audited; the session
expires/terminates with an announcement and an automatic post-hoc verification Task; Event
immutability and secret plaintext reads stay impossible."""

from __future__ import annotations

import datetime as dt
import urllib.parse
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.security import totp
from tests.integration.phase4_security_seed import Seed, make_app, seed

pytestmark = pytest.mark.db
T0 = dt.datetime(2026, 6, 3, 9, 0, tzinfo=dt.UTC)


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
        yield client, seed(engine, "bg"), clock


def _enroll(client: TestClient, s: Seed, who: str, clock: FixedClock) -> tuple[str, list[str]]:
    h = s.bearer(who)
    body = client.post("/api/v1/auth/mfa/enroll", headers=h).json()
    secret = urllib.parse.parse_qs(urllib.parse.urlparse(body["otpauth_uri"]).query)["secret"][0]
    assert (
        client.post(
            "/api/v1/auth/mfa/confirm", json={"code": totp.totp(secret, clock.now())}, headers=h
        ).status_code
        == 200
    )
    return secret, body["recovery_codes"]


def test_break_glass_lifecycle(world: tuple[TestClient, Seed, FixedClock], engine: Engine) -> None:
    client, s, clock = world
    secret, codes = _enroll(client, s, "owner", clock)
    h = {**s.bearer("owner"), "Idempotency-Key": "bg-1"}
    base = {"scope": "restore DB access", "reason": "primary admin path down"}
    # wrong recovery code / wrong TOTP / non-owner → no session, audit only
    r = client.post(
        "/api/v1/breakglass/activate",
        json={**base, "recovery_code": "AAAAA-AAAAA", "totp_code": totp.totp(secret, clock.now())},
        headers=h,
    )
    assert r.status_code == 401 and r.json()["code"] == "MFA_RECOVERY_CODE_INVALID"
    r = client.post(
        "/api/v1/breakglass/activate",
        json={**base, "recovery_code": codes[0], "totp_code": "000000"},
        headers=h,
    )
    assert r.status_code == 401 and r.json()["code"] == "MFA_CODE_INVALID"
    r = client.post(
        "/api/v1/breakglass/activate",
        json={**base, "recovery_code": codes[1], "totp_code": "000000"},
        headers=s.bearer("admin"),
    )
    assert r.status_code == 404  # Administrators are denied admin.break_glass (normalized)
    with Session(engine) as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM breakglass_sessions WHERE workspace_id = :w"),
                {"w": s.ws},
            ).scalar_one()
            == 0
        )
    # activation (recovery code 0 was consumed by the failed attempt? no: consumed only on success)
    r = client.post(
        "/api/v1/breakglass/activate",
        json={**base, "recovery_code": codes[1], "totp_code": totp.totp(secret, clock.now())},
        headers=h,
    )
    assert r.status_code == 201, r.text
    bg = r.json()
    assert (
        bg["active"] and bg["expires_at"] == (clock.now() + dt.timedelta(seconds=3600)).isoformat()
    )
    sid = bg["session_id"]
    with Session(engine) as db:
        events = db.execute(
            text("SELECT type FROM events WHERE aggregate_id = :s ORDER BY aggregate_seq"),
            {"s": sid},
        ).all()
        assert [e[0] for e in events] == ["BREAK_GLASS_STARTED"]
        announce = db.execute(
            text(
                "SELECT payload->>'event_type' FROM delivery_outbox WHERE "
                "destination = 'mattermost:ops_channel' AND dedupe_key LIKE :k"
            ),
            {"k": f"breakglass:{sid}:%"},
        ).all()
        assert [a[0] for a in announce] == ["BREAK_GLASS_STARTED"]
    # actions in the session are recorded and audited; immutability and secret reads unchanged
    act = client.get("/api/v1/auth/me", headers={**s.bearer("owner"), "X-Break-Glass-Session": sid})
    assert act.status_code == 200
    with Session(engine) as db, db.begin():
        with pytest.raises(Exception, match=r"append-only|immutable|not allowed|forbid"):
            db.execute(
                text("UPDATE events SET payload = '{}'::jsonb WHERE aggregate_id = :s"), {"s": sid}
            )
    with Session(engine) as db:
        acts = db.execute(
            text("SELECT method, path FROM breakglass_actions WHERE session_id = :s"), {"s": sid}
        ).all()
        assert ("GET", "/api/v1/auth/me") in [(a[0], a[1]) for a in acts]
        assert (
            db.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = "
                    "'breakglass.action' AND target_id = :s"
                ),
                {"s": sid},
            ).scalar_one()
            >= 1
        )
    secret_read = client.get(
        "/api/v1/secrets/sec-anything/value",
        headers={**s.bearer("owner"), "X-Break-Glass-Session": sid},
    )
    assert secret_read.status_code in (404, 405)  # no plaintext read path exists
    # termination → BREAK_GLASS_ENDED, announcement, post-hoc verification Task
    t = client.post(f"/api/v1/breakglass/{sid}/terminate", headers=h)
    assert t.status_code == 200, t.text
    assert t.json()["ended_reason"] == "TERMINATED" and t.json()["posthoc_task_id"]
    task_id = t.json()["posthoc_task_id"]
    with Session(engine) as db:
        types = [
            e[0]
            for e in db.execute(
                text("SELECT type FROM events WHERE aggregate_id = :s ORDER BY aggregate_seq"),
                {"s": sid},
            ).all()
        ]
        assert types == ["BREAK_GLASS_STARTED", "BREAK_GLASS_ENDED"]
        title = db.execute(
            text("SELECT title, risk FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
        ).first()
        assert title is not None and sid in title[0] and title[1] == "HIGH"
        vr = db.execute(
            text(
                "SELECT verifier_account_id, implementer_account_id FROM "
                "verification_runs WHERE target_id = :t"
            ),
            {"t": task_id},
        ).first()
        assert vr is not None and vr[0] != vr[1] and vr[1] == s.ids["owner"]
    again = client.post(f"/api/v1/breakglass/{sid}/terminate", headers=h)
    assert again.status_code == 409 and again.json()["code"] == "BREAK_GLASS_ENDED"
    # expiry: a second session ends automatically after 60 minutes via the sweep
    r = client.post(
        "/api/v1/breakglass/activate",
        json={**base, "recovery_code": codes[2], "totp_code": totp.totp(secret, clock.now())},
        headers={**h, "Idempotency-Key": "bg-2"},
    )
    assert r.status_code == 201, r.text
    sid2 = r.json()["session_id"]
    clock.advance(dt.timedelta(minutes=60, seconds=1))
    assert client.post("/api/v1/breakglass/sweep", headers=s.bearer("owner")).json()["ended"] == [
        sid2
    ]
    g = client.get(f"/api/v1/breakglass/{sid2}", headers=s.bearer("owner")).json()
    assert g["ended_reason"] == "EXPIRED" and g["posthoc_task_id"]
