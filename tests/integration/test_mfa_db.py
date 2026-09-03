"""V-P4-20 (P4-09): TOTP enrollment/confirm/verify; MFA mandatory for Owner/Administrator
(critical actions blocked without a recent proof); Member policy; Agents/services excluded;
bypass attempts."""

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
from server.security.reauth import require_recent_mfa
from tests.integration.phase4_security_seed import Seed, csrf, login, make_app, seed

pytestmark = pytest.mark.db
T0 = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)


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
        yield client, seed(engine, "mfa"), clock


def _secret_from_uri(uri: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)["secret"][0]


def test_owner_enrolls_confirms_and_verifies_with_bearer_and_cookie(
    world: tuple[TestClient, Seed, FixedClock], engine: Engine
) -> None:
    client, s, clock = world
    h = s.bearer("owner")
    st = client.get("/api/v1/auth/mfa", headers=h).json()
    assert st == {
        "enrolled": False,
        "confirmed": False,
        "required": True,
        "verified_at": None,
        "method": None,
    }
    enrol = client.post("/api/v1/auth/mfa/enroll", headers=h)
    assert enrol.status_code == 201, enrol.text
    uri, codes = enrol.json()["otpauth_uri"], enrol.json()["recovery_codes"]
    assert len(codes) == 8 and all(len(c) == 11 for c in codes)
    secret = _secret_from_uri(uri)
    with Session(engine) as db:  # at rest: ciphertext only, never the base32 secret
        row = db.execute(
            text("SELECT secret_ciphertext, key_ref FROM mfa_enrollments WHERE account_id = :a"),
            {"a": s.ids["owner"]},
        ).first()
        assert row is not None and secret.encode() not in bytes(row[0])
        assert not db.execute(
            text("SELECT count(*) FROM recovery_codes WHERE account_id = :a AND code_hash = :c"),
            {"a": s.ids["owner"], "c": codes[0]},
        ).scalar_one()
    wrong = client.post("/api/v1/auth/mfa/confirm", json={"code": "000000"}, headers=h)
    assert wrong.status_code in (401, 409) and wrong.json()["code"] in ("MFA_CODE_INVALID",)
    ok = client.post(
        "/api/v1/auth/mfa/confirm", json={"code": totp.totp(secret, clock.now())}, headers=h
    )
    assert ok.status_code == 200, ok.text
    # Bearer verification → account-bound proof
    v = client.post(
        "/api/v1/auth/mfa/verify", json={"code": totp.totp(secret, clock.now())}, headers=h
    )
    assert v.status_code == 200 and v.json()["verified"] is True
    proof = require_recent_mfa(str(s.ids["owner"]), now=clock.now(), session_id=None)
    assert proof.method == "totp"
    # proof ages out after security.reauth_max_age_s (300 s)
    with pytest.raises(Exception) as exc:
        require_recent_mfa(
            str(s.ids["owner"]), now=clock.now() + dt.timedelta(seconds=301), session_id=None
        )
    assert "REAUTH_REQUIRED" in str(exc.value)
    # cookie session: MFA gate blocks writes until verified, then a session-bound proof exists
    login(client, s.tokens["owner"])
    headers = csrf(client)
    blocked = client.post("/api/v1/breakglass/sweep", headers=headers)
    assert blocked.status_code == 403 and blocked.json()["code"] == "MFA_REQUIRED"
    assert client.get("/api/v1/auth/mfa").status_code == 200  # safe methods stay available
    v2 = client.post(
        "/api/v1/auth/mfa/verify", json={"code": totp.totp(secret, clock.now())}, headers=headers
    )
    assert v2.status_code == 200, v2.text
    assert client.get("/api/v1/auth/mfa").json()["method"] == "totp"
    allowed = client.post("/api/v1/breakglass/sweep", headers=headers)
    assert allowed.status_code == 200, allowed.text
    # recovery code: single use
    r1 = client.post("/api/v1/auth/mfa/recovery", json={"recovery_code": codes[1]}, headers=headers)
    assert r1.status_code == 200 and r1.json()["method"] == "recovery_code"
    r2 = client.post("/api/v1/auth/mfa/recovery", json={"recovery_code": codes[1]}, headers=headers)
    assert r2.status_code == 401 and r2.json()["code"] == "MFA_RECOVERY_CODE_INVALID"
    client.cookies.clear()


def test_administrator_required_member_optional_agent_excluded(
    world: tuple[TestClient, Seed, FixedClock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, s, _clock = world
    assert client.get("/api/v1/auth/mfa", headers=s.bearer("admin")).json()["required"] is True
    assert client.get("/api/v1/auth/mfa", headers=s.bearer("member")).json()["required"] is False
    monkeypatch.setenv("AGENT_COLAB_SECURITY_MFA_MEMBERS", "true")
    assert client.get("/api/v1/auth/mfa", headers=s.bearer("member")).json()["required"] is True
    monkeypatch.delenv("AGENT_COLAB_SECURITY_MFA_MEMBERS")
    for who in ("agent", "system"):
        r = client.post("/api/v1/auth/mfa/enroll", headers=s.bearer(who))
        assert r.status_code == 403 and r.json()["code"] == "MFA_NOT_APPLICABLE"
    # an unenrolled administrator cannot bypass: verify without enrollment, forged proof claim
    r = client.post("/api/v1/auth/mfa/verify", json={"code": "123456"}, headers=s.bearer("admin"))
    assert r.status_code == 403 and r.json()["code"] == "MFA_NOT_ENROLLED"
    login(client, s.tokens["admin"])
    headers = {**csrf(client), "X-MFA-Verified": "true"}  # a header claim changes nothing
    r = client.post("/api/v1/breakglass/sweep", headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "MFA_REQUIRED"
    client.cookies.clear()
