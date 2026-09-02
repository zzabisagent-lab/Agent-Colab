"""P2-01: provider instance, channel templates (V-P2-19), channel import/configure/archive, and the
slash-command endpoint's token/nonce validation (zero side effects on rejection)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.channels import templates as tpl
from server.channels.mattermost import provider as prov
from server.channels.mattermost.client import FakeMattermostClient
from server.config import Settings
from server.db.engine import make_engine
from server.identity.principals import token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ADMIN = uuid.uuid4()
MEMBER = uuid.uuid4()
TOK_ADMIN, TOK_MEMBER = "svc-ch-admin", "svc-ch-member"
FAKE = FakeMattermostClient(users={"mm-u-admin": {"username": "admin"}})


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    prov.set_client_factory(lambda inst: FAKE)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ch', 'ch')"),
            {"i": WS},
        )
        repo = PostgresPolicyRepository()
        for acc, name, tok, perms in (
            (ADMIN, "acct-ch-admin", TOK_ADMIN, ["channel.manage", "task.*"]),
            (MEMBER, "acct-ch-member", TOK_MEMBER, ["task.read"]),
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
            repo.create_role(s, WS, f"ch-{name}", name)
            repo.commit_role_version(s, f"ch-{name}", perms, [], {}, ADMIN)
            repo.assign_role(s, acc, f"ch-{name}", ADMIN, dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    yield eng
    prov.set_client_factory(None)
    eng.dispose()


@pytest.fixture()
def client(database_url: str, engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=database_url, base_url="http://test"))
    from server.api.v1.channels import router as channels_router
    from server.api.v1.providers_mattermost import router as mm_router

    if not any(getattr(r, "path", "") == "/api/v1/channels/import" for r in app.routes):
        app.include_router(channels_router)
        app.include_router(mm_router)
        # the MCP app is mounted at the root; keep it last so the new routes stay reachable
        from starlette.routing import BaseRoute, Mount

        mounts: list[BaseRoute] = [r for r in app.router.routes if isinstance(r, Mount)]
        app.router.routes = [r for r in app.router.routes if r not in mounts] + mounts
    with TestClient(app) as c:
        yield c


def _h(tok: str, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}", "Idempotency-Key": key}


def test_templates_defaults_protected_and_custom_crud(client: TestClient, engine: Engine) -> None:
    listed = client.get("/api/v1/channel-templates", headers=_h(TOK_ADMIN, "x")).json()["items"]
    defaults = {t["template_id"] for t in listed if t["protected"]}
    assert defaults == {"work", "brainstorm", "approval", "ops"}
    for tid in ("work", "brainstorm", "approval", "ops"):
        r = client.delete(f"/api/v1/channel-templates/{tid}", headers=_h(TOK_ADMIN, f"del-{tid}"))
        assert r.status_code == 409 and r.json()["code"] == "TEMPLATE_PROTECTED"
    definition = dict(tpl.default_templates()["work"].definition)
    definition["retention_days"] = 30
    definition["task_domain"] = "research"
    r = client.post(
        "/api/v1/channel-templates",
        json={
            "template_id": "research-work",
            "name": "Research",
            "channel_type": "work",
            "definition": definition,
        },
        headers=_h(TOK_ADMIN, "tpl-1"),
    )
    assert r.status_code == 201, r.text
    bad = client.post(
        "/api/v1/channel-templates",
        json={
            "template_id": "bad",
            "name": "Bad",
            "channel_type": "work",
            "definition": {"retention_days": -1},
        },
        headers=_h(TOK_ADMIN, "tpl-bad"),
    )
    assert bad.status_code == 422 and bad.json()["code"] == "TEMPLATE_INVALID"
    denied = client.post(
        "/api/v1/channel-templates",
        json={"template_id": "nope", "name": "N", "channel_type": "work", "definition": definition},
        headers=_h(TOK_MEMBER, "tpl-denied"),
    )
    assert denied.status_code == 404  # normalized policy denial
    up = client.put(
        "/api/v1/channel-templates/research-work",
        json={"definition": {**definition, "retention_days": 45}},
        headers=_h(TOK_ADMIN, "tpl-up"),
    )
    assert up.status_code == 200 and up.json()["version"] == 2
    still = client.get("/api/v1/channel-templates", headers=_h(TOK_ADMIN, "x")).json()["items"]
    assert {t["template_id"] for t in still if t["protected"]} == {
        "work",
        "brainstorm",
        "approval",
        "ops",
    }
    assert tpl.default_templates()["work"].definition["retention_days"] == 365  # defaults untouched
    assert (
        client.delete(
            "/api/v1/channel-templates/research-work", headers=_h(TOK_ADMIN, "tpl-del")
        ).status_code
        == 200
    )


def test_channels_import_configure_independently_and_archive(
    client: TestClient, engine: Engine
) -> None:
    inst = client.post(
        "/api/v1/providers/mattermost/instances",
        json={
            "base_url": "http://mm-ch.test",
            "team_name": "colab-ch",
            "team_id": "team-ch",
            "bot_user_id": "bot-user",
        },
        headers=_h(TOK_ADMIN, "inst-1"),
    )
    assert inst.status_code == 201, inst.text
    pid = inst.json()["resource_id"]
    assert inst.json()["identity_display"] == "override"
    a = client.post(
        "/api/v1/channels/import",
        json={"provider_instance_id": pid, "external_channel_id": "ext-a", "channel_type": "work"},
        headers=_h(TOK_ADMIN, "imp-a"),
    )
    b = client.post(
        "/api/v1/channels/import",
        json={
            "provider_instance_id": pid,
            "external_channel_id": "ext-b",
            "channel_type": "custom",
        },
        headers=_h(TOK_ADMIN, "imp-b"),
    )
    assert a.status_code == 201 and b.status_code == 201, (a.text, b.text)
    ca, cb = a.json()["resource_id"], b.json()["resource_id"]
    assert a.json()["template_id"] == "work" and b.json()["template_id"] is None
    got_a = client.get(f"/api/v1/channels/{ca}", headers=_h(TOK_ADMIN, "x")).json()
    assert got_a["retention_days"] == 365 and got_a["policy"]["task_domain"] == "general"
    conf = client.post(
        f"/api/v1/channels/{ca}/configure",
        json={"retention_days": 30, "language": "ko", "policy": {"task_domain": "research"}},
        headers=_h(TOK_ADMIN, "conf-a"),
    )
    assert conf.status_code == 200, conf.text
    got_a = client.get(f"/api/v1/channels/{ca}", headers=_h(TOK_ADMIN, "x")).json()
    got_b = client.get(f"/api/v1/channels/{cb}", headers=_h(TOK_ADMIN, "x")).json()
    assert (
        got_a["retention_days"] == 30
        and got_a["language"] == "ko"
        and got_a["policy"]["task_domain"] == "research"
    )
    assert got_b["retention_days"] == 365 and got_b["language"] is None and got_b["policy"] == {}
    bad = client.post(
        f"/api/v1/channels/{ca}/configure",
        json={"policy": {"retention_days": "x"}},
        headers=_h(TOK_ADMIN, "conf-bad"),
    )
    assert bad.status_code == 422 and bad.json()["code"] == "TEMPLATE_INVALID"
    denied = client.post(
        f"/api/v1/channels/{ca}/configure",
        json={"retention_days": 1},
        headers=_h(TOK_MEMBER, "conf-denied"),
    )
    assert denied.status_code == 404
    arch = client.post(f"/api/v1/channels/{cb}/archive", headers=_h(TOK_ADMIN, "arch-b"))
    assert arch.status_code == 200
    assert (
        client.get(f"/api/v1/channels/{cb}", headers=_h(TOK_ADMIN, "x")).json()["status"]
        == "archived"
    )
    with Session(engine) as s:
        types = [
            r[0]
            for r in s.execute(
                text(
                    "SELECT type FROM events WHERE aggregate_type = 'channel' AND workspace_id = "
                    ":w ORDER BY recorded_seq"
                ),
                {"w": WS},
            ).all()
        ]
    assert types == [
        "CHANNEL_CONFIGURED",
        "CHANNEL_CONFIGURED",
        "CHANNEL_CONFIGURED",
        "CHANNEL_ARCHIVED",
    ]


def test_slash_endpoint_validates_token_and_nonce_before_any_side_effect(
    client: TestClient, engine: Engine
) -> None:
    inst = client.post(
        "/api/v1/providers/mattermost/instances",
        json={
            "base_url": "http://mm2.test",
            "team_name": "t2",
            "team_id": "team-slash",
            "bot_user_id": "bot-user",
        },
        headers=_h(TOK_ADMIN, "inst-2"),
    ).json()
    reg = client.post(
        "/api/v1/providers/mattermost/commands/register",
        json={
            "provider_instance_id": inst["resource_id"],
            "callback_url": "http://test/api/v1/providers/mattermost/commands",
        },
        headers=_h(TOK_ADMIN, "reg-2"),
    )
    assert reg.status_code == 201, reg.text
    token = FAKE.commands[-1]["token"]
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT count(*) FROM provider_command_tokens WHERE token_hash = :h"),
                {"h": prov.token_hash(token)},
            ).scalar_one()
            == 1
        )
        before = s.execute(text("SELECT count(*) FROM events")).scalar_one()
    form = {
        "token": "wrong",
        "team_id": "team-slash",
        "channel_id": "ext-x",
        "user_id": "mm-u-admin",
        "user_name": "admin",
        "command": "/colab",
        "text": 'task create "x" --criteria "y"',
        "trigger_id": "t-1",
    }
    r = client.post("/api/v1/providers/mattermost/commands", data=form)
    assert r.status_code == 401 and r.json()["code"] == "CALLBACK_SIGNATURE_INVALID"
    r2 = client.post(
        "/api/v1/providers/mattermost/commands",
        data={**form, "team_id": "team-unknown", "token": token},
    )
    assert r2.status_code == 403 and r2.json()["code"] == "PROVIDER_INSTANCE_UNKNOWN"
    ok = client.post("/api/v1/providers/mattermost/commands", data={**form, "token": token})
    assert (
        ok.status_code == 200 and ok.json()["response_type"] == "ephemeral"
    )  # unlinked user → guidance
    assert "link" in ok.json()["text"].lower()
    replay = client.post("/api/v1/providers/mattermost/commands", data={**form, "token": token})
    assert replay.status_code == 403 and replay.json()["code"] == "CALLBACK_NONCE_REUSED"
    with Session(engine) as s:
        assert s.execute(text("SELECT count(*) FROM events")).scalar_one() == before
