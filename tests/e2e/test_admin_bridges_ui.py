"""V-P2-12 (UI half, P2-07): Bridge changes by an unauthorized account are rejected in the web
console; an authorized administrator manages Bridges. The real server serves the built console
under /admin; Playwright drives Chromium (user-space wrapper on hosts without root)."""

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
from server.secrets.envelope import new_master_key
from tests.e2e.mfa_helper import enroll_totp

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web-admin"
WS, CHANNEL, TG_PI = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
ADMIN, MEMBER = uuid.uuid4(), uuid.uuid4()
TOK_ADMIN, TOK_MEMBER = "svc-ui-admin-token-0001", "svc-ui-member-token-0001"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ui', 'ui')"),
            {"i": WS},
        )
        for acc, name, tok in (
            (ADMIN, "acct-ui-admin", TOK_ADMIN),
            (MEMBER, "acct-ui-member", TOK_MEMBER),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) "
                    "VALUES (:i, :a, :w, 'human', :a)"
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
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "base_url, team_or_bot_ref) VALUES (:i, 'tg:900001', :w, 'telegram', NULL, "
                "'ui-bot')"
            ),
            {"i": TG_PI, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name, "
                "external_channel_id) VALUES (:i, 'chan-ui', :w, 'work', 'UI channel', 'mm-ui-1')"
            ),
            {"i": CHANNEL, "w": WS},
        )
        for acc in (ADMIN, MEMBER):
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHANNEL, "a": acc},
            )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "ui-admin", "ui admin")
        repo.commit_role_version(
            s,
            "ui-admin",
            ["channel.manage", "bridge.manage", "admin.settings", "task.read"],
            [],
            {},
            ADMIN,
        )
        repo.create_role(s, WS, "ui-member", "ui member")
        repo.commit_role_version(s, "ui-member", ["task.read", "channel.read"], [], {}, ADMIN)
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        repo.assign_role(s, ADMIN, "ui-admin", ADMIN, now)
        repo.assign_role(s, MEMBER, "ui-member", ADMIN, now)
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


def test_bridge_admin_ui_authorization(server: str) -> None:
    if shutil.which("pnpm") is None:
        pytest.skip("pnpm not available")
    wrapper = Path.home() / ".local" / "bin" / "chrome-headless-shell-wrapped"
    secret = enroll_totp(server, TOK_ADMIN, "ui-mfa")
    env = {
        **os.environ,
        "WEB_ADMIN_URL": f"{server}/admin",
        "E2E_TOTP_SECRET_B32": secret,
        "E2E_AUTHORIZED_TOKEN": TOK_ADMIN,
        "E2E_UNAUTHORIZED_TOKEN": TOK_MEMBER,
        "E2E_CHANNEL_ID": "chan-ui",
        "E2E_TG_INSTANCE": "tg:900001",
    }
    if wrapper.exists():
        env["PLAYWRIGHT_CHROMIUM_EXECUTABLE"] = str(wrapper)
    proc = subprocess.run(
        ["pnpm", "exec", "playwright", "test", "tests/bridges.spec.ts"],
        cwd=WEB,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]


def test_unauthorized_api_change_is_rejected_and_audited(server: str, engine: Engine) -> None:
    import httpx

    body = {"provider_instance_id": "tg:900001", "telegram_chat_id": "-1001234567891"}
    r = httpx.post(
        f"{server}/api/v1/channels/chan-ui/bridges",
        json=body,
        headers={"Authorization": f"Bearer {TOK_MEMBER}", "Idempotency-Key": "ui-api-1"},
    )
    assert r.status_code == 404  # normalized policy denial
    with Session(engine) as s:
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM telegram_bridges "
                    "WHERE telegram_chat_id = '-1001234567891'"
                )
            ).scalar_one()
            == 0
        )
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE action = 'policy.deny' AND actor_account_id = :a"
                ),
                {"a": MEMBER},
            ).scalar_one()
            >= 1
        )
