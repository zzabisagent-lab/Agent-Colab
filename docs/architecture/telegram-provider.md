# Telegram provider (P2-04)

Authority: spec §10, §15.4–5; development plan §3.1 (Telegram Bridge), §6.5, §7.5, §7A.6;
P0-13 spike (`docs/protocol/telegram-spike.md`) and Bridge contract
(`docs/protocol/telegram-bridge-contract.md`, `server/channels/telegram_contract.py`).

## Modules

| Module | Role |
|---|---|
| `server/channels/telegram/client.py` | `TelegramClient` protocol; `HttpTelegramClient` (Bot API over httpx) with per-chat token bucket (20 per 60 s, 1 msg/s sustained, one in-flight send per chat) and `retry_after` handling on 429 (capped at 120 s, at most 4 attempts); stable errors `TELEGRAM_RATE_LIMITED`, `TELEGRAM_FORBIDDEN`, `TELEGRAM_BAD_REQUEST`, `TELEGRAM_UNAVAILABLE`; the bot token never appears in `repr` or error text; `FakeTelegramClient` for tests (simulates 429, rejects `message_thread_id: 1`, records calls) |
| `server/channels/telegram/provider.py` | provider instance id `tg:<bot id>`; `TelegramNotificationProvider` = P1-13 outbox `Provider` for destinations `telegram:<chat_id>[:<message_thread_id>]`, idempotent per `payload["dedupe_key"]` |
| `server/channels/telegram/intake.py` | JSON-Schema validation of updates (`schemas/api/telegram/webhook-update.v1.schema.json`), `normalize_update` → `InboundMessage` (chat, thread, message, reply target, sender, text/caption, attachments, `is_topic_message`, `forum_topic_created`, edited flag), `poll_updates` long-polling loop with a persisted offset (`OffsetStore`) |
| `server/api/v1/providers_telegram.py` | webhook `POST /api/v1/providers/telegram/updates/{provider_instance_id}` |
| `server/channels/telegram/attachments.py` | attachment policy (`schemas/api/telegram/attachment-policy.v1.schema.json`), `evaluate_attachment`, `fetch_to_artifact` |
| `migrations/sql/0004_phase2_telegram.sql` | `telegram_update_receipts (provider_instance_id, update_id)` — replay protection |

## Webhook validation order (§7.5)

1. `X-Telegram-Bot-Api-Secret-Token` must equal the configured secret
   (`app.state.telegram_webhook_secret` or `AGENT_COLAB_TELEGRAM_WEBHOOK_SECRET`), compared in
   constant time → otherwise 401 `CALLBACK_SIGNATURE_INVALID`.
2. Optional `X-Colab-Body-SHA256` must match the body → otherwise 401 `CALLBACK_BODY_HASH_MISMATCH`.
3. The path's provider instance must exist, be `telegram` and `active` → otherwise 404
   `PROVIDER_INSTANCE_UNKNOWN`.
4. The update must validate against the schema → otherwise 400 `TELEGRAM_UPDATE_INVALID`;
   unsupported kinds (membership, callback queries) are acknowledged and ignored.
5. The message `date` must be within the 5-minute tolerance → otherwise 403
   `CALLBACK_TIMESTAMP_EXPIRED`.
6. `(provider_instance_id, update_id)` is inserted into `telegram_update_receipts`; a duplicate
   is a replay → 200 `{"status": "replayed"}` with zero side effects.
7. Only then is the normalized message handed to `app.state.telegram_inbound_handler` (the
   Bridge, P2-05). The intake itself never appends a domain Event.

Telegram does not sign webhook bodies; the secret token is the only authenticator the Bot API
offers, so the body-hash header is an optional extra for proxies that add it. Deployments that
cannot expose a public HTTPS endpoint use `poll_updates` (offset persisted after every update).

## Attachment policy (V-P2-11)

Deny by default: size ≤ 20 MB (configurable), MIME must match an allow prefix (images, PDF, text,
JSON, Office/OpenDocument) and no deny prefix (executables, scripts, archives, JavaScript,
Python), file name passes the Artifact storage rules (no traversal, separators, drive prefixes,
control characters, denied extensions). Decisions carry `ATTACHMENT_TOO_LARGE`,
`ATTACHMENT_MIME_DENIED`, `ATTACHMENT_PATH_INVALID`, `ATTACHMENT_SCAN_PENDING`. Allowed
attachments are downloaded only after the decision, re-checked for size on the bytes, stored
content-addressed with their SHA-256, and optionally scanned (`ATTACHMENT_SCAN_FAILED`).

## Constraints taken from the P0-13 spike

- General topic = `message_thread_id` omitted; the client never sends `1`.
- Topic id = `message_id` of the `forum_topic_created` service message (delivered as an update).
- Replies keep the thread of the `message_thread_id` parameter; `reply_parameters` names the
  target message.
- Edits: own messages only; the Bridge relays edits as new replies when the original is foreign.
- Rate limit: bursts of ~1 msg/s per chat, then 429 with `retry_after` 31–33 s — honoured.

## Secrets

`TELEGRAM_BOT_TOKEN` and the webhook secret are Secret references resolved from the environment
in Phase 2 (`client_from_env`); the Secret Broker (Phase 4) replaces the environment lookup. The
token is never logged, never part of an Event, and redacted from error descriptions.
