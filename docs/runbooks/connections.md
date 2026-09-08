# Agent-Colab connections

This slice uses installed CLIs or operator-provided commands. It creates no vendor accounts,
OAuth sessions, or API keys. Prerequisites P3-01/03/04/10 and P2-01/04/05 are IMPLEMENTED in
PROGRESS.md. Work/result exchange uses the existing inbox; role names remain operator-defined.

## Register and run an Agent

1. Install this checkout and its dependencies on the runner host (Python 3.12, `uv sync`).
   The examples assume `/opt/agent-colab`, with a writable work directory `/srv/agent-work`.
2. In Web Admin → Agents, register an Agent with adapter `mcp` and pull delivery. Give its
   Account an operator-defined role granting `work.poll` and `agent.self`, plus the permissions
   needed for its assigned Tasks. Agent-Colab creates the Agent, Account, inbox identity and
   service credential. Copy the **one-time service token**, then select **Dismiss token**.
   It is never recovered by connection instructions. If lost, rotate credentials through Account
   administration. Test connection and Activate before starting the runner.
3. Select one of the seven runner kinds, then **Connection instructions** on the Agent row.
   The authenticated API is `GET /api/v1/agents/{agent_id}/connection-instructions?runner_kind=codex`.
   It supplies account_id, agent_id, adapter, URLs, env placeholders and a systemd unit.
4. On the runner host, create `/etc/agent-colab/runner.env`, owned by the runner user, mode 0600.
   Replace placeholders locally in an editor; do not paste credentials into terminal commands,
   shell history, tickets, screenshots or docs. Do not use `set -x`.

```dotenv
AGENT_COLAB_BASE_URL=http://192.168.20.233:8080
AGENT_COLAB_AGENT_ID=agent-example
AGENT_COLAB_SERVICE_TOKEN=<one-time service token>
AGENT_COLAB_WORKDIR=/srv/agent-work
AGENT_COLAB_RUNNER_KIND=codex
AGENT_COLAB_RUNNER_TIMEOUT=120
```

Configuration is environment-only. `AGENT_COLAB_BASE_URL` takes precedence over
`AGENT_COLAB_MCP_URL`; the latter must end in `/mcp` and is converted to the REST base.
Command-specific env overrides take precedence over the defaults below. The timeout is in seconds.
The runner host stores CLI authentication separately, under the service user's home or vendor env.
The subprocess does not inherit the Agent-Colab service token.

| Runner kind | Default argv | Override env key |
|---|---|---|
| openclaw | `openclaw agent exec --message-file -` | `OPENCLAW_COMMAND` |
| hermes | `hermes chat --query-file - --quiet --source agent-colab-runner` | `HERMES_COMMAND` |
| chatgpt | `agent-colab-chatgpt` (operator wrapper) | `CHATGPT_COMMAND` |
| codex | `codex exec -` | `CODEX_COMMAND` |
| claude | `claude -p` | `CLAUDE_COMMAND` |
| claude-code | `claude -p` | `CLAUDE_CODE_COMMAND` |
| gemini | `gemini` (non-TTY stdin) | `GEMINI_COMMAND` |

For **each row**, choose its kind in the env file; registration, env storage, manual start,
systemd operation and verification below are the same. Claude and Claude Code intentionally share
an executable but retain distinct runner kinds. ChatGPT requires a locally installed integration
wrapper; there is no assumed official `chatgpt` executable. OpenClaw, Hermes and Gemini use their documented stdin entry points:
[OpenClaw agent exec](https://docs.openclaw.ai/cli/agent),
[Hermes query-file](https://hermes-agent.nousresearch.com/docs/reference/cli-commands), and
[Gemini headless mode](https://geminicli.com/docs/cli/headless/).
Claude print mode follows the [CLI reference](https://code.claude.com/docs/en/cli-usage).
Use current versions supporting these flags, or override with a compatible local stdin wrapper.
The ChatGPT wrapper is **not bundled** and must be installed/configured by the operator.

Overrides are shell-quoted argv (for example `HERMES_COMMAND='/opt/local/bin/hermes-stdin'`),
parsed without a shell. No pipes, variable expansion, or command substitution are evaluated.
Every command must consume its UTF-8 prompt on stdin, write its report to stdout, diagnostics to
stderr, and exit zero on success/nonzero on failure. Prompt text includes runner kind and the work
payload as JSON; it never changes the executable or argv. Codex stdin use follows the
[official noninteractive guide](https://developers.openai.com/codex/noninteractive/).

A credential-free smoke test can temporarily use the selected kind's override `/bin/cat`.
It exercises transport/prompt/result wiring without invoking an AI model. Restore the real command
before assigning actual work. No real vendor invocation is covered by automated tests.

Run from the checkout as the dedicated runner user:

```bash
set -a
. /etc/agent-colab/runner.env
set +a
.venv/bin/python -m server.runner --print-config
.venv/bin/python -m server.runner --once
```

`--print-config` (alias `--dry-run`) performs no network/subprocess calls and hides command contents
and credentials. `--once` sends heartbeat, polls at most one item, fetches its payload, acknowledges,
starts, executes, then submits a schema-valid success/failure document with redacted stdout/stderr,
exit code and elapsed time. Usage reports `ADAPTER_NO_METERING`. Idle `--once` is successful.
Without `--once`, it repeats every ten seconds. HTTP redirects are disabled.

For persistent operation, save `/etc/systemd/system/agent-colab-runner.service`:

```ini
[Unit]
Description=Agent-Colab runner
After=network-online.target
[Service]
User=agent-runner
WorkingDirectory=/opt/agent-colab
EnvironmentFile=/etc/agent-colab/runner.env
ExecStart=/opt/agent-colab/.venv/bin/python -m server.runner
Restart=on-failure
RestartSec=10
UMask=0077
[Install]
WantedBy=multi-user.target
```

Set an explicit PATH in the service env if CLIs are outside the system PATH, or use absolute
command overrides. Create the service user and work directory, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agent-colab-runner
sudo systemctl status agent-colab-runner
```

Verify **online/heartbeat** in Agents. Assign work to the Agent through the existing Task routing
flow. `GET /api/v1/work/{work_item_id}` using that Agent's credential must show `RESULT_RECEIVED` and
a result receipt (the submitted result carries `SUCCEEDED` or `FAILED`); another Agent cannot fetch/poll its work. `POST /api/v1/work/poll` accepts
`{"agent_id":"agent-example","max_items":1}` with Agent bearer auth and a fresh Idempotency-Key.
The matching `/ack`, `/start`, `/result` routes retain existing command-bus policy and ownership.
To exchange information, include the preceding Agent's report in the next assigned work payload;
this MVP adds no direct peer messaging protocol.

Limitations: a running command does not emit interim heartbeats; choose a timeout below the
server's offline threshold for long-lived online status. Process death or result transport failure
after execution relies on existing work timeout/operator recovery; no durable local result spool
or automatic resubmission is provided. Output is captured in memory and truncated before submission.
Redaction covers secret-named environment values, bearer strings and secret-looking assignments;
it cannot identify arbitrary unknown secrets in unlabelled prose. Do not supply secrets as work
payloads or ask CLIs to dump credential files. The runner executes with the service user's privileges.

## Mattermost on a private network

Web Admin → Channels → a channel's Bridges → **Show connection instructions** exposes both provider
recipes. The authenticated read-only API is `GET /api/v1/providers/connection-instructions`.
URLs use server `AGENT_COLAB_BASE_URL`; configure that to the operator-visible origin.

On the Agent-Colab server, store these values in its mode 0600 service env file and restart it:

- `AGENT_COLAB_MATTERMOST_URL`: Mattermost origin, e.g. `http://192.168.20.233:8065`.
- `AGENT_COLAB_MATTERMOST_BOT_TOKEN`: create a bot in Mattermost Integrations → Bot Accounts;
  add it to the team and the channels being imported.
- `AGENT_COLAB_MATTERMOST_ADMIN_TOKEN`: optional command-management personal access token from
  Mattermost. Required when the bot cannot manage slash commands. Keep it only on the server.
- `team_name`: slug from Mattermost team URL. Get `team_id` via Mattermost
  `GET /api/v4/teams/name/{team_name}`. Get `bot_user_id` from the bot profile/API.

All following POSTs use an authorized Human/service Account with `channel.manage`, an
`Authorization: Bearer` header loaded from protected env, and a fresh `Idempotency-Key`.
In a Web Admin session, use its normal authenticated request mechanism including CSRF.
Use a Python client so tokens do not appear in process argv; do not print full responses or errors:

```python
import os
import uuid
import httpx

base = os.environ["AGENT_COLAB_BASE_URL"]
with httpx.Client(
    base_url=base,
    headers={"Authorization": "Bearer " + os.environ["AGENT_COLAB_ADMIN_SERVICE_TOKEN"]},
    timeout=30,
) as client:
    response = client.post(
        "/api/v1/providers/mattermost/instances",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "base_url": os.environ["AGENT_COLAB_MATTERMOST_URL"],
            "team_name": "<team slug>",
            "team_id": "<team ID>",
            "bot_user_id": "<bot user ID>",
        },
    )
    print("HTTP status:", response.status_code)
    # On success read response.json()['resource_id'] locally for the steps below.
```

`AGENT_COLAB_ADMIN_SERVICE_TOKEN` above is a local script variable, not a server configuration key.
Repeat this request pattern for:

1. `POST /api/v1/providers/mattermost/commands/register` with
   `{"provider_instance_id":"<resource_id>","callback_url":"http://192.168.20.233:8080/api/v1/providers/mattermost/commands"}`.
   It creates/updates `/colab` and obtains the slash token directly from Mattermost; only its hash
   is stored in `provider_command_tokens`. No slash-token env var or plaintext readback exists.
2. `POST /api/v1/channels/import` with
   `{"provider_instance_id":"<resource_id>","external_channel_id":"<Mattermost Channel Info ID>","channel_type":"work"}`.
   Save the resulting Agent-Colab channel ID.
3. Run `/colab help` in the imported channel. Mattermost posts to the private callback above.
   Task-card actions use `http://192.168.20.233:8080/api/v1/providers/mattermost/actions`.
   Ensure Mattermost's outbound connection policy allows that private host. No public exposure
   is required. Interactive actions also require the existing signing-key configuration.

Ordinary Mattermost messages require the WebSocket subscriber in addition to slash callbacks.
On the server host, with the same server env (including `AGENT_COLAB_DATABASE_URL`), run:

```bash
.venv/bin/python -m server.connection_relay mattermost-listen
```

Use a separate systemd unit based on the runner template, with the server env file and the above
ExecStart. The server gateway already drains bridge outbox deliveries. Run one listener per
Mattermost instance. Messages should appear in a configured Telegram bridge without echo loops.

## Telegram connection

1. Use **@BotFather `/newbot`**, save the token as `TELEGRAM_BOT_TOKEN` in the server and relay env
   files, and restart the server. The same client is wired into the existing bridge Test endpoint.
2. Add the bot to the destination group. Grant posting permissions; forum `topic_per_root` also
   requires Manage Topics. Configure bot privacy to receive ordinary group messages when needed.
3. On the server host, with `AGENT_COLAB_DATABASE_URL` set, register the provider:

```bash
.venv/bin/python -m server.connection_relay telegram-register --workspace '<workspace UUID>'
```

The workspace UUID is `workspaces.id` from your initialized deployment (not the display name).
This trusted host command reads `getMe`, stores a `tg:<bot-id>` provider row without a token,
and refuses to reuse a bot belonging to another workspace. It does not bypass remote API auth;
it runs with the local operator's database credential. Repeating registration is harmless.

4. For private networks use **polling**. Telegram cloud cannot call an HTTP RFC1918 address.
   Ensure no webhook is installed and run exactly one poller for this bot. To remove a previously
   installed webhook, run this explicit operator action on the relay host:

```python
from server.channels.telegram.provider import client_from_env

client = client_from_env()
assert client is not None
client.delete_webhook()
```

Before starting the poller, send a message in the intended group/topic. To obtain just mapping IDs
without printing message bodies or secrets:

```python
from server.channels.telegram.provider import client_from_env

client = client_from_env()
assert client is not None
for update in client.get_updates(None, 0):
    message = update.get("message", {})
    if "chat" in message:
        print("chat_id:", message["chat"]["id"], "topic_id:", message.get("message_thread_id"))
```

Use the signed `chat.id` as `telegram_chat_id`. `telegram_thread_id` is the forum message's
`message_thread_id`, **null** for general, never 1. With a Human credential granting `bridge.manage`,
POST `/api/v1/channels/{channel_id}/bridges` using the authenticated request pattern above:

```json
{
  "provider_instance_id": "tg:<bot-id>",
  "telegram_chat_id": "<signed chat id>",
  "telegram_thread_id": null,
  "direction": "bidirectional",
  "thread_mode": "general"
}
```

Directions: `mattermost_to_telegram`, `telegram_to_mattermost`, `bidirectional`.
Thread modes: `general`, `fixed_topic` (set an actual topic ID), `topic_per_root` (forum bot creates
one topic per root). Command execution stays disabled unless explicitly enabled by bridge policy.
Save resource_id as bridge_id; POST `/api/v1/channels/{channel_id}/bridges/{bridge_id}/enable`.

Start intake:

```bash
.venv/bin/python -m server.connection_relay telegram-poll --offset-file /var/lib/agent-colab/telegram-offset.json
```

Use another systemd unit based on the earlier template, with server env, writable state directory,
and this ExecStart. `--once` runs one poll round. Offset is atomically persisted after each handled
update, so failures are replayable; existing bridge deduplication handles replay. Keep the offset
file with the relay's persistent state. The server gateway delivers its outbox.

5. POST `/api/v1/channels/{channel_id}/bridges/{bridge_id}/test` with `{}` using the same authenticated
   request pattern. Expect a message in Telegram. GET the same path ending `/status`, and check
   delivery/error state. Send a Telegram message and confirm it arrives in Mattermost; send a
   Mattermost message with `mattermost-listen` running and verify the reverse direction.

Webhook alternative: choose a publicly reachable HTTPS ingress that forwards only the provider
callback to `http://192.168.20.233:8080/api/v1/providers/telegram/updates/tg:<bot-id>`.
Generate `AGENT_COLAB_TELEGRAM_WEBHOOK_SECRET` locally and put the same value in server/relay env;
restart server. Stop polling before configuring:

```bash
.venv/bin/python -m server.connection_relay telegram-webhook --callback-url 'https://<your-ingress>/api/v1/providers/telegram/updates/tg:<bot-id>'
```

The command reads both secrets from env and supplies Telegram's `secret_token` parameter.
Agent-Colab validates `X-Telegram-Bot-Api-Secret-Token`. The private callback URL is useful for
routing/testing inside your network, but must not be registered directly with Telegram cloud.

This implementation supports one configured bot token and Mattermost origin per server process.
Full provider creation UI, multi-provider credential resolution and live third-party acceptance
are outside this slice. The instructions/API/UI and local relay are concrete entry points; no
live messages, registration or credential changes are performed by the automated test suite.
