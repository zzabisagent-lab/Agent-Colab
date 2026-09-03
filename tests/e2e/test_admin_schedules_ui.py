"""V-P5-21 / V-P5-22 (UI halves, P5-08): Schedule builder, preview, lifecycle, run-now and
history in the console; an unauthorized account is denied (real server under /admin)."""

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
from tests.e2e.mfa_helper import enroll_totp, verify_totp

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web-admin"
WS, CHANNEL, ADMIN, MEMBER, RUNNER = (uuid.uuid4() for _ in range(5))
TOK_ADMIN, TOK_MEMBER = "svc-ui-sched-admin-0001", "svc-ui-sched-member-0001"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ui-sch', 's')"),
            {"i": WS},
        )
        for acc, name, typ, tok in (
            (ADMIN, "acct-ui-sch-admin", "human", TOK_ADMIN),
            (MEMBER, "acct-ui-sch-member", "human", TOK_MEMBER),
            (RUNNER, "acct-ui-sch-runner", "service", None),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
            if tok:
                s.execute(
                    text(
                        "INSERT INTO service_credentials (id, account_id, fingerprint, "
                        "token_hash) VALUES (:i, :a, :f, :h)"
                    ),
                    {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{name}", "h": token_hash(tok)},
                )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-ui-sch', :w, 'work', 'sched')"
            ),
            {"i": CHANNEL, "w": WS},
        )
        for acc in (ADMIN, MEMBER, RUNNER):
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHANNEL, "a": acc},
            )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "ui-sch-admin", "schedule admin")
        repo.commit_role_version(
            s,
            "ui-sch-admin",
            [
                "schedule.manage",
                "schedule.run",
                "schedule.read",
                "task.create",
                "task.read",
                "admin.accounts",
            ],
            [],
            {},
            ADMIN,
        )
        repo.create_role(s, WS, "ui-sch-member", "member")
        repo.commit_role_version(s, "ui-sch-member", ["task.read"], [], {}, ADMIN)
        repo.create_role(s, WS, "ui-sch-runner", "runner")
        repo.commit_role_version(s, "ui-sch-runner", ["task.create", "task.read"], [], {}, ADMIN)
        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        repo.assign_role(s, ADMIN, "ui-sch-admin", ADMIN, now)
        repo.assign_role(s, MEMBER, "ui-sch-member", ADMIN, now)
        repo.assign_role(s, RUNNER, "ui-sch-runner", ADMIN, now)
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


def test_schedule_admin_console(server: str, engine: Engine) -> None:
    if shutil.which("pnpm") is None:
        pytest.skip("pnpm not available")
    secret = enroll_totp(server, TOK_ADMIN, "ui-sch-mfa")
    wrapper = Path.home() / ".local" / "bin" / "chrome-headless-shell-wrapped"
    env = {
        **os.environ,
        "WEB_ADMIN_URL": f"{server}/admin",
        "E2E_AUTHORIZED_TOKEN": TOK_ADMIN,
        "E2E_UNAUTHORIZED_TOKEN": TOK_MEMBER,
        "E2E_TOTP_SECRET_B32": secret,
        "E2E_CHANNEL_ID": "chan-ui-sch",
        "E2E_PRINCIPAL": "acct-ui-sch-runner",
    }
    if wrapper.exists():
        env["PLAYWRIGHT_CHROMIUM_EXECUTABLE"] = str(wrapper)
    proc = subprocess.run(
        ["pnpm", "exec", "playwright", "test", "tests/schedules.spec.ts"],
        cwd=WEB,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-2000:]
    # API half of V-P5-21: the Member's run-now is rejected; the Administrator's creates a Run
    verify_totp(server, TOK_ADMIN, secret, "ui-sch-api-mfa")
    listing = httpx.get(
        f"{server}/api/v1/schedules", headers={"Authorization": f"Bearer {TOK_ADMIN}"}
    )
    assert listing.status_code == 200, listing.text
    sid = next(
        s["schedule_id"] for s in listing.json()["items"] if s["name"] == "UI nightly report"
    )
    denied = httpx.post(
        f"{server}/api/v1/schedules/{sid}/run-now",
        json={},
        headers={"Authorization": f"Bearer {TOK_MEMBER}", "Idempotency-Key": "sch-deny-1"},
    )
    assert denied.status_code in (403, 404)
    with Session(engine) as s:
        manual = s.execute(
            text("SELECT count(*) FROM schedule_runs WHERE schedule_id = :s AND run_kind = 'MANUAL'"),
            {"s": sid},
        ).scalar_one()
    assert manual == 1
