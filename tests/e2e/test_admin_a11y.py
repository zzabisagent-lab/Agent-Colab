"""V-P4-18 (P4 console): axe WCAG 2.1 AA scan of every console route and keyboard-only critical
flows, run by Playwright against the real server (built console under /admin)."""

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
WS, ADMIN = uuid.uuid4(), uuid.uuid4()
TOK_ADMIN = "svc-ui-a11y-admin-0001"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-a11y', 'a')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-a11y-admin', :w, 'human', 'A11y Admin')"
            ),
            {"i": ADMIN, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                "VALUES (:i, :a, 'sha256:a11y', :h)"
            ),
            {"i": uuid.uuid4(), "a": ADMIN, "h": token_hash(TOK_ADMIN)},
        )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "a11y-admin", "a11y admin")
        repo.commit_role_version(
            s,
            "a11y-admin",
            ["agent.manage", "admin.settings", "admin.accounts", "task.read"],
            [],
            {},
            ADMIN,
        )
        repo.assign_role(s, ADMIN, "a11y-admin", ADMIN, dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
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


def test_console_accessibility(server: str) -> None:
    if shutil.which("pnpm") is None:
        pytest.skip("pnpm not available")
    wrapper = Path.home() / ".local" / "bin" / "chrome-headless-shell-wrapped"
    env = {**os.environ, "WEB_ADMIN_URL": f"{server}/admin", "E2E_AUTHORIZED_TOKEN": TOK_ADMIN}
    if wrapper.exists():
        env["PLAYWRIGHT_CHROMIUM_EXECUTABLE"] = str(wrapper)
    proc = subprocess.run(
        ["pnpm", "exec", "playwright", "test", "tests/a11y.spec.ts"],
        cwd=WEB,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-2000:]
