# Channels architecture (Phase 2 module map)

Authority: spec §8, §10, development plan §3.1 (Channel Gateway, Telegram Bridge, Command Router,
Notification), §6.5, §7A, §7H, §7G. Phase 0 contracts already fixed: `server/channels/commands.py`
(grammar parser, P0-10), `server/channels/contract.py` (action-callback validation, P0-10),
`server/channels/telegram_contract.py` (Bridge thread mapping, P0-13). Phase 1 gives the command
bus (`server/application/bus.py`), `delivery_outbox` + drain (`server/notifications/outbox.py`),
identity links (`server/identity/external_links.py`) and the Task projection.

| Module | Package | Responsibility |
|---|---|---|
| `server/channels/mattermost/client.py` | P2-01 | REST v4 + WebSocket client (`MattermostClient` protocol + httpx implementation + fake for tests): posts (create/patch/ephemeral/DM), users, channels, commands registration, config read, WS event stream `posted`/`post_edited`/`reaction_added` |
| `server/channels/mattermost/provider.py` | P2-01 | provider instance = base URL + team; bot identity; slash-command registration; 4 default channel templates (work/brainstorm/approval/ops) + custom; channel import (`channels` rows with `external_channel_id`) |
| `server/api/v1/providers_mattermost.py` | P2-01 | `POST /api/v1/providers/mattermost/commands` (slash command form POST; validates the command token, 5-minute timestamp, one-time nonce via P0-10 contract) → Command Router; `POST /api/v1/providers/mattermost/actions` (interactive callbacks, P2-12) |
| `server/channels/router.py` | P2-10 | `route(session, runtime, SlashRequest) -> CommandResponse`: parse (P0-10) → principal from the Mattermost user's active ExternalIdentityLink (unlinked ⇒ only `link`/`help`) → thread-context target resolution → bus command via `execute_command` with idempotency `provider_instance:post_id` → ephemeral error (cause + example) or public thread reply |
| `server/channels/outbox.py` | P2-03 | Renderer enqueue helpers over `delivery_outbox` (kinds `mattermost.post`, `mattermost.patch`, `mattermost.ephemeral`, `telegram.send`, `telegram.edit`); dedupe keys; the same transaction as the Event append |
| `server/channels/renderer.py` | P2-03 / P2-11 | Event → card/thread rendering: Task card (root post edited in place), one thread reply per transition, progress coalescing 10 s, >16k → Artifact link, sub-Task link card; Brainstorm card (Phase 6) |
| `server/channels/telegram/client.py` | P2-04 | Bot API client (`TelegramClient` protocol + httpx implementation + fake): sendMessage with `message_thread_id`/`reply_parameters`, editMessageText, getUpdates/webhook intake, forum topics, rate-limit `retry_after` handling per P0-13 |
| `server/channels/telegram/bridge.py` | P2-05 / P2-06 | per-channel Bridges: direction/content filters/redaction/identity prefix, thread mapping via `message_mappings` (unique source key, origin marker, hop count), duplicate Telegram target rejection with admin exception, dead-letter, replay |
| `server/channels/policy.py` | P2-08 | Telegram command policy (read/reply default; §7A.6 restricted grammar) |
| `server/channels/ingestion.py` | P2-15 | message ingestion/normalization after redaction, retention job with legal hold, tombstones, `REDACTED_BY_RETENTION` provenance |
| `server/channels/actions.py` | P2-12 | interactive action callback handling (signature/nonce/authz at callback time, exactly-once) |
| `server/identity/mattermost_link.py` | P2-13 | `/colab link start|confirm` over `ExternalLinkService` (DM code, TTL, lockout, admin approval) |
| `i18n/{ko,en}/*.json`, `server/i18n.py` | P2-16 | message keys used by router/renderer/document headings |
| `server/notifications/providers.py` | P2-17 | Mattermost mention/DM and SMTP providers for the P1-13 outbox drain |

Rules shared by every package:

- Every state change still goes through the command bus; the gateway never interprets free text.
- Provider callbacks are validated before any normalization (§7.5): token, 5-minute timestamp,
  one-time nonce, body hash.
- Outbound side effects are recorded in `delivery_outbox` in the same transaction as the Event and
  delivered by the drain with exactly-once dedupe keys; providers are idempotent per dedupe key.
- Secrets (bot tokens, command tokens) are Secret references: Phase 2 reads them from settings/env
  (`AGENT_COLAB_MATTERMOST_*`, `TELEGRAM_BOT_TOKEN`), never from Events, logs, or payloads.
- Tests use the local Mattermost Team Edition (`scripts/dev/mattermost-local.sh`) and the two
  Telegram forum chats from `.env`; unit tests use the fake clients.
