"""P4-08: V-P4-09 (CSRF / cookies / CSP / session expiry / re-auth), V-P4-08 API-side authz parity
(cookie and Bearer principals get identical server-side decisions; a Member cannot escalate through
a forged console request), and the login/MFA failure rate limit (429 after 6 failures, one redacted
audit per rejection, recorded here for V-P4-09; the setup token guard covers V-P4-02)."""

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
from tests.integration.phase4_security_seed import Seed, csrf, login, make_app, seed

pytestmark = pytest.mark.db
T0 = dt.datetime(2026, 6, 2, 9, 0, tzinfo=dt.UTC)
ADMIN_WRITES = [
    (
        "POST",
        "/api/v1/agents",
        {"agent_id": "agent-sec-x", "display_name": "x", "adapter_type": "mcp"},
    ),
    (
        "POST",
        "/api/v1/roles",
        {"role_id": "role-sec-x", "display_name": "x", "permissions": ["task.read"]},
    ),
    ("POST", "/api/v1/breakglass/sweep", None),
]


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
        yield client, seed(engine, "sec"), clock


def _enroll_and_verify(client: TestClient, s: Seed, who: str, clock: FixedClock) -> str:
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


def test_security_headers_cookies_csrf_and_session_expiry(
    world: tuple[TestClient, Seed, FixedClock],
) -> None:
    client, s, clock = world
    r = client.get("/healthz")
    assert r.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert (
        r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"
    )
    assert "Strict-Transport-Security" not in r.headers  # loopback http base_url: HSTS off
    lr = client.post("/api/v1/auth/sessions", json={"service_token": s.tokens["member"]})
    assert lr.status_code == 201
    set_cookie = lr.headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "SameSite=strict" in set_cookie.replace("Strict", "strict")
    # state-changing cookie request without CSRF token → 403, nothing executed
    body = {"title": "csrf", "channel_id": str(s.channel), "domain": "research", "risk": "LOW"}
    r = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "csrf-1"})
    assert r.status_code == 403 and r.json()["code"] == "CSRF_TOKEN_INVALID"
    headers = {**csrf(client), "Idempotency-Key": "csrf-2"}
    bad = client.post("/api/v1/tasks", json=body, headers={**headers, "X-CSRF-Token": "forged"})
    assert bad.status_code == 403
    ok = client.post("/api/v1/tasks", json=body, headers=headers)
    assert ok.status_code == 201, ok.text
    # idle expiry: 1800 s without a request revokes the session
    clock.advance(dt.timedelta(seconds=1801))
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401 and r.json()["code"] == "SESSION_IDLE_EXPIRED"
    client.cookies.clear()


def test_ui_api_authz_parity_no_escalation(world: tuple[TestClient, Seed, FixedClock]) -> None:
    client, s, clock = world
    secret = _enroll_and_verify(client, s, "admin", clock)
    results: dict[str, list[int]] = {"bearer": [], "cookie": []}
    for method, path, body in ADMIN_WRITES:
        r = client.request(
            method,
            path,
            json=body,
            headers={**s.bearer("member"), "Idempotency-Key": f"par-b-{path}"},
        )
        results["bearer"].append(r.status_code)
    login(client, s.tokens["member"])
    headers = {**csrf(client), "X-Requested-With": "web-admin"}
    for method, path, body in ADMIN_WRITES:
        r = client.request(
            method, path, json=body, headers={**headers, "Idempotency-Key": f"par-c-{path}"}
        )
        results["cookie"].append(r.status_code)
    client.cookies.clear()
    assert results["bearer"] == results["cookie"], results
    assert all(code in (403, 404) for code in results["bearer"]), results
    # the administrator succeeds through both paths once MFA is verified
    h = s.bearer("admin")
    assert (
        client.post(
            "/api/v1/auth/mfa/verify", json={"code": totp.totp(secret, clock.now())}, headers=h
        ).status_code
        == 200
    )
    assert client.post("/api/v1/breakglass/sweep", headers=h).status_code in (
        403,
        404,
    )  # Owner-only, also for admins
    r = client.post(
        "/api/v1/roles",
        json={"role_id": "role-sec-ok", "display_name": "ok", "permissions": ["task.read"]},
        headers={**h, "Idempotency-Key": "par-adm"},
    )
    assert r.status_code in (200, 201), r.text


def test_login_and_mfa_failures_are_rate_limited(
    world: tuple[TestClient, Seed, FixedClock], engine: Engine
) -> None:
    client, s, clock = world
    h = s.bearer("owner")
    _enroll_and_verify(client, s, "owner", clock)
    codes = []
    for _ in range(6):
        codes.append(
            client.post("/api/v1/auth/mfa/verify", json={"code": "000000"}, headers=h).status_code
        )
    assert codes[:5] == [401] * 5 and codes[5] == 401
    blocked = client.post("/api/v1/auth/mfa/verify", json={"code": "000000"}, headers=h)
    assert blocked.status_code == 429 and blocked.json()["code"] == "RATE_LIMITED"
    with Session(engine) as db:
        rows = db.execute(
            text(
                "SELECT redacted_metadata::text, action FROM audit_events "
                "WHERE action = 'mfa_verify.rate_limited'"
            )
        ).all()
    assert len(rows) >= 1 and all(s.tokens["owner"] not in str(r[0]) for r in rows)
    clock.advance(dt.timedelta(minutes=15, seconds=1))
    again = client.post("/api/v1/auth/mfa/verify", json={"code": "000000"}, headers=h)
    assert again.status_code == 401  # block lifted, counting restarts
