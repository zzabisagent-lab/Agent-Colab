"""P4-04 Settings (V-P4-05/V-P4-06): validation before apply; redacted diff/audit linked by
version; rollback as a new version; precedence with an emergency env override."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.identity.principals import token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository
from server.secrets.envelope import new_master_key
from server.settings.registry import SettingsError, spec_for, validate

pytestmark = pytest.mark.db
WS, ADMIN, MEMBER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
TOK_ADMIN, TOK_MEMBER = "svc-settings-admin-0001", "svc-settings-member-0001"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-settings', 's')"),
            {"i": WS},
        )
        for acc, name, tok in (
            (ADMIN, "acct-set-admin", TOK_ADMIN),
            (MEMBER, "acct-set-member", TOK_MEMBER),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, 'human', :a)"
                ),
                {"i": acc, "a": name, "w": WS},
            )
            s.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, fingerprint, "
                    "token_hash) VALUES (:i, :a, :f, :h)"
                ),
                {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{name}", "h": token_hash(tok)},
            )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "set-admin", "settings admin")
        repo.commit_role_version(
            s, "set-admin", ["admin.settings", "ops.manage", "task.read"], [], {}, ADMIN
        )
        repo.assign_role(s, ADMIN, "set-admin", ADMIN, dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def client(database_url: str, engine: Engine) -> Iterator[TestClient]:
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    app = create_app(Settings(database_url=database_url, master_key_b64=new_master_key()))
    with TestClient(app) as c:
        yield c


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": uuid.uuid4().hex}


def test_registry_validates_types_before_apply() -> None:
    assert validate(spec_for("scheduler.poll_interval_s"), "15") == 15
    for key, value, code in (
        ("scheduler.poll_interval_s", "fast", "SETTING_TYPE_INVALID"),
        ("scheduler.poll_interval_s", 0, "SETTING_RANGE_INVALID"),
        ("mattermost.url", "ftp://x", "SETTING_ENDPOINT_INVALID"),
        ("mattermost.url", "http://user:pw@mm.test", "SETTING_ENDPOINT_INVALID"),
        ("storage.artifact_root", "relative/path", "SETTING_PATH_INVALID"),
        ("secrets.provider", "keychain", "SETTING_ENUM_INVALID"),
        ("instance.default_timezone", "Mars/Olympus", "SETTING_TIMEZONE_INVALID"),
        ("instance.default_language", "english", "SETTING_LANGUAGE_INVALID"),
    ):
        with pytest.raises(SettingsError) as exc:
            validate(spec_for(key), value)
        assert exc.value.code == code, key
    with pytest.raises(SettingsError) as unknown:
        spec_for("nope.key")
    assert unknown.value.code == "SETTING_UNKNOWN"


def _version_rows(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text(
                    "SELECT count(*) FROM settings_versions WHERE setting_key IN "
                    "('scheduler.poll_interval_s','mattermost.url')"
                )
            ).scalar_one()
        )


def test_settings_api_validation_diff_audit_rollback(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V-P4-05: wrong type / endpoint / permission are rejected before any apply
    r = client.put(
        "/api/v1/settings/scheduler.poll_interval_s", json={"value": "fast"}, headers=_h(TOK_ADMIN)
    )
    assert r.status_code == 400 and r.json()["code"] == "SETTING_TYPE_INVALID"
    before = _version_rows(engine)  # settings are instance-level: compare, do not assume 0
    r = client.put(
        "/api/v1/settings/mattermost.url", json={"value": "not a url"}, headers=_h(TOK_ADMIN)
    )
    assert r.status_code == 400 and r.json()["code"] == "SETTING_ENDPOINT_INVALID"
    r = client.put(
        "/api/v1/settings/scheduler.poll_interval_s", json={"value": 10}, headers=_h(TOK_MEMBER)
    )
    assert r.status_code == 404  # normalized denial
    assert client.get("/api/v1/settings", headers=_h(TOK_MEMBER)).status_code == 404
    assert _version_rows(engine) == before  # rejected writes created no version
    # V-P4-06: change twice, redacted diff in audit linked by version, rollback → new version
    r = client.put(
        "/api/v1/settings/scheduler.poll_interval_s",
        json={"value": 10, "reason": "tune"},
        headers=_h(TOK_ADMIN),
    )
    assert r.status_code == 200 and r.json()["value"] == 10, r.text
    v0 = int(r.json()["version"])  # instance-level setting: earlier modules may have versions
    r = client.put(
        "/api/v1/settings/scheduler.poll_interval_s", json={"value": 20}, headers=_h(TOK_ADMIN)
    )
    assert r.json()["version"] == v0 + 1 and r.json()["layer"] == "runtime"
    r = client.post(
        f"/api/v1/settings/scheduler.poll_interval_s/rollback/{v0}", headers=_h(TOK_ADMIN)
    )
    assert r.status_code == 200 and r.json()["version"] == v0 + 2 and r.json()["value"] == 10
    history = client.get(
        "/api/v1/settings/scheduler.poll_interval_s", headers=_h(TOK_ADMIN)
    ).json()["history"]
    versions = [h["version"] for h in history]
    assert versions[-3:] == [v0, v0 + 1, v0 + 2] and versions == sorted(versions)
    assert history[-1]["reason"] == f"rollback to version {v0}"
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT redacted_metadata FROM audit_events WHERE action = "
                "'settings.change' AND target_id = 'scheduler.poll_interval_s' ORDER BY id"
            )
        ).all()
    diffs = [r[0]["diff"] for r in rows]
    assert diffs[1]["before"] == {"version": 1, "was": 10} and diffs[1]["after"] == {"is": 20}
    assert [r[0]["previous_version"] for r in rows] == [None, 1, 2]
    assert [r[0]["version"] for r in rows] == [1, 2, 3]
    # a secret setting: encrypted, redacted everywhere, only fingerprints in the diff
    r = client.put(
        "/api/v1/settings/notifications.smtp_password",
        json={"value": "smtp-pass-XYZ-1"},
        headers=_h(TOK_ADMIN),
    )
    assert r.status_code == 200 and r.json()["value"] == "<redacted>" and "smtp-pass" not in r.text
    r = client.put(
        "/api/v1/settings/notifications.smtp_password",
        json={"value": "smtp-pass-XYZ-2"},
        headers=_h(TOK_ADMIN),
    )
    assert "smtp-pass" not in r.text and r.json()["extra"]["fingerprint"].startswith("sha256:")
    with Session(engine) as s:
        leak = s.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE redacted_metadata::text LIKE '%smtp-pass%'"
            )
        ).scalar_one()
        plain = s.execute(
            text("SELECT count(*) FROM settings_versions WHERE value_json::text LIKE '%smtp-pass%'")
        ).scalar_one()
        secret_rows = s.execute(
            text(
                "SELECT value_ciphertext IS NOT NULL, key_ref FROM settings_versions "
                "WHERE setting_key = 'notifications.smtp_password'"
            )
        ).all()
        events = s.execute(
            text(
                "SELECT count(*) FROM events WHERE type = 'SETTING_CHANGED' AND "
                "aggregate_id = 'set-notifications-smtp_password'"
            )
        ).scalar_one()
    assert leak == 0 and plain == 0 and all(r[0] for r in secret_rows) and events == 2
    for row in secret_rows:
        assert row[1].startswith("dek://")
    history = client.get(
        "/api/v1/settings/notifications.smtp_password", headers=_h(TOK_ADMIN)
    ).json()["history"]
    assert all(h["value"] == "<redacted>" for h in history)
    # precedence: an emergency env override wins over the runtime version, read-only
    monkeypatch.setenv("AGENT_COLAB_EMERGENCY_SCHEDULER_POLL_INTERVAL_S", "7")
    view = client.get("/api/v1/settings/scheduler.poll_interval_s", headers=_h(TOK_ADMIN)).json()
    assert view["layer"] == "emergency_env" and view["value"] == 7
    monkeypatch.delenv("AGENT_COLAB_EMERGENCY_SCHEDULER_POLL_INTERVAL_S")
    # preflight is re-run for integration settings and blocks the apply on failure
    r = client.put(
        "/api/v1/settings/storage.artifact_root",
        json={"value": "/proc/agent-colab-nope"},
        headers=_h(TOK_ADMIN),
    )
    assert r.status_code == 409 and r.json()["code"] == "SETTING_PREFLIGHT_FAILED"
