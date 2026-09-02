"""P2-01: Mattermost client (HTTP via a mocked transport, fake client, WS frame normalization)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from server.channels.mattermost.client import (
    FakeMattermostClient,
    HttpMattermostClient,
    MattermostError,
)
from server.channels.mattermost.provider import detect_identity_display
from server.channels.mattermost.websocket import normalize, ws_url


def _transport(calls: list[tuple[str, str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, body))
        assert request.headers["Authorization"].startswith("Bearer ")
        if request.url.path == "/api/v4/users/me":
            return httpx.Response(200, json={"id": "bot-1", "username": "agent-colab"})
        if request.url.path == "/api/v4/posts" and request.method == "POST":
            return httpx.Response(201, json={"id": "p1", **body})
        if request.url.path.endswith("/patch"):
            return httpx.Response(
                200, json={"id": "p1", "channel_id": "c", "user_id": "bot-1", **body}
            )
        if request.url.path == "/api/v4/commands":
            return httpx.Response(201, json={"id": "cmd1", "token": "tok", **body})
        if request.url.path == "/api/v4/posts/ephemeral":
            return httpx.Response(200, json={"id": "e1"})
        if request.url.path == "/api/v4/channels/c/posts":
            return httpx.Response(
                200,
                json={
                    "order": ["b", "a"],
                    "posts": {
                        "a": {"id": "a", "channel_id": "c", "user_id": "u", "message": "1"},
                        "b": {
                            "id": "b",
                            "channel_id": "c",
                            "user_id": "u",
                            "message": "2",
                            "root_id": "a",
                        },
                    },
                },
            )
        if request.url.path == "/api/v4/users/username/nobody":
            return httpx.Response(
                404, json={"id": "store.sql_user.get_by_username.app_error", "message": "missing"}
            )
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


def test_http_client_paths_bodies_and_errors() -> None:
    calls: list[tuple[str, str, Any]] = []
    client = HttpMattermostClient(
        "http://mm.test/", "secret-token", httpx.Client(transport=_transport(calls))
    )
    assert "secret" not in repr(client)
    assert client.me()["id"] == "bot-1"
    post = client.create_post("c", "hello", root_id="r", props={"x": 1})
    assert post.id == "p1" and post.root_id == "r" and post.props == {"x": 1}
    patched = client.patch_post("p1", "edited")
    assert patched.message == "edited"
    cmd = client.create_command("team", "colab", "http://cb/commands")
    assert (
        cmd["token"] == "tok"
        and calls[-1][2]["method"] == "P"
        and calls[-1][2]["trigger"] == "colab"
    )
    client.ephemeral("u", "c", "psst")
    posts = client.list_posts("c")
    assert [p.id for p in posts] == ["b", "a"] and posts[0].root_id == "a"
    with pytest.raises(MattermostError) as exc:
        client.get_user_by_username("@nobody")
    assert exc.value.status == 404


def test_fake_client_records_and_threads() -> None:
    fake = FakeMattermostClient(users={"u1": {"username": "alice"}})
    root = fake.create_post("c", "card")
    reply = fake.create_post("c", "reply", root_id=root.id)
    assert reply.root_id == root.id and fake.calls[-1][0] == "create_post"
    assert fake.get_user_by_username("@alice")["id"] == "u1"
    cmd = fake.create_command("t", "colab", "http://x")
    assert fake.regen_command_token(cmd["id"])["token"].endswith("-rotated")
    fake.ephemeral("u1", "c", "hi")
    assert (
        fake.ephemeral_count()
        if hasattr(fake, "ephemeral_count")
        else fake.ephemerals == [("u1", "c", "hi")]
    )


def test_identity_display_detection_follows_the_spike_rule() -> None:
    both = {"ServiceSettings": {"EnablePostUsernameOverride": True, "EnablePostIconOverride": True}}
    assert detect_identity_display(both) == "override"
    assert (
        detect_identity_display({"ServiceSettings": {"EnablePostUsernameOverride": True}})
        == "prefix"
    )
    assert detect_identity_display({}) == "prefix"
    assert detect_identity_display(None) == "prefix"


def test_websocket_url_and_frame_normalization() -> None:
    assert ws_url("https://mm.example.com/") == "wss://mm.example.com/api/v4/websocket"
    assert ws_url("http://127.0.0.1:8065") == "ws://127.0.0.1:8065/api/v4/websocket"
    frame = {
        "event": "posted",
        "seq": 4,
        "broadcast": {"channel_id": "c"},
        "data": {
            "post": json.dumps({"id": "p", "user_id": "u", "channel_id": "c", "message": "m"}),
            "channel_type": "O",
        },
    }
    evt = normalize(frame)
    assert (
        evt is not None
        and evt["event"] == "posted"
        and evt["post"]["id"] == "p"
        and evt["user_id"] == "u"
    )
    assert normalize({"event": "typing", "data": {}}) is None
    assert normalize({"event": "post_edited", "data": {"post": "not-json"}})["post"] is None  # type: ignore[index]
