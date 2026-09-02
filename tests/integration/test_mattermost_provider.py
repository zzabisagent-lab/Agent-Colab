"""V-P2-01 against the real local Mattermost Team Edition: a `/colab task create` slash command in
a channel creates the TASK_CREATED Event and a thread reply. Requires
scripts/dev/mattermost-local.sh and ~/.local/opt/mattermost/.spike-credentials (never printed)."""

from __future__ import annotations

import datetime as dt
import os
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

from server.channels.mattermost import provider as prov
from server.channels.mattermost.client import HttpMattermostClient
from server.config import Settings
from server.db.engine import make_engine
from server.identity.principals import token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "mattermost-local.sh"
CREDS = Path.home() / ".local" / "opt" / "mattermost" / ".spike-credentials"
MM_URL = "http://127.0.0.1:8065"
WS = uuid.uuid4()
ADMIN = uuid.uuid4()
TOK_ADMIN = "svc-mm-admin"


def _creds() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in CREDS.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


@pytest.fixture(scope="module")
def mattermost() -> Iterator[dict[str, str]]:
    if not CREDS.exists() or not SCRIPT.exists():
        pytest.skip("local Mattermost credentials/script unavailable")
    subprocess.run(["bash", str(SCRIPT), "start"], check=False, capture_output=True, timeout=180)
    for _ in range(60):
        try:
            if httpx.get(f"{MM_URL}/api/v4/system/ping", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    else:
        pytest.skip("local Mattermost did not start")
    creds = _creds()
    yield creds
    subprocess.run(["bash", str(SCRIPT), "stop"], check=False, capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-mm', 'mm')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-mm-admin', :w, 'human', 'mm admin')"
            ),
            {"i": ADMIN, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                "VALUES (:i, :a, 'sha256:mm-admin', :h)"
            ),
            {"i": uuid.uuid4(), "a": ADMIN, "h": token_hash(TOK_ADMIN)},
        )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "mm-admin", "mm admin")
        repo.commit_role_version(
            s, "mm-admin", ["channel.manage", "task.*", "approval.*"], [], {}, ADMIN
        )
        repo.assign_role(s, ADMIN, "mm-admin", ADMIN, dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def server(database_url: str, engine: Engine, mattermost: dict[str, str]) -> Iterator[str]:
    os.environ["AGENT_COLAB_MATTERMOST_BOT_TOKEN"] = mattermost["BOT_TOKEN"]
    os.environ["AGENT_COLAB_MATTERMOST_ADMIN_TOKEN"] = mattermost["ADMIN_TOKEN"]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = create_app(Settings(database_url=database_url, base_url=base))
    from starlette.routing import BaseRoute, Mount

    from server.api.v1.channels import router as channels_router
    from server.api.v1.providers_mattermost import router as mm_router

    if not any(getattr(r, "path", "") == "/api/v1/channels/import" for r in app.routes):
        app.include_router(channels_router)
        app.include_router(mm_router)
        mounts: list[BaseRoute] = [r for r in app.router.routes if isinstance(r, Mount)]
        app.router.routes = [r for r in app.router.routes if r not in mounts] + mounts
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    yield base
    srv.should_exit = True
    thread.join(timeout=10)
    os.environ.pop("AGENT_COLAB_MATTERMOST_BOT_TOKEN", None)
    os.environ.pop("AGENT_COLAB_MATTERMOST_ADMIN_TOKEN", None)


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOK_ADMIN}", "Idempotency-Key": key}


def test_slash_task_create_in_real_mattermost(
    server: str, engine: Engine, mattermost: dict[str, str]
) -> None:
    admin = HttpMattermostClient(MM_URL, mattermost["ADMIN_TOKEN"])
    me = admin.me()
    team = admin.get_team_by_name("colab-test")
    channels = {c["name"]: c for c in admin.list_team_channels(team["id"])}
    assert "work-a" in channels, "spike tenant missing channel work-a"
    channel = channels["work-a"]
    api = httpx.Client(base_url=server, timeout=30)
    inst = api.post(
        "/api/v1/providers/mattermost/instances",
        json={
            "base_url": MM_URL,
            "team_name": "colab-test",
            "team_id": team["id"],
            "bot_user_id": mattermost["BOT_USER_ID"],
        },
        headers=_h("mm-inst"),
    )
    assert inst.status_code == 201, inst.text
    pid = inst.json()["resource_id"]
    assert inst.json()["identity_display"] in ("override", "prefix")
    reg = api.post(
        "/api/v1/providers/mattermost/commands/register",
        json={
            "provider_instance_id": pid,
            "callback_url": f"{server}/api/v1/providers/mattermost/commands",
        },
        headers=_h("mm-reg"),
    )
    assert reg.status_code == 201, reg.text
    imp = api.post(
        "/api/v1/channels/import",
        json={
            "provider_instance_id": pid,
            "external_channel_id": channel["id"],
            "channel_type": "work",
        },
        headers=_h("mm-imp"),
    )
    assert imp.status_code == 201, imp.text
    with Session(engine) as s, s.begin():
        instance = prov.load_instance(s, pid)
        assert instance is not None
        s.execute(
            text(
                "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                "external_user_id, "
                "account_id, verification_method, status, verified_at) VALUES (:i, :l, :p, :e, :a, "
                "'admin_approval', 'active', now())"
            ),
            {"i": uuid.uuid4(), "l": "link-mm-admin", "p": instance.id, "e": me["id"], "a": ADMIN},
        )
        row = prov.internal_channel(s, instance.id, channel["id"])
        assert row is not None
        s.execute(
            text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
            {"c": row["id"], "a": ADMIN},
        )
        before = s.execute(
            text("SELECT count(*) FROM events WHERE workspace_id = :w AND type = 'TASK_CREATED'"),
            {"w": WS},
        ).scalar_one()
    title = f"MM e2e {uuid.uuid4().hex[:6]}"
    executed = httpx.post(
        f"{MM_URL}/api/v4/commands/execute",
        headers={"Authorization": f"Bearer {mattermost['ADMIN_TOKEN']}"},
        json={
            "channel_id": channel["id"],
            "command": f'/colab task create "{title}" --criteria "report attached"',
        },
        timeout=30,
    )
    assert executed.status_code == 200, executed.text[:300]
    with Session(engine) as s:
        after = s.execute(
            text("SELECT count(*) FROM events WHERE workspace_id = :w AND type = 'TASK_CREATED'"),
            {"w": WS},
        ).scalar_one()
        task = s.execute(
            text("SELECT task_id FROM tasks_projection WHERE title = :t"), {"t": title}
        ).first()
        binding = s.execute(
            text("SELECT root_post_id FROM thread_bindings WHERE subject_id = :t"),
            {"t": task[0] if task else ""},
        ).first()
    assert after == before + 1 and task is not None, executed.text[:400]
    assert binding is not None
    root = admin.get_post(str(binding[0]))
    assert task[0] in root.message and root.channel_id == channel["id"]
    assert root.user_id == mattermost["BOT_USER_ID"]
    # free text in the channel is never interpreted as a command (zero Events)
    admin.create_post(channel["id"], f"task create {title} again please")
    with Session(engine) as s:
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM events WHERE workspace_id = :w AND type = 'TASK_CREATED'"
                ),
                {"w": WS},
            ).scalar_one()
            == after
        )
    # the same trigger cannot be replayed: a forged re-post of the form is refused
    forged = api.post(
        "/api/v1/providers/mattermost/commands",
        data={
            "token": "forged",
            "team_id": team["id"],
            "channel_id": channel["id"],
            "user_id": me["id"],
            "user_name": me["username"],
            "command": "/colab",
            "text": f'task create "{title} forged" --criteria "x"',
            "trigger_id": "forged-1",
        },
    )
    assert forged.status_code == 401
