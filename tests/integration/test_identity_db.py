"""P1-05 DB/API tests: credentials, sessions, spoof audit (V-P1-08); links on tables (V-P1-23)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.api.v1.identity import router as identity_router
from server.config import Settings
from server.domain.clock import FixedClock
from server.events.store import InMemoryEventStore
from server.identity.external_links import link_id_for, sql_service
from server.identity.principals import (
    IdentityError,
    create_session,
    fingerprint_of,
    issue_service_token,
    resolve_service_token,
    resolve_session,
    revoke_service_token,
    revoke_session,
    rotate_service_token,
)
from server.main import create_app

pytestmark = pytest.mark.db

WS = uuid.uuid4()
T0 = dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    from server.db.engine import make_engine

    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-idn', 'idn')"),
            {"i": WS},
        )
        for acc, typ in (
            ("acct-idn-svc", "service"),
            ("acct-idn-alice", "human"),
            ("acct-idn-bob", "human"),
            ("acct-idn-admin", "human"),
        ):
            c.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, account_id, workspace_id, account_type, display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": uuid.uuid4(), "a": acc, "w": WS, "t": typ},
            )
        for pid, prov in (
            ("mm-idn-a", "mattermost"),
            ("mm-idn-b", "mattermost"),
            ("tg-idn", "telegram"),
        ):
            c.execute(
                text(
                    "INSERT INTO provider_instances "
                    "(id, provider_instance_id, workspace_id, provider, "
                    "team_or_bot_ref) VALUES (:i, :p, :w, :prov, 'ref')"
                ),
                {"i": uuid.uuid4(), "p": pid, "w": WS, "prov": prov},
            )
    yield eng
    eng.dispose()


def _acc(engine: Engine, account_id: str) -> uuid.UUID:
    with engine.connect() as c:
        return uuid.UUID(
            str(
                c.execute(
                    text("SELECT id FROM accounts WHERE account_id = :a"), {"a": account_id}
                ).scalar_one()
            )
        )


def test_service_token_issue_rotate_revoke(engine: Engine) -> None:
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        token, fp = issue_service_token(
            s, "acct-idn-svc", actor_label="test", correlation_id="c", clock=clock
        )
        assert fp == fingerprint_of(token)
        p = resolve_service_token(s, token)
        assert (
            p is not None
            and p.account_id == "acct-idn-svc"
            and p.credential_kind == "service_token"
        )
        assert resolve_service_token(s, token + "x") is None
        new_token, new_fp = rotate_service_token(
            s, "acct-idn-svc", fp, actor_label="test", correlation_id="c", clock=clock
        )
        assert resolve_service_token(s, token) is None, (
            "old token rejected immediately after rotation"
        )
        assert resolve_service_token(s, new_token) is not None
        revoke_service_token(s, new_fp, actor_label="test", correlation_id="c", clock=clock)
        assert resolve_service_token(s, new_token) is None
        with pytest.raises(IdentityError):
            revoke_service_token(s, new_fp, actor_label="test", correlation_id="c", clock=clock)
        stored = s.execute(
            text("SELECT token_hash FROM service_credentials WHERE fingerprint = :f"), {"f": fp}
        ).scalar_one()
        assert token not in stored and len(stored) == 64


def test_sessions_expire_and_revoke(engine: Engine) -> None:
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        tok = create_session(s, "acct-idn-alice", ttl_seconds=3600, mfa_verified=True, clock=clock)
        p = resolve_session(s, tok, clock)
        assert p is not None and p.credential_kind == "session" and p.mfa_verified is True
        clock.advance(dt.timedelta(seconds=3601))
        assert resolve_session(s, tok, clock) is None
        tok2 = create_session(s, "acct-idn-alice", clock=clock)
        assert revoke_session(s, tok2, clock) is True
        assert resolve_session(s, tok2, clock) is None


@pytest.fixture()
def client(database_url: str, engine: Engine) -> Iterator[tuple[TestClient, str]]:
    app = create_app(Settings(database_url=database_url))
    if not any(getattr(r, "path", "") == "/api/v1/identity/me" for r in app.routes):
        app.include_router(identity_router)
    app.state.event_store = InMemoryEventStore()
    with Session(engine) as s, s.begin():
        token, _ = issue_service_token(s, "acct-idn-svc", actor_label="test", correlation_id="c")
    with TestClient(app) as c:
        yield c, token


def test_api_actor_from_credential_only_and_spoof_audited(
    client: tuple[TestClient, str], engine: Engine
) -> None:
    c, token = client
    assert c.get("/api/v1/identity/me").status_code == 401
    assert (
        c.get("/api/v1/identity/me", headers={"Authorization": "Bearer nope"}).json()["code"]
        == "AUTH_INVALID"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Colab-Actor": "acct-idn-admin",
        "Idempotency-Key": "k1",
    }
    me = c.get("/api/v1/identity/me", headers=headers).json()
    assert me["account_id"] == "acct-idn-svc"
    body = {
        "provider_instance_id": "mm-idn-a",
        "external_user_id": "mm-u-spoof",
        "actor_account_id": "acct-idn-admin",
        "on_behalf_of": "acct-idn-alice",
    }
    r = c.post("/api/v1/identity/links/challenge", json=body, headers=headers)
    assert r.status_code == 201, r.text
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT actor_label, redacted_metadata FROM audit_events "
                "WHERE action = 'identity.spoof_attempt' "
                "AND target_id = 'acct-idn-svc' ORDER BY id"
            )
        ).all()
        chal = conn.execute(
            text(
                "SELECT redacted_metadata->>'provider_instance_id' FROM audit_events "
                "WHERE action = 'identity.link_challenge' "
                "ORDER BY id DESC LIMIT 1"
            )
        ).scalar_one()
    assert len(rows) >= 2, "header claim and body claims audited"
    claims = {cl for _, m in rows for cl in m["claims"]}
    assert {"header:x-colab-actor", "body:actor_account_id", "body:on_behalf_of"} <= {
        x.lower() for x in claims
    }
    assert all(lbl == "acct-idn-svc" for lbl, _ in rows)
    assert "acct-idn-admin" not in str([m for _, m in rows]), "claimed values are never recorded"
    assert chal == "mm-idn-a"


def test_links_on_real_tables(engine: Engine) -> None:
    clock = FixedClock(T0)
    store = InMemoryEventStore(clock=clock)
    actor = _acc(engine, "acct-idn-svc")
    with Session(engine) as s, s.begin():
        svc = sql_service(s, store, clock)
        issued = svc.start_challenge(
            "mm-idn-a", "mm-u-1", actor_account_uuid=actor, correlation_id="c"
        )
        with pytest.raises(IdentityError) as exc:
            svc.confirm_challenge(
                "mm-idn-a",
                "mm-u-1",
                "99999999",
                "acct-idn-alice",
                path="web",
                actor_account_uuid=actor,
                correlation_id="c",
            )
        assert exc.value.code == "EXTERNAL_IDENTITY_CHALLENGE_INVALID"
        link = svc.confirm_challenge(
            "mm-idn-a",
            "mm-u-1",
            issued.code,
            "acct-idn-alice",
            path="web",
            actor_account_uuid=actor,
            correlation_id="c",
        )
        assert link.status == "active"
        assert svc.resolve_command_principal("mm-idn-a", "mm-u-1").account_id == "acct-idn-alice"
        # same external user on another instance is an independent link
        issued_b = svc.start_challenge(
            "mm-idn-b", "mm-u-1", actor_account_uuid=actor, correlation_id="c"
        )
        svc.confirm_challenge(
            "mm-idn-b",
            "mm-u-1",
            issued_b.code,
            "acct-idn-bob",
            path="web",
            actor_account_uuid=actor,
            correlation_id="c",
        )
        assert svc.resolve_command_principal("mm-idn-b", "mm-u-1").account_id == "acct-idn-bob"
        # application-level duplicate
        with pytest.raises(IdentityError) as exc:
            svc.start_challenge("mm-idn-a", "mm-u-1", actor_account_uuid=actor, correlation_id="c")
        assert exc.value.code == "EXTERNAL_IDENTITY_DUPLICATE"
        # suspension blocks commands immediately (same transaction, no side effects on resolve)
        svc.suspend_link(link.link_id, "POLICY", actor_account_uuid=actor, correlation_id="c")
        with pytest.raises(IdentityError) as exc:
            svc.resolve_command_principal("mm-idn-a", "mm-u-1")
        assert exc.value.code == "EXTERNAL_IDENTITY_NOT_ACTIVE"
        svc.revoke_link(link.link_id, "LEFT", actor_account_uuid=actor, correlation_id="c")
    # DB-level duplicate: a second row for the same (instance, user) violates the unique constraint
    with engine.connect() as conn:
        inst = conn.execute(
            text("SELECT id FROM provider_instances WHERE provider_instance_id = 'mm-idn-a'")
        ).scalar_one()
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                "external_user_id, account_id, verification_method, status) VALUES "
                "(:i, 'link-dup', :p, 'mm-u-1', :a, 'signed_challenge', 'active')"
            ),
            {"i": uuid.uuid4(), "p": inst, "a": _acc(engine, "acct-idn-bob")},
        )
    with engine.connect() as conn:
        n_active = conn.execute(
            text(
                "SELECT count(*) FROM external_identity_links l "
                "JOIN provider_instances p ON p.id = l.provider_instance_id "
                "WHERE p.provider_instance_id = 'mm-idn-a' AND l.external_user_id = 'mm-u-1' "
                "AND l.status = 'active'"
            )
        ).scalar_one()
        events = store.stream(str(WS), "external_identity_link", link_id_for("mm-idn-a", "mm-u-1"))
    assert n_active == 0
    assert [e["type"] for e in events] == [
        "IDENTITY_LINK_CHALLENGED",
        "IDENTITY_LINK_VERIFIED",
        "IDENTITY_LINK_SUSPENDED",
        "IDENTITY_LINK_REVOKED",
    ]
