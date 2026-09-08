import json
from pathlib import Path

import pytest

from server.connections import agent_instructions, provider_instructions
from server.runner import KINDS


@pytest.mark.parametrize("kind", list(KINDS))
def test_agent_instructions(kind):
    data = agent_instructions("http://192.168.20.233:8000", "agent-test", "acct-test", "mcp", kind)
    text = json.dumps(data)
    for required in [
        "agent-test",
        "acct-test",
        "mcp",
        kind,
        "AGENT_COLAB_BASE_URL",
        "AGENT_COLAB_SERVICE_TOKEN",
        "AGENT_COLAB_WORKDIR",
        "AGENT_COLAB_RUNNER_KIND",
        "--once",
        "EnvironmentFile",
        "/api/v1/work/poll",
        "heartbeat",
    ]:
        assert required in text
    assert data["env"]["AGENT_COLAB_SERVICE_TOKEN"] == "<one-time service token>"


def test_providers_concrete():
    guides = provider_instructions("http://192.168.20.233:8000")
    mm, tg = json.dumps(guides["mattermost"]), json.dumps(guides["telegram"])
    for value in [
        "AGENT_COLAB_MATTERMOST_BOT_TOKEN",
        "AGENT_COLAB_MATTERMOST_ADMIN_TOKEN",
        "team_id",
        "/channels/import",
        "http://192.168.20.233:8000/api/v1/providers/mattermost/commands",
        "/actions",
        "provider_command_tokens",
    ]:
        assert value in mm
    for value in [
        "BotFather",
        "TELEGRAM_BOT_TOKEN",
        "AGENT_COLAB_TELEGRAM_WEBHOOK_SECRET",
        "telegram_chat_id",
        "telegram_thread_id",
        "bidirectional",
        "fixed_topic",
        "/test",
        "/api/v1/providers/telegram/updates/",
        "polling",
    ]:
        assert value in tg


def test_ui_exposes_connections_and_one_time_token():
    text = Path("web-admin/src/features/agents/AgentsPage.tsx").read_text()
    assert "connection-instructions" in text
    assert "service_token" in text
    assert "Dismiss token" in text
    assert (
        "<ProviderConnections />"
        in Path("web-admin/src/features/channels/ChannelsPage.tsx").read_text()
    )
    assert (
        "<ProviderConnections />"
        in Path("web-admin/src/features/bridges/BridgesPage.tsx").read_text()
    )
