"""Live Bridge check against the real Telegram Bot API (skipped only when `.env` lacks keys):
one Mattermost→Telegram relay delivered through the outbox drain with the real client, the
mapping completed with the real topic/message ids, then cleanup."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.channels.outbox import RecordingChannelProvider
from server.channels.telegram.bridge import Bridge, MattermostPostView, TelegramBridgeProvider
from server.channels.telegram.client import HttpTelegramClient
from server.db.engine import make_engine
from server.domain.clock import FixedClock

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


WS = uuid.uuid4()
ADMIN = uuid.uuid4()
MM_PI = uuid.uuid4()
CHAN = uuid.uuid4()


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-brl', 'brl')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-brl', :w, 'human', 'brl')"
            ),
            {"i": ADMIN, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "base_url, team_or_bot_ref, bot_user_id) VALUES (:i, 'mm:live:colab', :w, "
                "'mattermost', "
                "'http://mm', 'colab', 'mmbot')"
            ),
            {"i": MM_PI, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                "external_channel_id, channel_type, display_name) VALUES (:i, 'chan-brl', :w, :pi, "
                "'ext-live', 'work', 'live')"
            ),
            {"i": CHAN, "w": WS, "pi": MM_PI},
        )
    yield eng
    eng.dispose()


@pytest.mark.skipif(
    not _load_env().get("TELEGRAM_BOT_TOKEN"), reason="TELEGRAM_BOT_TOKEN not configured in .env"
)
def test_live_relay_creates_topic_and_completes_mapping(engine: Engine) -> None:
    env = _load_env()
    client = HttpTelegramClient(env["TELEGRAM_BOT_TOKEN"])
    bot_id = str(client.get_me()["id"])
    chat_a = env["TELEGRAM_TEST_CHAT_A"]
    clock = FixedClock(dt.datetime.now(dt.UTC))
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO telegram_bridges (id, bridge_id, workspace_id, channel_id, "
                "provider_instance_id, "
                "telegram_chat_id, direction, created_by) VALUES (:i, 'bridge-live', :w, :c, :pi, "
                ":chat, "
                "'bidirectional', :by)"
            ),
            {
                "i": uuid.uuid4(),
                "w": WS,
                "c": CHAN,
                "pi": f"tg:{bot_id}",
                "chat": chat_a,
                "by": ADMIN,
            },
        )
    bridge = Bridge()
    tg = TelegramBridgeProvider(client)
    mm = RecordingChannelProvider(prefix="mattermost")
    created_thread: int | None = None
    sent_id: int | None = None
    try:
        with Session(engine) as s, s.begin():
            post = MattermostPostView(
                "mm:live:colab", "ext-live", "live-post-1", None, "u1", "alice", "live bridge relay"
            )
            out = bridge.on_mattermost_post(s, clock, post)
            assert out[0].accepted
            bridge.deliver(s, {"telegram": tg, "mattermost": mm}, clock, str(WS))
            row = s.execute(
                text(
                    "SELECT delivery_status, tg_message_id, tg_thread_id FROM message_mappings "
                    "WHERE bridge_id = 'bridge-live'"
                )
            ).first()
            assert row is not None and row[0] == "sent" and row[1] and row[2]
            sent_id, created_thread = int(row[1]), int(row[2])
            # the mapped topic id must equal the created forum topic's thread id
            assert created_thread == tg.topics[out[0].dedupe_key or ""]
    finally:
        if sent_id is not None:
            client.delete_message(chat_a, sent_id)
        if created_thread is not None:
            client.delete_forum_topic(chat_a, created_thread)
