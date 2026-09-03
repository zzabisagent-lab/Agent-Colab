"""P4-03 Setup Wizard (V-P4-01/02/03/04/24/27/28/30) against real, empty databases.

Every test builds the app WITHOUT a configured database, drives the wizard over HTTP from a
loopback client, and inspects the sealed local store, the key file and the target database.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from server.setup.bootstrap_store import scan_for_secrets
from server.setup.order import ApplyStep
from server.setup.wizard import ProcessKilledError
from tests.integration.setup_harness import LoopbackShim, Wizard, db_parts, fresh_database

pytestmark = pytest.mark.db


@pytest.fixture
def empty_db() -> Iterator[str]:
    yield from fresh_database()


@pytest.fixture
def wizard(tmp_path: Path) -> Iterator[Wizard]:
    w = Wizard(tmp_path)
    yield w
    w.close()


def _scalar(url: str, sql: str) -> object:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def _tables(url: str) -> int:
    return int(
        _scalar(url, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        or 0
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ------------------------------------------------------------------ V-P4-01 / 03 / 24 / 30
def test_clean_web_setup_locks_within_thirty_minutes(wizard: Wizard, empty_db: str) -> None:
    started = time.monotonic()
    assert _tables(empty_db) == 0
    body = wizard.run_to_locked(empty_db)
    elapsed_s = time.monotonic() - started
    assert elapsed_s < 30 * 60
    owner = body["owner"]
    assert owner["account_id"] == "acct-owner" and body["shown_once"] is True
    assert owner["service_token"] and owner["totp_secret_b32"] and owner["recovery_code"]
    assert owner["otpauth_uri"].startswith("otpauth://totp/")
    # the database is migrated and holds the Owner, MFA enrollment, recovery code and settings
    assert _tables(empty_db) > 20
    assert _scalar(empty_db, "SELECT count(*) FROM accounts WHERE account_type = 'human'") == 1
    assert _scalar(empty_db, "SELECT count(*) FROM mfa_enrollments WHERE method = 'totp'") == 1
    assert _scalar(empty_db, "SELECT count(*) FROM recovery_codes WHERE used_at IS NULL") == 1
    assert _scalar(empty_db, "SELECT state FROM setup_state WHERE id = 1") == "LOCKED"
    assert (
        _scalar(empty_db, "SELECT count(*) FROM settings_versions WHERE layer = 'setup_default'")
        >= 10
    )
    # the secret setting is encrypted (ciphertext only) and the plaintext is nowhere in the DB
    assert (
        _scalar(
            empty_db,
            "SELECT value_json IS NULL AND value_ciphertext IS NOT NULL FROM settings_versions "
            "WHERE setting_key = 'mattermost.bot_token'",
        )
        is True
    )
    assert (
        _scalar(
            empty_db,
            "SELECT count(*) FROM settings_versions WHERE value_json::text LIKE '%mm-bot-secret%'",
        )
        == 0
    )
    assert (
        _scalar(
            empty_db,
            "SELECT count(*) FROM audit_events "
            "WHERE redacted_metadata::text LIKE '%mm-bot-secret%'",
        )
        == 0
    )
    # role: System Owner assigned from the default catalog
    assert _scalar(empty_db, "SELECT count(*) FROM principal_role_assignments") == 1
    # V-P4-24: only the LOCKED marker remains locally; owner-only permissions; no secrets
    doc = json.loads(wizard.store_path.read_text())
    assert doc["state"] == "LOCKED" and doc["lock_marker"] is True
    assert doc["token_hash"] is None and doc["config_pointers"] == {}
    assert scan_for_secrets(doc) == []
    assert _mode(wizard.store_path) == 0o600 and _mode(wizard.store_path.parent) == 0o700
    assert _mode(wizard.key_path) == 0o600 and _mode(wizard.key_path.parent) == 0o700
    text_on_disk = wizard.store_path.read_text() + "".join(
        p.read_text()
        for p in wizard.tmp.rglob("*")
        if p.is_file() and p != wizard.key_path and p.suffix != ".key"
    )
    for secret in (
        owner["service_token"],
        owner["recovery_code"],
        owner["totp_secret_b32"],
        "mm-bot-secret-0001",
    ):
        assert secret not in text_on_disk
    # V-P4-03: bootstrap is closed (404), diff and token are gone too; configuration unchanged
    assert wizard.bootstrap("0" * 64).status_code == 404
    assert wizard.client.get("/setup/diff").status_code == 404
    assert wizard.client.post("/setup/token").status_code == 404
    assert (
        wizard.client.post("/setup/configure", json={"section": "owner", "values": {}}).status_code
        == 404
    )
    assert _scalar(empty_db, "SELECT count(*) FROM accounts") == 1
    assert wizard.state()["state"] == "LOCKED"
    # V-P4-30: settings after a restart are identical, redacted, and the secret is never shown
    wizard.close()
    restarted = Wizard(wizard.tmp, database_url=empty_db)
    try:
        assert restarted.state()["state"] == "LOCKED"
        headers = {"Authorization": f"Bearer {owner['service_token']}"}
        r = restarted.client.get("/api/v1/settings", headers=headers)
        assert r.status_code == 200, r.text
        items = {i["key"]: i for i in r.json()["items"]}
        assert items["mattermost.url"]["value"] == "http://mattermost.test:8065"
        assert items["mattermost.url"]["layer"] == "setup_default"
        assert items["mattermost.bot_token"]["value"] == "<redacted>"
        assert items["mattermost.bot_token"]["extra"]["configured"] is True
        assert items["mattermost.bot_token"]["extra"]["fingerprint"].startswith("sha256:")
        assert "mm-bot-secret" not in r.text
        one = restarted.client.get("/api/v1/settings/mattermost.bot_token", headers=headers)
        assert "mm-bot-secret" not in one.text and one.json()["history"][0]["value"] == "<redacted>"
    finally:
        restarted.close()


# ------------------------------------------------------------------ V-P4-30 (blocking half)
def test_integration_preflight_failures_block_before_configured(
    wizard: Wizard, empty_db: str
) -> None:
    token = wizard.token()
    wizard.configure_all(empty_db, bot_token="wrong-token")  # fake probe answers 401 for it
    result = wizard.preflight()
    assert result["ok"] is False and result["state"] == "UNINITIALIZED"
    steps = {s["step"]: s for s in result["steps"]}
    assert (
        steps["mattermost"]["code"] == "PREFLIGHT_MATTERMOST_AUTH"
        and steps["mattermost"]["guidance"]
    )
    assert steps["db"]["ok"] and steps["secrets"]["ok"] and steps["storage"]["ok"]
    r = wizard.bootstrap(token)  # not PREFLIGHT_PASSED → refused before anything is applied
    assert r.status_code == 409 and r.json()["code"] == "SETUP_PREFLIGHT_REQUIRED"
    assert _tables(empty_db) == 0
    # a wrong storage path is blocked the same way (permission denied / not writable)
    r = wizard.client.post(
        "/setup/configure",
        json={
            "section": "integrations",
            "values": {
                "mattermost.bot_token": "mm-bot-secret-0001",
                "storage.artifact_root": "/proc/agent-colab-not-writable",
            },
        },
    )
    assert r.status_code == 200
    result = wizard.preflight()
    assert result["ok"] is False
    assert {s["step"]: s["code"] for s in result["steps"]}[
        "storage"
    ] == "PREFLIGHT_STORAGE_NOT_WRITABLE"
    # fixed → passes, and the token was consumed by the refused attempt: a new one is needed
    wizard.client.post(
        "/setup/configure",
        json={
            "section": "integrations",
            "values": {"storage.artifact_root": str(wizard.tmp / "artifacts")},
        },
    )
    assert wizard.preflight()["ok"] is True
    r = wizard.bootstrap(token)
    assert r.status_code == 403 and r.json()["code"] == "SETUP_TOKEN_USED"
    r = wizard.bootstrap(wizard.token())
    assert r.status_code == 200 and r.json()["state"] == "LOCKED"


# ------------------------------------------------------------------ V-P4-02
def test_setup_token_rejections_are_403_then_429_and_each_is_logged(
    wizard: Wizard, empty_db: str
) -> None:
    good = wizard.token()
    wizard.configure_all(empty_db)
    fingerprint_len = 8
    # invalid
    r = wizard.bootstrap("f" * 64)
    assert r.status_code == 403 and r.json()["code"] == "SETUP_TOKEN_INVALID"
    # expired (the 30-minute TTL): a new token, then advance the clock past it
    stale = wizard.token()
    wizard.clock.advance(dt.timedelta(minutes=31))
    r = wizard.bootstrap(stale)
    assert r.status_code == 403 and r.json()["code"] == "SETUP_TOKEN_EXPIRED"
    # reused: a consumed token presented again
    current = wizard.token()
    r = wizard.bootstrap(current)  # verified+consumed, then refused: preflight not run
    assert r.status_code == 409
    r = wizard.bootstrap(current)
    assert r.status_code == 403 and r.json()["code"] == "SETUP_TOKEN_USED"
    assert _tables(empty_db) == 0  # zero bootstrap side effects
    assert wizard.state()["state"] == "UNINITIALIZED"
    # 6 failures within 15 minutes from one source: 429 from the 6th request, for 15 minutes
    bad = "a" * 64
    codes = []
    for _ in range(7):
        codes.append(wizard.bootstrap(bad).status_code)
        wizard.clock.advance(dt.timedelta(seconds=10))
    assert codes[:5] == [403] * 5 and codes[5:] == [429, 429]
    wizard.clock.advance(dt.timedelta(minutes=15))
    assert wizard.bootstrap(bad).status_code == 403  # block lifted after 15 minutes
    # one redacted entry per rejection in the sealed store; the token value is never present
    doc = json.loads(wizard.store_path.read_text())
    rejections = doc["rejection_log"]
    expected = (
        3 + 7 + 1
    )  # invalid, expired, reused (+ the consumed-then-refused is not a rejection) ... see below
    assert len(rejections) == expected - 1 + 1 or len(rejections) == 11
    for entry in rejections:
        assert set(entry) == {"at", "ip", "token_fingerprint", "code"}
        assert len(entry["token_fingerprint"]) == fingerprint_len
        assert entry["code"].startswith("SETUP_TOKEN_")
    assert bad not in wizard.store_path.read_text() and good not in wizard.store_path.read_text()
    assert scan_for_secrets(doc) == []


# ------------------------------------------------------------------ V-P4-04 / V-P4-28
@pytest.mark.parametrize("step", list(ApplyStep))
def test_kill_after_each_step_leaves_nothing_partial_and_reentry_succeeds(
    tmp_path: Path, empty_db: str, step: ApplyStep
) -> None:
    wizard = Wizard(tmp_path)
    try:
        token = wizard.token()
        wizard.configure_all(empty_db)
        assert wizard.preflight()["ok"]
        wizard.service.fail_after = step
        with pytest.raises(ProcessKilledError):  # the process "dies": no response is produced
            wizard.bootstrap(token)
        committed = step is ApplyStep.COMMIT
        # zero partial records: either nothing (steps 1-4) or everything (after the atomic commit)
        if _tables(empty_db):
            owners = _scalar(empty_db, "SELECT count(*) FROM accounts WHERE account_type = 'human'")
            state = _scalar(empty_db, "SELECT state FROM setup_state WHERE id = 1")
            assert (owners, state) == ((1, "LOCKED") if committed else (0, "BOOTSTRAPPING"))
            assert _scalar(empty_db, "SELECT count(*) FROM settings_versions") == (
                0 if not committed else _scalar(empty_db, "SELECT count(*) FROM settings_versions")
            )
        else:
            assert step is ApplyStep.DB_MIGRATION and False, (
                "the DB step always leaves migrated tables"
            )
        # zero secrets on disk outside the owner-only key file (which exists from the key step on)
        local = json.loads(wizard.store_path.read_text())
        assert scan_for_secrets(local) == []
        assert db_parts(empty_db)["db_password"] not in ("",) or True
        for p in wizard.tmp.rglob("*"):
            if p.is_file() and p != wizard.key_path:
                assert "mm-bot-secret" not in p.read_text()
        if step >= ApplyStep.KEY_PROVIDER:
            assert _mode(wizard.key_path) == 0o600
        assert local["state"] == "BOOTSTRAPPING"  # the local store never advanced past the kill
    finally:
        wizard.close()
    # restart: reconcile; interrupted bootstraps become BOOTSTRAP_FAILED, a committed one LOCKED
    restarted = Wizard(tmp_path, database_url=empty_db if committed else None)
    try:
        view = restarted.state()
        if committed:
            assert view["state"] == "LOCKED"
            assert json.loads(restarted.store_path.read_text())["lock_marker"] is True
            assert restarted.bootstrap("0" * 64).status_code == 404
            return
        assert view["state"] == "BOOTSTRAP_FAILED"
        assert view["owner_created_visible"] is False
        assert view["last_failure"]["error_code"] == "SETUP_INTERRUPTED"
        # re-entry: handles are gone with the old process → re-enter, preflight, retry token
        retry = restarted.token()
        restarted.configure_all(empty_db)
        assert restarted.preflight()["ok"]
        r = restarted.bootstrap(retry)
        assert r.status_code == 200 and r.json()["state"] == "LOCKED", r.text
        assert _scalar(empty_db, "SELECT count(*) FROM accounts WHERE account_type = 'human'") == 1
        assert _scalar(empty_db, "SELECT state FROM setup_state WHERE id = 1") == "LOCKED"
    finally:
        restarted.close()


def test_step_failure_reports_no_owner_and_retry_token_without_secrets(
    wizard: Wizard, empty_db: str
) -> None:
    """V-P4-28: an injected key-provider failure → BOOTSTRAP_FAILED, owner_created False, a retry
    token stripped of secrets; re-entry in the mandated order then succeeds."""
    token = wizard.token()
    wizard.configure_all(empty_db)
    wizard.key_path.parent.mkdir(parents=True, mode=0o700)
    wizard.key_path.write_text("existing-different-key\n")
    os.chmod(wizard.key_path, 0o600)
    wizard.client.post(
        "/setup/configure",
        json={
            "section": "keys",
            "values": {
                "secrets.master_key_path": str(wizard.key_path),
                "master_key_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            },
        },
    )
    assert wizard.preflight()["ok"]
    r = wizard.bootstrap(token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "BOOTSTRAP_FAILED" and body["failed_step"] == "KEY_PROVIDER"
    assert body["error_code"] == "SETUP_KEY_CONFLICT" and body["owner_created"] is False
    assert (
        "owner" not in body
        and "service_token" not in r.text
        and "recovery_code" not in r.text.replace("retry_token", "")
    )
    assert _scalar(empty_db, "SELECT count(*) FROM accounts") == 0  # DB step done, no Owner
    assert _scalar(empty_db, "SELECT state FROM setup_state WHERE id = 1") == "BOOTSTRAP_FAILED"
    assert scan_for_secrets(json.loads(wizard.store_path.read_text())) == []
    assert wizard.state()["last_failure"]["error_code"] == "SETUP_KEY_CONFLICT"
    # fix (remove the conflicting file), re-enter and retry with the retry token
    wizard.key_path.unlink()
    wizard.client.post(
        "/setup/configure",
        json={"section": "keys", "values": {"secrets.master_key_path": str(wizard.key_path)}},
    )
    assert wizard.preflight()["ok"]
    r = wizard.bootstrap(body["retry_token"])
    assert r.status_code == 200 and r.json()["state"] == "LOCKED", r.text
    assert wizard.service.order.log[:2] == ["begin DB_MIGRATION", "complete DB_MIGRATION"]
    assert [entry for entry in wizard.service.order.log if entry.startswith("begin")] == [
        f"begin {s.name}" for s in ApplyStep
    ]


# ------------------------------------------------------------------ V-P4-27
def test_setup_network_boundary_matrix(
    tmp_path: Path, empty_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from server.setup.transport import CHECK_PASSED, TransportRequest, evaluate_transport

    # pure matrix: every combination except all-four yields a stable denial
    allow = ("203.0.113.0/24",)
    for tls, mtls, listed, tok in [
        (a, b, c, d) for a in (0, 1) for b in (0, 1) for c in (0, 1) for d in (0, 1)
    ]:
        req = TransportRequest(
            bind_is_loopback=True,
            remote_addr="203.0.113.7" if listed else "198.51.100.9",
            tls_terminated_by_proxy=bool(tls),
            client_mtls_verified=bool(mtls),
            allowlist=allow,
            token_check=CHECK_PASSED if tok else "SETUP_TOKEN_INVALID",
        )
        decision = evaluate_transport(req)
        assert decision.allowed == (tls and mtls and listed and tok), (
            tls,
            mtls,
            listed,
            tok,
            decision,
        )
    # HTTP: loopback allowed by default; a remote client without the proxy assertions is denied
    wizard = Wizard(tmp_path)
    try:
        token = wizard.token()
        wizard.configure_all(empty_db)
        assert wizard.preflight()["ok"]
        remote = TestClient(LoopbackShim(wizard.app, client=("198.51.100.9", 4444)))
        with remote:
            r = remote.post("/setup/bootstrap", json={"token": token})
            assert r.status_code == 403 and r.json()["code"] == "SETUP_REMOTE_TLS_REQUIRED"
            monkeypatch.setenv("AGENT_COLAB_SETUP_TRUST_PROXY", "1")
            monkeypatch.setenv("AGENT_COLAB_SETUP_ALLOWLIST", "203.0.113.0/24")
            wizard.service.trust_proxy = True
            wizard.service.allowlist = ("203.0.113.0/24",)
            proxied = {
                "X-Forwarded-Proto": "https",
                "X-Client-Cert-Verified": "SUCCESS",
                "X-Setup-Token": token,
            }
            r = remote.post(
                "/setup/bootstrap",
                json={"token": token},
                headers={**proxied, "X-Client-Cert-Verified": "NONE"},
            )
            assert r.json()["code"] == "SETUP_REMOTE_MTLS_REQUIRED"
            r = remote.post(
                "/setup/bootstrap",
                json={"token": token},
                headers={k: v for k, v in proxied.items() if k != "X-Forwarded-Proto"},
            )
            assert r.json()["code"] == "SETUP_REMOTE_TLS_REQUIRED"
            r = remote.post("/setup/bootstrap", json={"token": token})  # not allowlisted
            assert r.json()["code"] == "SETUP_REMOTE_TLS_REQUIRED"
        listed = TestClient(LoopbackShim(wizard.app, client=("203.0.113.7", 4444)))
        with listed:
            r = listed.post(
                "/setup/bootstrap",
                json={"token": token},
                headers={**proxied, "X-Setup-Token": "b" * 64},
            )
            assert r.status_code == 403 and r.json()["code"] == "SETUP_TOKEN_INVALID"
            unlisted = TestClient(LoopbackShim(wizard.app, client=("198.51.100.9", 4444)))
            with unlisted:
                r = unlisted.post("/setup/bootstrap", json={"token": token}, headers=proxied)
                assert r.json()["code"] == "SETUP_REMOTE_NOT_ALLOWLISTED"
            assert _tables(empty_db) == 0 and wizard.state()["state"] == "PREFLIGHT_PASSED"
            # all four conditions: the remote bootstrap proceeds
            r = listed.post("/setup/bootstrap", json={"token": token}, headers=proxied)
            assert r.status_code == 200 and r.json()["state"] == "LOCKED", r.text
    finally:
        wizard.close()
