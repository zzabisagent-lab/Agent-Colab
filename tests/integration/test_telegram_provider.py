"""P2-04 integration: webhook spoof/replay/stale against the real app (V-P2-09) and, when the
`.env` keys exist, real Bot API sends/edits/deletes plus notification-provider idempotency."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.v1.providers_telegram import router as telegram_router
from server.channels.telegram.client import HttpTelegramClient
from server.channels.telegram.intake import InboundMessage
from server.channels.telegram.provider import TelegramNotificationProvider, provider_instance_id
from server.config import Settings
from server.db.engine import make_engine
from server.main import create_app

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
WS = uuid.uuid4()
PI = "tg:424242"
SECRET = "webhook-secret-for-tests-only"
FIXTURES = json.loads(
    (ROOT / "tests" / "fixtures" / "telegram" / "updates-samples.json").read_text()
)


def _load_env() -> dict[str, str]:
    """Read `.env` without ever printing values (tests only)."""
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-tg', 'tg')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "team_or_bot_ref) VALUES (:i, :p, :w, 'telegram', 'bot 424242')"
            ),
            {"i": uuid.uuid4(), "p": PI, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "team_or_bot_ref, status) VALUES "
                "(:i, 'tg:999', :w, 'telegram', 'disabled bot', 'disabled')"
            ),
            {"i": uuid.uuid4(), "w": WS},
        )
    yield eng
    eng.dispose()


@pytest.fixture()
def client(database_url: str, engine: Engine) -> Iterator[tuple[TestClient, list[InboundMessage]]]:
    from server.domain.clock import FixedClock

    app = create_app(Settings(database_url=database_url, base_url="http://test"))
    if not any(getattr(r, "path", "").startswith("/api/v1/providers/telegram") for r in app.routes):
        # create_app mounts the MCP app at "/" last; routers added afterwards must precede it
        app.include_router(telegram_router)
        mounts = [r for r in app.router.routes if getattr(r, "name", "") == "mcp"]
        for m in mounts:
            app.router.routes.remove(m)
            app.router.routes.append(m)
    received: list[InboundMessage] = []
    app.state.telegram_webhook_secret = SECRET
    app.state.telegram_inbound_handler = received.append
    # fixture updates are dated 2026-01-10T00:26:40Z; pin the clock inside the tolerance
    app.state.runtime.clock = FixedClock(dt.datetime.fromtimestamp(1768000000, tz=dt.UTC))
    with TestClient(app) as c:
        yield c, received


def _post(
    c: TestClient,
    update: dict[str, Any],
    *,
    secret: str | None = SECRET,
    pi: str = PI,
    body_hash: str | None = None,
) -> Any:
    body = json.dumps(update).encode()
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    if body_hash is not None:
        headers["X-Colab-Body-SHA256"] = body_hash
    return c.post(f"/api/v1/providers/telegram/updates/{pi}", content=body, headers=headers)


def _events(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": WS}
            ).scalar_one()
        )


def test_webhook_spoof_replay_and_stale_have_no_side_effects(
    client: tuple[TestClient, list[InboundMessage]], engine: Engine
) -> None:
    c, received = client
    before = _events(engine)
    update = FIXTURES["topic_message"]
    assert _post(c, update, secret=None).status_code == 401
    r = _post(c, update, secret="wrong-secret")
    assert r.status_code == 401 and r.json()["code"] == "CALLBACK_SIGNATURE_INVALID"
    r = _post(c, update, body_hash="0" * 64)
    assert r.status_code == 401 and r.json()["code"] == "CALLBACK_BODY_HASH_MISMATCH"
    r = _post(c, update, pi="tg:999")
    assert r.status_code == 404 and r.json()["code"] == "PROVIDER_INSTANCE_UNKNOWN"
    r = _post(c, update, pi="tg:unknown")
    assert r.status_code == 404
    stale = json.loads(json.dumps(update))
    stale["update_id"] = 900101
    stale["message"]["date"] = 1768000000 - 3600
    r = _post(c, stale)
    assert r.status_code == 403 and r.json()["code"] == "CALLBACK_TIMESTAMP_EXPIRED"
    r = _post(c, FIXTURES["invalid_missing_chat"])
    assert r.status_code == 400 and r.json()["code"] == "TELEGRAM_UPDATE_INVALID"
    assert received == [] and _events(engine) == before
    # a valid update is handled exactly once; its replay is acknowledged without a second handling
    good_hash = hashlib.sha256(json.dumps(update).encode()).hexdigest()
    r = _post(c, update, body_hash=good_hash)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    r = _post(c, update)
    assert r.status_code == 200 and r.json()["status"] == "replayed"
    assert len(received) == 1 and received[0].message_thread_id == 3
    r = _post(c, FIXTURES["membership"])
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert _events(engine) == before  # the intake never creates domain Events
    with Session(engine) as s:
        n = s.execute(
            text("SELECT count(*) FROM telegram_update_receipts WHERE provider_instance_id = :p"),
            {"p": PI},
        ).scalar_one()
    assert n == 1


@pytest.mark.skipif(
    not _load_env().get("TELEGRAM_BOT_TOKEN"), reason="TELEGRAM_BOT_TOKEN not configured in .env"
)
def test_real_bot_api_send_edit_delete_and_provider_idempotency() -> None:
    env = _load_env()
    token, chat_a = env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_TEST_CHAT_A"]
    api = HttpTelegramClient(token)
    me = api.get_me()
    assert me["is_bot"] and provider_instance_id(str(me["id"])).startswith("tg:")
    stamp = dt.datetime.now(dt.UTC).strftime("%H%M%S")
    general = api.send_message(chat_a, f"[agent-colab P2-04 test {stamp}] general")
    assert general.message_thread_id is None
    topic = api.create_forum_topic(chat_a, f"agent-colab-p2-04-{stamp}")
    try:
        first = api.send_message(chat_a, "topic message", message_thread_id=topic.message_thread_id)
        assert first.message_thread_id == topic.message_thread_id
        reply = api.send_message(
            chat_a,
            "reply",
            message_thread_id=topic.message_thread_id,
            reply_to_message_id=first.message_id,
        )
        assert reply.message_thread_id == topic.message_thread_id
        edited = api.edit_message_text(chat_a, reply.message_id, "reply (edited)")
        assert edited.text == "reply (edited)"
        provider = TelegramNotificationProvider(api)
        dest = f"telegram:{chat_a}:{topic.message_thread_id}"
        payload = {"text": "notification", "dedupe_key": f"test-{stamp}"}
        provider.send(dest, payload)
        provider.send(dest, payload)
        assert len(provider.delivered) == 1
        for mid in (first.message_id, reply.message_id, provider.delivered[payload["dedupe_key"]]):
            assert api.delete_message(chat_a, mid)
        assert api.delete_message(chat_a, general.message_id)
    finally:
        api.close_forum_topic(chat_a, topic.message_thread_id)
        api.delete_forum_topic(chat_a, topic.message_thread_id)
    assert token not in repr(api)
    _ = os.environ  # values are never printed
