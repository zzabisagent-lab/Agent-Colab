# Message ingestion and retention (P2-15)

Authority: development plan §7H, §6.5; spec §8.1, §9.1 (Conversation, Message), §11.2, §15.7/21.

## Ingestion scope and DLP boundary

`server/channels/ingestion.py` stores provider messages only when they are in scope
(`in_ingestion_scope`): posts in Task/Brainstorm threads, Bridge-relayed messages, or the whole
channel when the channel documentation policy is `full_channel`. Before anything is persisted the
body passes `RedactionScanner`, which replaces secret-looking spans (`CANARY-NOT-A-SECRET-<n>`,
PEM blocks, AWS/GitHub/Slack/Telegram tokens, bearer tokens, DSN passwords, `key=value`
credential assignments, high-entropy blobs) with `<redacted:kind>` and reports kinds only. The
redacted text is what `messages.body_redacted`, Events, audit rows, Bridges and documents may
ever see. The original body is kept exclusively as envelope ciphertext under a per-message DEK
(`dek://<workspace>/message/<message_id>`), never in plaintext columns.

Message ids are deterministic (`msg-<sha256(source|source id|conversation)[:24]>`) and the unique
key `(source, source_message_id, conversation_id)` makes re-ingestion a no-op (`duplicate=True`).

## Retention and legal hold

`message_retention_policies` (per channel: `retention_days` default 365, `legal_hold`,
`documentation_policy`) is the authority; `channels.retention_days/legal_hold` mirror it for the
channel configuration screens. `set_retention` validates (1–3650 days) and writes a redacted audit
row. The daily `retention_job(session, crypto, clock, workspace_id, actor_account_id)`:

1. selects undeleted messages whose `received_at + retention_days < now` (injected Clock);
2. skips every message whose channel or message carries a legal hold (counted);
3. destroys the message DEK (`EnvelopeCrypto.destroy`, reason `RETENTION`, key tombstone chained);
4. appends a chained, immutable `message_tombstones` row and marks the message
   `deleted_at`, `body_redacted = REDACTED_BY_RETENTION`, `tombstone_ref = <tombstone hash>`.

Rows are never deleted; ciphertext stays as undecryptable bytes; a second run destroys nothing.

## Provenance

`server/application/messages.py::provenance_for(conversation_id)` returns message references
for the Documentation Service with `status = available | REDACTED_BY_RETENTION` and the tombstone
hash, so documents state explicitly that a source message was removed by retention.

## Commands

`SetChannelRetention` (`channel.manage`) and `RunRetention` (`ops.manage`, crypto injected via
`ctx.extras["crypto"]`) are bus commands; neither appends an Event (spec §9.3 defines no retention
Event) — both are audited.

## Ambiguities resolved

- §7H names a per-channel `retention_days`; 0003 already mirrored it on `channels`. The policy
  table is the authority so legal hold and documentation policy can be versioned together.
- "Delete expired Messages by DEK destruction" is implemented as crypto-shredding plus a marker;
  the row and ciphertext remain (spec §11.2: Event bytes are never removed; the same rule is
  applied to message ciphertext) so provenance and tombstones stay verifiable.
