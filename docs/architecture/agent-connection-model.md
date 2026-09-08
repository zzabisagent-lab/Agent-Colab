# Agent-Colab Agent Connection Model

This document defines the concrete connection model for external AI agents.

## Scope

Initial supported runner kinds:

- `openclaw`
- `hermes`
- `chatgpt`
- `codex`
- `claude`
- `claude-code`
- `gemini`

Agent-Colab is the control plane. External agents do not connect magically after registration. A runner process on the agent host bridges Agent-Colab work items to each CLI/API agent.

## Values created by Agent-Colab

When an operator registers an Agent, Agent-Colab creates or records:

| Value | Source | Secret? | Purpose |
|---|---|---:|---|
| `agent_id` | operator-chosen registration input | no | stable agent identity |
| `account_id` | generated as `acct-<agent_id>` | no | internal principal |
| `service_token` | generated once at registration | yes | runner authenticates to Agent-Colab |
| `credential_fingerprint` | generated/stored hash identity | no | audit and credential tracking |

`service_token` is shown once and must be stored on the runner host or in a secret manager. It must never be committed, logged, or displayed after creation.

## Values stored on the runner host

Each runner host needs:

```dotenv
AGENT_COLAB_BASE_URL=http://192.168.20.233:8080
AGENT_COLAB_AGENT_ID=agent-codex-controller-01
AGENT_COLAB_SERVICE_TOKEN=svc-REDACTED
AGENT_COLAB_WORKDIR=/path/to/worktree
AGENT_COLAB_RUNNER_KIND=codex
```

The selected CLI must also be installed and authenticated using its own vendor mechanism. Agent-Colab does not create Codex/OpenClaw/Claude/Gemini/ChatGPT OAuth sessions.

## Pull runner flow

```text
runner starts
  -> authenticate with service token
  -> poll Agent-Colab for own work
  -> ack work item
  -> start work item
  -> build CLI prompt from payload/task/document
  -> run external CLI/API command
  -> capture stdout/stderr/status/artifacts
  -> submit work_result
  -> repeat
```

## Per-agent command defaults

| Runner kind | Default command shape | Notes |
|---|---|---|
| `codex` | `codex exec <prompt>` | Codex CLI auth required |
| `claude-code` | `claude -p <prompt>` | Claude Code CLI auth required |
| `claude` | configurable command/API wrapper | direct Claude API/CLI varies by installation |
| `opencode` / `openclaw` | configurable command | project uses `openclaw` label; command may be `openclaw` or a wrapper |
| `hermes` | `hermes chat -q <prompt>` | Hermes profile/auth must be configured |
| `chatgpt` | configurable command/API wrapper | official local CLI is not assumed |
| `gemini` | configurable command/API wrapper | Gemini CLI/auth varies |

All command paths must be overrideable by environment variables.

## Mattermost simple connection model

Operator must know and store:

| Value | Source |
|---|---|
| Mattermost base URL | Mattermost server |
| team name/id | Mattermost |
| bot token | Mattermost bot account; store as secret ref/env |
| slash command token | Mattermost slash command configuration; store as secret ref/env |
| command callback URL | Agent-Colab displays concrete URL |
| action callback URL | Agent-Colab displays concrete URL |
| channels to import/bridge | Mattermost team/channel list |

Agent-Colab should display concrete callback URLs based on `AGENT_COLAB_BASE_URL`.

## Telegram simple connection model

Operator must know and store:

| Value | Source |
|---|---|
| bot token | BotFather; store as secret ref/env |
| chat id | Telegram chat/forum |
| topic id | Telegram forum topic, optional |
| bridge direction | operator selection |
| thread mode | operator selection |
| webhook URL or polling mode | Agent-Colab displays concrete URL |

## Verification

A complete connection is not just saved settings. It must verify:

1. Runner can authenticate to Agent-Colab.
2. Runner can poll or receive work.
3. Runner can execute the selected external agent command.
4. Runner can submit `work_result`.
5. Agent-Colab Audit/Overview reflects the result.
6. Mattermost/Telegram test messages prove callbacks/outbox work.
