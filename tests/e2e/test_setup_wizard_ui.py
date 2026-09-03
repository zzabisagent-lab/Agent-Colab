"""V-P4-01 (F-P4-001): the Web Setup Wizard configures a clean environment (empty database, empty
storage roots, no master key) to LOCKED through the browser alone; afterwards the bootstrap
endpoints answer 404 and the Owner can sign in with the service token shown once."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from server.api.setup import build_service
from server.config import Settings
from server.main import create_app
from tests.integration.setup_harness import db_parts, fake_mattermost_probe, fresh_database

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web-admin"


@pytest.fixture
def empty_db() -> Iterator[str]:
    yield from fresh_database()


@pytest.fixture
def server(tmp_path: Path, empty_db: str) -> Iterator[tuple[str, Path]]:
    if not (WEB / "dist" / "index.html").exists():
        subprocess.run(["pnpm", "run", "build"], cwd=WEB, check=True, capture_output=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    settings = Settings(
        database_url=None,
        base_url=base,
        bootstrap_state_path=str(tmp_path / "bootstrap" / "state.json"),
        master_key_b64=None,
    )
    app = create_app(settings)
    service = build_service(app)
    service.mattermost_probe = fake_mattermost_probe  # no Mattermost server in this environment
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    assert srv.started
    yield base, tmp_path
    srv.should_exit = True
    thread.join(timeout=10)


def test_web_wizard_configures_clean_environment(server: tuple[str, Path], empty_db: str) -> None:
    if shutil.which("pnpm") is None:
        pytest.skip("pnpm not available")
    base, tmp = server
    parts = db_parts(empty_db)
    wrapper = Path.home() / ".local" / "bin" / "chrome-headless-shell-wrapped"
    env = {
        **os.environ,
        "WEB_ADMIN_URL": f"{base}/admin",
        "E2E_DB_HOST": str(parts["db_host"]),
        "E2E_DB_PORT": str(parts["db_port"]),
        "E2E_DB_NAME": str(parts["db_name"]),
        "E2E_DB_USER": str(parts["db_user"]),
        "E2E_DB_PASSWORD": str(parts["db_password"]),
        "E2E_KEY_PATH": str(tmp / "keys" / "master.key"),
        "E2E_ARTIFACT_ROOT": str(tmp / "artifacts"),
        "E2E_DOCUMENT_ROOT": str(tmp / "documents"),
    }
    if wrapper.exists():
        env["PLAYWRIGHT_CHROMIUM_EXECUTABLE"] = str(wrapper)
    started = time.monotonic()
    proc = subprocess.run(
        ["pnpm", "exec", "playwright", "test", "tests/setup.spec.ts"],
        cwd=WEB,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-6000:] + proc.stderr[-2000:]
    assert time.monotonic() - started < 30 * 60  # configured within 30 minutes
    state = httpx.get(f"{base}/setup/state").json()
    assert state["state"] == "LOCKED"
    assert httpx.post(f"{base}/setup/bootstrap", json={"token": "x" * 32}).status_code == 404
    key = tmp / "keys" / "master.key"
    assert key.exists() and (key.stat().st_mode & 0o077) == 0
