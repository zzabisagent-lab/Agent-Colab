"""The gateway's Mattermost provider must deliver every payload kind, not only new posts.

A card update is a ``patch`` payload. Wired to the post-only bridge provider, the gateway posted a
second message instead of editing the card, so a status change appeared as a duplicate card in the
channel. This pins the resolved provider to the full delivery path (V-P7-02, V-P7-22).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from server.channels.gateway import LazyMattermostProvider
from server.channels.mattermost.client import Post
from server.db.engine import make_engine

pytestmark = pytest.mark.db

WS = uuid.uuid4()
PI = uuid.uuid4()
PI_ID = "mm:gw-test:colab-gw"
EXT = "chan-gw-ext"


@dataclass
class RecordingClient:
    posts: list[tuple[str, str]] = field(default_factory=list)
    patches: list[tuple[str, str]] = field(default_factory=list)
    ephemerals: list[tuple[str, str]] = field(default_factory=list)
    dms: list[tuple[str, str]] = field(default_factory=list)

    def create_post(
        self,
        channel_id: str,
        message: str,
        root_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> Post:
        self.posts.append((channel_id, message))
        return Post(
            id=f"post-{len(self.posts)}", channel_id=channel_id, user_id="bot", message=message
        )

    def patch_post(self, post_id: str, message: str, props: dict[str, Any] | None = None) -> Post:
        self.patches.append((post_id, message))
        return Post(id=post_id, channel_id=EXT, user_id="bot", message=message)

    def ephemeral(self, user_id: str, channel_id: str, message: str) -> None:
        self.ephemerals.append((user_id, message))

    def direct_message(self, user_id: str, message: str) -> Post:
        self.dms.append((user_id, message))
        return Post(id=f"dm-{len(self.dms)}", channel_id="dm", user_id="bot", message=message)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-gw', 'gw')"),
            {"i": WS},
        )
    yield eng
    with eng.begin() as c:
        c.execute(text("DELETE FROM provider_instances WHERE workspace_id = :w"), {"w": WS})
        c.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": WS})
    eng.dispose()


def _register_instance(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, "
                "provider, base_url, team_or_bot_ref, identity_display, status, config) VALUES "
                "(:i, :p, :w, 'mattermost', 'http://mm', 'team-gw', 'prefix', 'active', "
                '\'{"team_name": "colab-gw"}\') ON CONFLICT (id) DO NOTHING'
            ),
            {"i": PI, "p": PI_ID, "w": WS},
        )


def test_a_card_update_patches_the_card_instead_of_posting_again(engine: Engine) -> None:
    _register_instance(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    client = RecordingClient()
    provider = LazyMattermostProvider(client, factory)

    first = provider.deliver(f"mattermost:{EXT}", {"message": "card v1", "dedupe_key": "gw-1"})
    updated = provider.deliver(
        f"mattermost:{EXT}",
        {"message": "card v2", "post_id": first, "dedupe_key": "gw-2"},
    )

    assert client.posts == [(EXT, "card v1")]
    assert client.patches == [(first, "card v2")]
    assert updated == first


def test_ephemeral_and_direct_message_payloads_reach_their_own_calls(engine: Engine) -> None:
    _register_instance(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    client = RecordingClient()
    provider = LazyMattermostProvider(client, factory)

    provider.deliver(
        f"mattermost:{EXT}",
        {"message": "only you", "ephemeral": True, "user_id": "u-1", "dedupe_key": "gw-3"},
    )
    provider.deliver(
        f"mattermost:{EXT}", {"message": "hello", "dm_user_id": "u-2", "dedupe_key": "gw-4"}
    )

    assert client.ephemerals == [("u-1", "only you")]
    assert client.dms == [("u-2", "hello")]
    assert client.posts == []


def test_without_a_registered_instance_it_still_posts_and_retries_the_lookup(
    engine: Engine,
) -> None:
    """Startup order must not break delivery: post-only until an instance exists, then full."""
    with engine.begin() as c:
        c.execute(text("DELETE FROM provider_instances WHERE workspace_id = :w"), {"w": WS})
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    client = RecordingClient()
    provider = LazyMattermostProvider(client, factory)

    provider.deliver(f"mattermost:{EXT}", {"message": "before", "dedupe_key": "gw-5"})
    assert client.posts == [(EXT, "before")]

    _register_instance(engine)
    post_id = provider.deliver(f"mattermost:{EXT}", {"message": "after", "dedupe_key": "gw-6"})
    provider.deliver(
        f"mattermost:{EXT}", {"message": "after v2", "post_id": post_id, "dedupe_key": "gw-7"}
    )
    assert client.patches == [(post_id, "after v2")]
