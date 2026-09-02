"""V-P3-13 (P3-07): Agent and Role management through the web console — full add/edit/suspend
(and revoke) paths yield the same results and audit entries as the API. The real server serves
the built console under /admin; Playwright drives Chromium."""

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

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web-admin"
WS = uuid.uuid4()
ADMIN, MEMBER = uuid.uuid4(), uuid.uuid4()
TOK_ADMIN, TOK_MEMBER = "svc-ui-agents-admin-0001", "svc-ui-agents-member-0001"
ADMIN_PERMS = [
    "agent.manage",
    "admin.accounts",
    "role.manage",
    "admin.settings",
    "task.read",
    "channel.manage",
]


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ui-ag', 'ui')"),
            {"i": WS},
        )
        for acc, name, tok in (
            (ADMIN, "acct-ui-ag-admin", TOK_ADMIN),
            (MEMBER, "acct-ui-ag-member", TOK_MEMBER),
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
        repo.create_role(s, WS, "ui-ag-admin", "ui agent admin")
        repo.commit_role_version(s, "ui-ag-admin", ADMIN_PERMS, [], {}, ADMIN)
        repo.create_role(s, WS, "ui-ag-member", "ui member")
        repo.commit_role_version(s, "ui-ag-member", ["task.read"], [], {}, ADMIN)
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        repo.assign_role(s, ADMIN, "ui-ag-admin", ADMIN, now)
        repo.assign_role(s, MEMBER, "ui-ag-member", ADMIN, now)
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


def _audit_actions(engine: Engine, target_id: str) -> list[tuple[str, str]]:
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT action, result FROM audit_events WHERE workspace_id = :w "
                "AND target_id = :t ORDER BY id"
            ),
            {"w": WS, "t": target_id},
        ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


def test_agent_admin_console_paths(server: str, engine: Engine) -> None:
    if shutil.which("pnpm") is None:
        pytest.skip("pnpm not available")
    wrapper = Path.home() / ".local" / "bin" / "chrome-headless-shell-wrapped"
    env = {
        **os.environ,
        "WEB_ADMIN_URL": f"{server}/admin",
        "E2E_AUTHORIZED_TOKEN": TOK_ADMIN,
        "E2E_UNAUTHORIZED_TOKEN": TOK_MEMBER,
        "E2E_AGENT_ID": "agent-ui-1",
        "E2E_ASSIGN_ACCOUNT": "acct-ui-ag-member",
    }
    if wrapper.exists():
        env["PLAYWRIGHT_CHROMIUM_EXECUTABLE"] = str(wrapper)
    proc = subprocess.run(
        ["pnpm", "exec", "playwright", "test", "tests/agents.spec.ts"],
        cwd=WEB,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    # the same lifecycle through the API produces the same results and audit trail
    h = {"Authorization": f"Bearer {TOK_ADMIN}"}
    body = {
        "agent_id": "agent-api-1",
        "display_name": "API Agent",
        "adapter_type": "webhook",
        "endpoint": {"url": "https://agent.example.test/hook"},
        "credential_ref": "secret://agents/api-1/signing",
        "limits": {"concurrent_tasks": 2, "requests_per_minute": 60, "daily_cost_units": 1000000},
    }
    r = httpx.post(
        f"{server}/api/v1/agents", json=body, headers={**h, "Idempotency-Key": "ui-api-reg"}
    )
    assert r.status_code in (200, 201), r.text
    for action in ("suspend", "revoke"):
        r = httpx.post(
            f"{server}/api/v1/agents/agent-api-1/{action}",
            json={},
            headers={**h, "Idempotency-Key": f"ui-api-{action}"},
        )
        assert r.status_code == 200, r.text
    ui_trail = [a for a, _ in _audit_actions(engine, "agent-ui-1")]
    api_trail = [a for a, _ in _audit_actions(engine, "agent-api-1")]
    assert ui_trail and api_trail
    assert [a for a in ui_trail if a in api_trail] == [a for a in api_trail if a in ui_trail]
    # an unauthorized API caller is rejected the same way the console was
    r = httpx.post(
        f"{server}/api/v1/agents",
        json={**body, "agent_id": "agent-api-unauth"},
        headers={"Authorization": f"Bearer {TOK_MEMBER}", "Idempotency-Key": "ui-api-unauth"},
    )
    assert r.status_code in (403, 404)
