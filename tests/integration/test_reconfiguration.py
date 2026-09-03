"""V-P4-19 (P4-03): reconfiguration from LOCKED needs maintenance mode + recovery code + MFA
re-authentication; the 29-minute session reconfigures, the 30-minute expiry and ordinary
sessions get 403 with the configuration unchanged and the state back to LOCKED."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from tests.integration.setup_harness import Wizard, fresh_database, install_fake_reauth

pytestmark = pytest.mark.db


@pytest.fixture
def empty_db() -> Iterator[str]:
    yield from fresh_database()


def _versions(url: str, key: str) -> int:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT count(*) FROM settings_versions WHERE setting_key = :k"),
                    {"k": key},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _member_token(url: str, clock: Any) -> str:
    """An ordinary human session: a member Account with a service token but no owner role."""
    import uuid

    from server.identity.principals import token_hash

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            ws = conn.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar_one()
            acc = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, status, "
                    "display_name) VALUES (:i, 'acct-member', :w, 'human', 'ACTIVE', 'Member')"
                ),
                {"i": acc, "w": ws},
            )
            conn.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                    "VALUES (:i, :a, 'sha256:member', :h)"
                ),
                {"i": uuid.uuid4(), "a": acc, "h": token_hash("svc-member-token-0001")},
            )
    finally:
        engine.dispose()
    return "svc-member-token-0001"


def test_reconfiguration_session_boundaries(tmp_path: Path, empty_db: str) -> None:
    wizard = Wizard(tmp_path)
    try:
        body = wizard.run_to_locked(empty_db)
    finally:
        wizard.close()
    owner_token, recovery = body["owner"]["service_token"], body["owner"]["recovery_code"]
    app = Wizard(tmp_path, database_url=empty_db)
    restore = install_fake_reauth(app.clock)
    try:
        owner = {"Authorization": f"Bearer {owner_token}"}
        member = {"Authorization": f"Bearer {_member_token(empty_db, app.clock)}"}
        assert app.state()["state"] == "LOCKED"
        # without maintenance mode: refused, recovery code untouched
        r = app.client.post("/setup/reconfigure", json={"recovery_code": recovery}, headers=owner)
        assert r.status_code == 403 and r.json()["code"] == "SETUP_REAUTH_REQUIRED", r.text
        # maintenance mode on (Owner, MFA-fresh)
        r = app.client.post(
            "/api/v1/maintenance/enter", json={"reason": "reconfiguration"}, headers=owner
        )
        assert r.status_code == 200, r.text
        # an ordinary session (member) cannot open a reconfiguration session at all
        r = app.client.post("/setup/reconfigure", json={"recovery_code": recovery}, headers=member)
        assert r.status_code == 403
        # without a fresh MFA proof: refused
        restore()
        r = app.client.post("/setup/reconfigure", json={"recovery_code": recovery}, headers=owner)
        assert r.status_code == 403 and r.json()["code"] == "SETUP_REAUTH_REQUIRED"
        restore = install_fake_reauth(app.clock)
        # wrong recovery code: refused
        r = app.client.post(
            "/setup/reconfigure", json={"recovery_code": "AAAA-BBBB-CCCC-DDDD"}, headers=owner
        )
        assert r.status_code == 403 and r.json()["code"] == "SETUP_REAUTH_REQUIRED"
        # all three proofs: session opens; the used recovery code is rotated (shown once)
        r = app.client.post("/setup/reconfigure", json={"recovery_code": recovery}, headers=owner)
        assert r.status_code == 201, r.text
        opened = r.json()
        sid, next_code = opened["session_id"], opened["recovery_code_next"]
        assert app.state()["state"] == "RECONFIGURING"
        r = app.client.post("/setup/reconfigure", json={"recovery_code": recovery}, headers=owner)
        assert r.status_code in (403, 409)  # the old code is consumed / a session is already open
        # 29 minutes later the session still reconfigures
        app.clock.advance(dt.timedelta(minutes=29))
        before = _versions(empty_db, "instance.name")
        r = app.client.put(
            f"/setup/reconfigure/{sid}/settings",
            json={"changes": {"instance.name": "Colab Renamed"}},
            headers=owner,
        )
        assert r.status_code == 200, r.text
        assert _versions(empty_db, "instance.name") == before + 1
        # an ordinary session with the session id: 403, unchanged
        r = app.client.put(
            f"/setup/reconfigure/{sid}/settings",
            json={"changes": {"instance.name": "Nope"}},
            headers=member,
        )
        assert r.status_code == 403 and _versions(empty_db, "instance.name") == before + 1
        # 30 minutes: expired → 403, configuration unchanged, state LOCKED
        app.clock.advance(dt.timedelta(minutes=1, seconds=1))
        r = app.client.put(
            f"/setup/reconfigure/{sid}/settings",
            json={"changes": {"instance.name": "Late"}},
            headers=owner,
        )
        assert r.status_code == 403 and r.json()["code"] == "SETUP_SESSION_EXPIRED", r.text
        assert _versions(empty_db, "instance.name") == before + 1
        assert app.state()["state"] == "LOCKED"
        # a second session with the rotated code works again and closes cleanly
        r = app.client.post("/setup/reconfigure", json={"recovery_code": next_code}, headers=owner)
        assert r.status_code == 201, r.text
        sid2 = r.json()["session_id"]
        r = app.client.post(f"/setup/reconfigure/{sid2}/close", headers=owner)
        assert r.status_code == 200 and r.json()["state"] == "LOCKED"
        # bootstrap stays closed throughout
        assert app.client.post("/setup/bootstrap", json={"token": "0" * 64}).status_code == 404
    finally:
        restore()
        app.close()
