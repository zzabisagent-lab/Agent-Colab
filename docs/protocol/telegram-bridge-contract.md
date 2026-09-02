# Telegram Bridge thread-mapping contract v1 (P0-13 → P2-04/P2-05/P2-06)

Authority: spec §10, development plan §6.5, §7A.6, §7H; observations in
`docs/protocol/telegram-spike.md`. Code: `server/channels/telegram_contract.py`; mapping row
schema: `schemas/api/telegram/bridge-mapping.v1.schema.json`; fixtures:
`tests/fixtures/telegram/mapping-cases.yaml`.

## Unit of configuration

A Bridge belongs to one Mattermost channel and one Telegram chat (`BridgeConfig`). Connecting one
Telegram chat/topic to several channels is rejected by default (P2-05 unique policy; explicit
administrator exception recorded). `tg_thread_mode` is `topic_per_root` (default), `general`, or
`fixed_topic`.

## Mapping rules

| Source | Target |
|---|---|
| Mattermost root post | `topic_per_root`: create a forum topic named from the post (thread id = service message id, stored in the mapping); `general`: send to the General topic (thread omitted); `fixed_topic`: send into the configured topic |
| Mattermost thread reply | `sendMessage` into the topic of the mapped root with `reply_parameters.message_id` = the Telegram id of the mapped root post (`BRIDGE_TARGET_UNMAPPED` if the root was never relayed) |
| Telegram message replying to a mapped message | Mattermost reply in that message's thread |
| Telegram message in a mapped topic | Mattermost reply under the mapped root post |
| Telegram message in an unmapped topic or the General topic | new Mattermost root post (and a new mapping whose `tg_thread_id` is the topic, so later topic messages join it) |

`resolve_target` is pure and evaluates in a fixed order: loop check → direction check → duplicate
check → routing.

## Dedupe, origin marker, hop count

- `mapping_key = SHA-256(bridge_id | source_platform | source_message_id)`; the DB enforces
  `UNIQUE(bridge_id, source_platform, source_message_id)` → `BRIDGE_DUPLICATE_SOURCE`.
- Every relayed message stores an immutable origin marker (`origin_platform`, `origin_message_id`)
  and `hop_count` (0 original, 1 relayed). Messages with `hop_count ≥ 1`, messages authored by the
  bridge bot, and messages whose origin marker names the other platform are never relayed
  (`BRIDGE_LOOP_DETECTED`).
- Direction policy `mattermost_to_telegram | telegram_to_mattermost | bidirectional` and the
  enabled flag are enforced before any side effect (`BRIDGE_DIRECTION_DENIED`).

## Identity display

Relayed text is prefixed with `[<sender> via Telegram]` or `[<sender> via Mattermost]` (spec
§10.2); the sender name is the display name after redaction policy.

## Rate limit and retry

Per Telegram chat: token bucket of 20 sends per 60 s, sustained 1 send/s, one in-flight send. On
HTTP 429 the outbox waits `retry_after` (capped at 120 s) before the next attempt; other transient
errors use 1/2/4… s backoff; dead-letter after the outbox's attempt limit (P2-06). Edits of
relayed messages are attempted only on bot-owned messages and only within 48 h; a
`message is not modified` response is a successful no-op.

## Error codes

`BRIDGE_LOOP_DETECTED`, `BRIDGE_DUPLICATE_SOURCE`, `BRIDGE_DIRECTION_DENIED`,
`BRIDGE_TARGET_UNMAPPED`.
