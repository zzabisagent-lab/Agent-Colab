# Telegram Bridge (P2-05 / P2-06)

Authority: spec §10, §8.1, §15.4/5/7, §17; development plan §3.1 (Telegram Bridge), §6.5,
§7G, §21.1 (DLP scope, normal load); the P0-13 contract (`server/channels/telegram_contract.py`,
`docs/protocol/telegram-bridge-contract.md`) fixes thread mapping, dedupe keys, loop rules and the
rate budget. This document records how Phase 2 implements them.

## Data (migration 0007)

| Table | Purpose |
|---|---|
| `telegram_bridges` | one row per Bridge: channel, Telegram target (chat + optional topic), direction, thread mode, content/redaction/identity/rate policies, `allow_commands`, `admin_exception`, status. Partial unique index `(provider_instance_id, telegram_chat_id, COALESCE(telegram_thread_id,''))` where `admin_exception = false` enforces "one Telegram target → one Mattermost channel" by default (spec §10.1). |
| `message_mappings` | one row per relayed message: source/destination platform and ids, Mattermost post/root ids, Telegram chat/message/thread/reply ids, origin marker and hop count (≤ 1), redaction status, delivery status; unique on `(bridge_id, source_platform, source_message_id)` and on `dedupe_key`. |
| `bridge_dead_letters` | outbox rows that exhausted their attempts; replayed at most once (`replayed_at`). |

Directions are stored with the contract's values (`mattermost_to_telegram`,
`telegram_to_mattermost`, `bidirectional`).

## Relay pipeline (`server/channels/telegram/bridge.py`)

`Bridge.on_mattermost_post(session, clock, MattermostPostView)` and
`Bridge.on_telegram_message(session, clock, InboundMessage)` run, per Bridge of that channel/chat:

1. disabled Bridge → skipped (metrics `skipped_disabled`);
2. **loop**: bot-authored messages, messages carrying our origin marker (Mattermost `props.agent_colab_bridge`, or the `[sender via <Source>]` prefix on Telegram text) or `hop_count ≥ 1` → `BRIDGE_LOOP_DETECTED`, audited;
3. **direction**: reverse of a one-way Bridge → `BRIDGE_DIRECTION_DENIED`, audited, metrics `direction_denied`;
4. **duplicate**: an existing mapping for the same source → `BRIDGE_DUPLICATE_SOURCE`, metrics `duplicates_prevented`;
5. **content policy**: text / attachment / system_event / approval_notice / mention flags → `BRIDGE_CONTENT_FILTERED`;
6. **thread target** via `telegram_contract.resolve_target` over the completed mappings (root ↔ topic, reply ↔ reply, General/fixed topic modes);
7. **redaction** (`server/channels/telegram/redaction.py`): only the redacted text is persisted (`message_mappings.redaction_status`) or forwarded; finding *kinds* are audited (`bridge.redacted`), never values;
8. one pending `message_mappings` row and one `delivery_outbox` row are written in the caller's transaction (`Delivery(kind="telegram.send"|"mattermost.post", dedupe_key=mapping_key(...))`).

The origin marker travels as Mattermost post props (`agent_colab_bridge: {origin, hop, bridge_id}`)
and, on Telegram, as the identity prefix `[sender via Mattermost]`; both are recognised on the way
back, so an echo is dropped before any side effect.

## Delivery and completion

`Bridge.deliver(session, providers, clock, workspace_id)` drains the outbox for Bridge kinds with
`drain_channels` (backoff 1/5/25/125/625 s, `BRIDGE_MAX_ATTEMPTS = 8` so a 10-minute outage never
dead-letters), then completes mappings from the providers' idempotent result maps
(`record_delivered`). Providers:

- `TelegramBridgeProvider(client)` — creates the forum topic when the target asks for one, sends
  the text (`message_thread_id`/`reply_parameters`), returns `"<thread>:<message_id>"`;
- `MattermostBridgeProvider(client)` — `create_post(channel, message, root_id, props)`.

Both are idempotent per dedupe key, so a crash right after the provider call and a replay produce
exactly one destination side effect. Rows that still fail after the maximum attempts move to
`bridge_dead_letters` with a `BRIDGE_DELIVERY_FAILED` Event; `replay_dead_letters` re-enqueues
each exactly once.

## Administration (`server/channels/bridge_admin.py`, `server/application/bridges.py`,
`server/api/v1/bridges.py`)

Bus commands (permission `bridge.manage`, channel scope): `CreateBridge`, `UpdateBridge`,
`EnableBridge`/`DisableBridge` (`TELEGRAM_BRIDGE_ENABLED|DISABLED` Events), `TestBridge` (probe
message, no mapping), read models `bridge_status`/`list_bridges`. Binding a Telegram target that
another channel already owns is `BRIDGE_TARGET_DUPLICATE` unless `admin_exception=true` **and** the
actor holds `admin.settings`; the exception and its reason are stored and audited. REST:
`/api/v1/channels/{channel_id}/bridges` (`POST`, `GET`), `/{bridge_id}` (`PATCH`, `GET status`),
`/{bridge_id}/enable|disable|test`; unauthorized accounts receive the normalized 404.

## Metrics

`Bridge.metrics` (`BridgeMetrics`): delivered, enqueued, duplicates_prevented, loops_blocked,
direction_denied, content_filtered, redacted, dead_lettered, replayed, skipped_disabled — exposed
to the dashboard in P4-02.

## Wiring (parent)

- `app.state.telegram_inbound_handler = lambda msg: bridge.on_telegram_message(session, clock, msg)`
  inside a session scope; the Mattermost WebSocket subscriber calls `bridge.on_mattermost_post`.
- A periodic job calls `bridge.deliver(session, {"telegram": TelegramBridgeProvider(client),
  "mattermost": MattermostBridgeProvider(mm_client)}, clock, workspace_id)`.
- Mount `server/api/v1/bridges.py` before the MCP root mount.

## Ambiguities resolved

- Direction values follow the P0-13 contract enum rather than the shorter `mm_to_tg` spelling.
- "One Telegram target" = chat plus optional topic; the General topic and each forum topic are
  distinct targets.
- A relayed Telegram message is recognised as our own by the bot author id; the identity prefix is
  an additional origin mark because Telegram carries no message props.
