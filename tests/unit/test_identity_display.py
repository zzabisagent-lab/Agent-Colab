"""P2-14 unit rules: override vs prefix modes, injected identity stripped."""

from __future__ import annotations

import uuid

from server.channels.identity_display import (
    apply_display,
    display_for_agent,
    strip_injected_identity,
)
from server.channels.mattermost.client import FakeMattermostClient
from server.channels.mattermost.delivery import MattermostChannelProvider
from server.channels.mattermost.provider import ProviderInstance


def _provider(mode: str) -> ProviderInstance:
    return ProviderInstance(
        id=uuid.uuid4(),
        provider_instance_id=f"mm:test:{mode}",
        workspace_id=uuid.uuid4(),
        base_url="http://mm",
        team_id="team-1",
        team_name="colab-test",
        bot_user_id="bot",
        identity_display=mode,
        status="active",
    )


def test_override_mode_sets_props_only() -> None:
    identity = display_for_agent(_provider("override"), "Research Agent", "http://icon")
    out = apply_display({"message": "hello"}, identity)
    assert out["message"] == "hello"
    assert out["props"]["override_username"] == "Research Agent"
    assert out["props"]["override_icon_url"] == "http://icon"


def test_prefix_mode_prefixes_exactly_once() -> None:
    identity = display_for_agent(_provider("prefix"), "Research Agent")
    out = apply_display({"message": "hello"}, identity)
    assert out["message"] == "[Research Agent] hello" and "props" not in out
    again = apply_display(out, identity)
    assert again["message"] == "[Research Agent] hello"


def test_injected_identity_is_stripped_everywhere() -> None:
    payload = {
        "message": "x",
        "override_username": "admin",
        "display_name": "root",
        "props": {"override_icon_url": "http://evil", "keep": 1},
    }
    clean, removed = strip_injected_identity(payload)
    assert removed == ["display_name", "override_username", "props.override_icon_url"]
    assert clean["props"] == {"keep": 1} and "override_username" not in clean


def test_delivery_provider_applies_server_identity_and_ignores_payload_identity() -> None:
    for mode in ("override", "prefix"):
        client = FakeMattermostClient()
        provider = MattermostChannelProvider(client, _provider(mode))
        post_id = provider.deliver(
            "mattermost:chan-1",
            {
                "message": "done",
                "agent_display_name": "Research Agent",
                "props": {"override_username": "evil"},
                "dedupe_key": "k1",
            },
        )
        post = client.posts[post_id]
        if mode == "override":
            assert post.props["override_username"] == "Research Agent" and post.message == "done"
        else:
            assert post.message == "[Research Agent] done" and "override_username" not in post.props
        assert provider.injections == [("Research Agent", ["props.override_username"])]
        # idempotent per dedupe key: a second delivery returns the same post without a new call
        assert (
            provider.deliver("mattermost:chan-1", {"message": "done", "dedupe_key": "k1"})
            == post_id
        )
        assert len(client.posts) == 1
