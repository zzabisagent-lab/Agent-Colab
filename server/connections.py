"""Secret-free connection recipes shared by API and Web Admin.

B105 exceptions below are prose instructions and placeholders, never credentials.
"""

from typing import Any

from server.agents.runner_catalog import KINDS


def agent_instructions(
    base: str, agent_id: str, account_id: str, adapter: str, kind: str
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("unsupported runner kind")
    base = base.rstrip("/")
    return {
        "agent_id": agent_id,
        "account_id": account_id,
        "adapter_type": adapter,
        "runner_kind": kind,
        "creates": (
            "Agent-Colab creates an Agent, Account and one-time service token. Use mcp/pull."
        ),
        "host_stores": "Runner host stores a mode 0600 env file and local CLI authentication.",
        "env": {
            "AGENT_COLAB_BASE_URL": base,
            "AGENT_COLAB_AGENT_ID": agent_id,
            "AGENT_COLAB_SERVICE_TOKEN": "<one-time service token>",  # nosec B105
            "AGENT_COLAB_WORKDIR": "/srv/agent-work",
            "AGENT_COLAB_RUNNER_KIND": kind,
        },
        "command_override": kind.upper().replace("-", "_") + "_COMMAND",
        "default_command": list(KINDS[kind]),
        "command_contract": (
            "Override is shell-quoted argv, no shell expansion. Read prompt "
            "from stdin; emit report to stdout; nonzero exit means failure. "
            "agent-colab-* executables are operator-provided wrappers."
        ),
        "mcp_url": base + "/mcp",
        "rest_poll_url": base + "/api/v1/work/poll",
        "manual": (
            "set -a; . /etc/agent-colab/runner.env; set +a; "
            "/opt/agent-colab/.venv/bin/python -m server.runner --once"
        ),
        "systemd": (
            "[Unit]\nDescription=Agent-Colab "
            "runner\nAfter=network-online.target\n[Service]\nUser=agent-runner\nW"
            "orkingDirectory=/opt/agent-colab\nEnvironmentFile=/etc/agent-cola"
            "b/runner.env\nExecStart=/opt/agent-colab/.venv/bin/python -m "
            "server.runner\nRestart=on-failure\nRestartSec=10\nUMask=0077\n[Insta"
            "ll]\nWantedBy=multi-user.target\n"
        ),
        "verify": (
            "Activate the Agent with a role granting work.poll. Run "
            "--print-config, then --once. Inspect heartbeat/online in Agents "
            "and GET /api/v1/work/{work_item_id} for status and result "
            "receipts. Assign a Task to this Agent to exercise poll/result."
        ),
    }


def provider_instructions(base: str) -> dict[str, Any]:
    base = base.rstrip("/")
    return {
        "mattermost": {
            "values": {
                "AGENT_COLAB_MATTERMOST_URL": "Mattermost origin, e.g. http://192.168.20.233:8065",
                "team_name": (
                    "Team URL slug; team_id: Mattermost team ID from GET "
                    "/api/v4/teams/name/{team_name}"
                ),
                "AGENT_COLAB_MATTERMOST_BOT_TOKEN": (  # nosec B105
                    "Create bot under Integrations > Bot Accounts; store token in "
                    "server mode 0600 environment file."
                ),
                "AGENT_COLAB_MATTERMOST_ADMIN_TOKEN": (  # nosec B105
                    "Command-management credential from Mattermost personal access "
                    "tokens; server env only; optional if bot can manage commands."
                ),
                "slash_token": (  # nosec B105
                    "POST commands/register obtains/rotates the slash command token "
                    "automatically; only its hash is stored in "
                    "provider_command_tokens. No slash-token env key is consumed."
                ),
            },
            "command_callback_url": base + "/api/v1/providers/mattermost/commands",
            "action_callback_url": base + "/api/v1/providers/mattermost/actions",
            "register": (
                "POST /api/v1/providers/mattermost/instances with base_url, "
                "team_name, team_id, bot_user_id. Save resource_id."
            ),
            "command": (
                "POST /api/v1/providers/mattermost/commands/register with "
                "provider_instance_id and callback_url = command_callback_url. "
                "Trigger defaults to colab."
            ),
            "import": (
                "POST /api/v1/channels/import with provider_instance_id, "
                "external_channel_id (Mattermost Channel Info ID), "
                "channel_type=work."
            ),
            "test": (
                "In imported channel run /colab help. For actions, create a Task "
                "card and use its button. Mattermost must reach both callback "
                "URLs on the private network."
            ),
            "runbook": (
                "docs/runbooks/connections.md includes authenticated Python "
                "requests and inbound WebSocket wiring limitations."
            ),
        },
        "telegram": {
            "values": {
                "TELEGRAM_BOT_TOKEN": (  # nosec B105
                    "Create bot with @BotFather /newbot. Store only in server/relay "
                    "mode 0600 env file."
                ),
                "AGENT_COLAB_TELEGRAM_WEBHOOK_SECRET": (  # nosec B105
                    "Operator-generated random webhook secret, stored in server env; "
                    "supply same value to setWebhook secret_token."
                ),
                "telegram_chat_id": (
                    "Signed chat.id from getUpdates after sending a message to the bot/group."
                ),
                "telegram_thread_id": (
                    "message_thread_id from a forum topic message; null for general (never 1)."
                ),
            },
            "mode": (
                "Use polling on private networks; Telegram cloud cannot reach a "
                "192.168.20.233 HTTP webhook. Webhook mode requires a reachable "
                "HTTPS ingress."
            ),
            "callback_url": base + "/api/v1/providers/telegram/updates/{provider_instance_id}",
            "mapping": {
                "provider_instance_id": "tg:<bot-id>",
                "telegram_chat_id": "<signed chat id>",
                "telegram_thread_id": None,
                "direction": "bidirectional",
                "thread_mode": "general",
            },
            "thread_modes": (
                "general, fixed_topic (requires telegram_thread_id), "
                "topic_per_root (forum + bot Manage Topics permission)"
            ),
            "directions": "mattermost_to_telegram, telegram_to_mattermost, bidirectional",
            "create": (
                "POST /api/v1/channels/{channel_id}/bridges with mapping fields. "
                "Save resource_id; POST "
                "/api/v1/channels/{channel_id}/bridges/{bridge_id}/enable."
            ),
            "test": (
                "POST /api/v1/channels/{channel_id}/bridges/{bridge_id}/test, "
                "then GET the same path ending /status. Send a Telegram message "
                "and confirm it arrives in the imported Mattermost channel."
            ),
            "runbook": (
                "docs/runbooks/connections.md provides provider registration and "
                "durable-offset polling commands. Only one polling process per "
                "bot; remove webhook first."
            ),
        },
    }
