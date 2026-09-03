# Secret Broker (P4-05/P4-06/P4-07)

## Data model (migration 0012)

- `secrets` — metadata only (`secret_ref`, name, provider, current_version, metadata jsonb, status).
- `secret_versions` — `ciphertext = nonce || AES-256-GCM(value)` under a per-version DEK;
  `wrapped_dek = nonce || AES-256-GCM_master(DEK)`; `dek_id = dek://secret/<ref>/v<n>`. Destroying
  a version nulls `wrapped_dek` (crypto-shredding); the ciphertext is then undecryptable.
- `secret_grants` — who may lease what: Agent, optional Task/action scope, lease TTL default,
  single-use default, `exposure_allowed` + `exposure_approval_id` (LLM context), validity.
- `secret_leases` — one-time handles: only `sha256(handle)` is stored; scope columns (Agent,
  Task, action, work item, sidecar instance), `expires_at`, `used_at/use_count`, `revoked_at`,
  `cleanup_acked_at`.
- `secret_revocations` — sequence-numbered feed for sidecars (poll `since`, or SSE).
- `key_tombstones` (+ `signature`, `ledger_key_id`) — the signed, append-only DEK ledger.

## Keys

- Master key: `AGENT_COLAB_MASTER_KEY_FILE` (owner-only 0600, refused otherwise) or
  `AGENT_COLAB_MASTER_KEY_B64`; never in the DB. A DB dump + service tokens cannot decrypt
  anything (V-P4-17, `tests/integration/test_backup_key_separation.py`).
- Ledger key: `AGENT_COLAB_LEDGER_KEY_B64`, separate from the master key; signs tombstone
  entries (HMAC-SHA256 over the chain hash). `verify_chain` checks links, hashes, signatures.
- `reconcile_tombstones(session)` (restore): every tombstoned DEK is re-shredded in
  `secret_versions` and `sensitive_keys`; a restored backup never revives a destroyed DEK.

## Flow

grant (admin, `secret.grant`) → lease (Agent, `secret.lease`; handle `sh-…` returned once) →
resolve (Agent/sidecar; scope + expiry + single use + revocation + exposure checks; bytes once;
`SECRET_ACCESSED`) → revoke (admin, Task end via the task terminal hook, Agent revocation,
rotation): durable rows first, then the in-process `LIVE` registry (listeners wipe in-memory
buffers), then the feed row for sidecars. Denials: one redacted audit row each, written in an
independent transaction.

## Injection (P4-07)

`InMemoryHandleStore` resolves through the Broker for in-process adapters (`mcp` pull with an
in-memory handle, `webhook`), keeps values in `bytearray`s, and zeroes them on revoke/cleanup.
`SecretLogFilter` (installed on the root logger at app start) scrubs live values from log
records. The Mattermost bot adapter advertises `secret_handles: unsupported` and refuses
secret-carrying work items. Sidecars use the HTTP contract in `docs/protocol/secret-sidecar-api.md`.

## Canary DLP (V-P4-14)

`server/secrets/canary.py` registers `CANARY-NOT-A-SECRET-<n>` secrets and scans Events, audit
metadata, outbox rows, messages, channel posts, work items/receipts, documents, task
projections, notifications, log lines, error texts and document files; it reports locations only.

## Hooks for other packages

- Hard delete (P4-11): `ledger.record_tombstone(session, key, dek_id=…, …)` after shredding
  (`local_provider.destroy_version` or `EnvelopeCrypto.destroy`); `ledger.is_destroyed(dek_id)`.
- Restore / Setup: `ledger.reconcile_tombstones(session, key)` before the service opens.
- Tasks: `application.secrets.revoke_for_task_hook` is registered as a terminal-transition hook.
- Registry (Agent revoke): call `broker.revoke(kind="agent", target_id=<agent_id>, …)`.
