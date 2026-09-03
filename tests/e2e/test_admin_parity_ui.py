"""V-P4-08 (UI half): UI/API authorization parity. A Member cannot escalate through the console
(same normalized denial as the API); an Administrator's console actions produce the same audit
trail as the equivalent API calls."""

from __future__ import annotations

import datetime as dt
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.identity.principals import token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository
from server.secrets.envelope import new_master_key

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web-admin"
WS, ADMIN, MEMBER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
TOK_ADMIN, TOK_MEMBER = "svc-ui-parity-admin-0001", "svc-ui-parity-member-0001"
ADMIN_PERMS = ["admin.accounts", "admin.settings", "admin.audit", "secret.register", "task.read"]


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ui-par', 'p')"),
            {"i": WS},
        )
        for acc, name, tok in (
            (ADMIN, "acct-ui-par-admin", TOK_ADMIN),
            (MEMBER, "acct-ui-par-member", TOK_MEMBER),
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
                    "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                    "VALUES (:i, :a, :f, :h)"
                ),
                {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{name}", "h": token_hash(tok)},
            )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "ui-par-admin", "ui parity admin")
        repo.commit_role_version(s, "ui-par-admin", ADMIN_PERMS, [], {}, ADMIN)
        repo.create_role(s, WS, "ui-par-member", "ui parity member")
        repo.commit_role_version(s, "ui-par-member", ["task.read"], [], {}, ADMIN)
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        repo.assign_role(s, ADMIN, "ui-par-admin", ADMIN, now)
        repo.assign_role(s, MEMBER, "ui-par-member", ADMIN, now)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def server(database_url: str, engine: Engine) -> Iterator[str]:
    if not (WEB / "dist" / "index.html").exists():
        subprocess.run(["pnpm", "run", "build"], cwd=WEB, check=True, capture_output=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    app = create_app(
        Settings(database_url=database_url, base_url=base, master_key_b64=new_master_key())
    )
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


def _trail(engine: Engine, target_id: str) -> list[str]:
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT action FROM audit_events WHERE workspace_id = :w AND target_id = :t "
                "ORDER BY id"
            ),
            {"w": WS, "t": target_id},
        ).all()
    return [str(r[0]) for r in rows]


def _enroll_admin_totp(server: str) -> str:
    """Enroll and confirm TOTP for the administrator through the API; returns the base32 secret."""
    from urllib.parse import parse_qs, urlparse

    from server.security.totp import totp

    h = {"Authorization": f"Bearer {TOK_ADMIN}"}
    r = httpx.post(
        f"{server}/api/v1/auth/mfa/enroll", json={}, headers={**h, "Idempotency-Key": "par-mfa-1"}
    )
    assert r.status_code in (200, 201), r.text
    secret = parse_qs(urlparse(r.json()["otpauth_uri"]).query)["secret"][0]
    r = httpx.post(
        f"{server}/api/v1/auth/mfa/confirm",
        json={"code": totp(secret, dt.datetime.now(dt.UTC))},
        headers={**h, "Idempotency-Key": "par-mfa-2"},
    )
    assert r.status_code == 200, r.text
    return secret


def test_ui_api_authorization_parity(server: str, engine: Engine) -> None:
    if shutil.which("pnpm") is None:
        pytest.skip("pnpm not available")
    secret = _enroll_admin_totp(server)
    wrapper = Path.home() / ".local" / "bin" / "chrome-headless-shell-wrapped"
    env = {
        **os.environ,
        "WEB_ADMIN_URL": f"{server}/admin",
        "E2E_AUTHORIZED_TOKEN": TOK_ADMIN,
        "E2E_UNAUTHORIZED_TOKEN": TOK_MEMBER,
        "E2E_TOTP_SECRET_B32": secret,
    }
    if wrapper.exists():
        env["PLAYWRIGHT_CHROMIUM_EXECUTABLE"] = str(wrapper)
    proc = subprocess.run(
        ["pnpm", "exec", "playwright", "test", "tests/parity.spec.ts"],
        cwd=WEB,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-2000:]
    # the API path: the Member is denied the same way, the Administrator leaves the same trail
    member = {"Authorization": f"Bearer {TOK_MEMBER}"}
    admin = {"Authorization": f"Bearer {TOK_ADMIN}"}
    from server.security.totp import totp

    verified = httpx.post(
        f"{server}/api/v1/auth/mfa/verify",
        json={"code": totp(secret, dt.datetime.now(dt.UTC))},
        headers={**admin, "Idempotency-Key": "par-mfa-3"},
    )
    assert verified.status_code == 200, verified.text
    body = {"account_id": "acct-api-parity", "display_name": "API Parity", "account_type": "human"}
    denied = httpx.post(
        f"{server}/api/v1/accounts",
        json={**body, "account_id": "acct-api-escalation"},
        headers={**member, "Idempotency-Key": "par-deny-1"},
    )
    assert denied.status_code in (403, 404)
    created = httpx.post(
        f"{server}/api/v1/accounts", json=body, headers={**admin, "Idempotency-Key": "par-api-1"}
    )
    assert created.status_code in (200, 201), created.text
    suspended = httpx.post(
        f"{server}/api/v1/accounts/acct-api-parity/suspend",
        json={},
        headers={**admin, "Idempotency-Key": "par-api-2"},
    )
    assert suspended.status_code == 200, suspended.text
    ui_trail, api_trail = _trail(engine, "acct-ui-parity"), _trail(engine, "acct-api-parity")
    assert ui_trail and ui_trail == api_trail, (ui_trail, api_trail)
    with Session(engine) as s:
        escalations = s.execute(
            text("SELECT count(*) FROM accounts WHERE account_id LIKE 'acct-%-escalation'")
        ).scalar_one()
    assert escalations == 0
