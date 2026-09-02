# Setup state and pre-DB bootstrap store contract (P0-05, P0-09)

Authority: spec §12, §12.1; development plan §8.1–8.3, §21.1, §24. Code: `server/setup/`.
Schema: `schemas/api/setup/bootstrap-state.v1.schema.json`. Fixtures: `tests/fixtures/setup/`.
Verified by V-P0-12 now and by V-P4-01/02/03/04/19/24/27/28 when the Wizard is built (P4-03).

## State machine (`server/setup/state.py`)

| State | Ordinal | Leaves to |
|---|---|---|
| `UNINITIALIZED` | 0 | `PREFLIGHT_PASSED` |
| `PREFLIGHT_PASSED` | 1 | `BOOTSTRAPPING` |
| `BOOTSTRAPPING` | 2 | `CONFIGURED`, `BOOTSTRAP_FAILED` |
| `BOOTSTRAP_FAILED` | 2 | `BOOTSTRAPPING` (retry), `PREFLIGHT_PASSED` (re-run preflight) |
| `CONFIGURED` | 3 | `LOCKED` |
| `LOCKED` | 4 | `RECONFIGURING` |
| `RECONFIGURING` | 4 | `LOCKED` (completion or 30-minute expiry) |

- The ordinal never decreases except on the explicit `BOOTSTRAP_FAILED → PREFLIGHT_PASSED` retry
  edge, which re-runs preflight and keeps the failure record; committed configuration is never
  rolled back. Any other lower-ordinal target is `SETUP_TRANSITION_INVALID`.
- `require_bootstrap_open()` raises `SETUP_LOCKED` from `CONFIGURED` onward: `/setup/bootstrap`
  answers 404/403 after completion (V-P4-03).
- `BOOTSTRAP_FAILED` stores `failed_step`, `error_code`, the **fingerprint** of the retry token
  and `failed_at` — never token values or entered secrets.
- Reconfiguration opens only from `LOCKED` with a `ReconfigurationProof` whose maintenance mode,
  recovery-code verification, MFA re-authentication, and owner account are all present
  (`SETUP_REAUTH_REQUIRED` otherwise). The session lasts 30 minutes
  (`SETUP_RECONFIGURE_SESSION_MIN`); the first action at or after expiry closes it, returns the
  state to `LOCKED`, and raises `SETUP_SESSION_EXPIRED`; later actions get `SETUP_LOCKED`
  (V-P4-19: 29 minutes reconfigures, 30 minutes does not).

## Setup token (`server/setup/token.py`)

- `secrets.token_bytes(32)` (256 bits), shown once as 64 hex characters.
- Persisted form: `token_hash = SHA-256(value)`, `token_fingerprint = token_hash[:8]`,
  `token_expires_at = issued + 30 min`, `token_used`.
- `verify(presented, ip)` checks in order: source block → hash (constant-time) → single-use →
  TTL; codes `SETUP_TOKEN_BLOCKED`, `SETUP_TOKEN_INVALID`, `SETUP_TOKEN_USED`,
  `SETUP_TOKEN_EXPIRED`. A successful verify consumes the token.
- Failure counting key is `(ip, fingerprint of the presented value)`; 5 failures inside a
  15-minute sliding window block that key for 15 minutes (6th request is `SETUP_TOKEN_BLOCKED`,
  V-P4-02). Counters exported for the sealed store contain only ip, fingerprint, count, and
  `blocked_until`.

## Sealed local store (`server/setup/bootstrap_store.py`)

Path `/var/lib/agent-colab/bootstrap/state.json` (injectable). Contents are exactly the schema
fields: `schema_version`, `state`, `stage_ordinal`, `token_hash`, `token_fingerprint`,
`token_expires_at`, `token_used`, `config_pointers` (host/port/db name/user, provider name,
paths, instance name, base URL, language, timezone — closed list), `failure_counters`,
`last_failure`, `lock_marker`, `recovery_metadata` (`instance_id`, `locked_at`,
`setup_state_location: db`, `schema_migration_head`), `updated_at`. `additionalProperties` is
false at every level.

Guarantees:

- **No secrets**: before every write and after every read, keys matching
  password/secret/private/api_key/client_secret/totp/recovery_code/dsn/credential/… are rejected
  (`token_hash`, `token_fingerprint`, `secret_provider` are the only allowed exceptions), and
  string values that look like a DSN with credentials, a PEM block, or a ≥ 32-character
  high-entropy blob are rejected: `BOOTSTRAP_STORE_SECRET_REJECTED`. Error details name the
  JSON path only, never the value.
- **Owner-only**: directory 0700, file 0600, created and re-applied on write; a read with any
  group/other bit or a different owner fails with `BOOTSTRAP_STORE_PERMISSIONS_INVALID`.
- **Atomic**: temp file in the same directory, fsync, `rename`, directory fsync; a failed
  validation leaves no temp file.
- **Monotonic stage**: a write whose `stage_ordinal` is lower than the stored one is
  `BOOTSTRAP_STORE_STAGE_REGRESSION`, except the explicit `BOOTSTRAP_FAILED → PREFLIGHT_PASSED`
  retry (`allow_retry_regression=True`).
- **After DB migration**: `lock_marker_document()` — state `LOCKED`, `lock_marker: true`, no
  token hash, empty pointers, minimal recovery metadata — is the only content that remains
  (V-P4-24).
- **Starts without a DB**: the store and state machine need no database URL; the first document
  holds the token hash and nothing else.

## Pre-DB secret handles (`server/setup/handles.py`)

DB credentials and initial key material entered before the DB exists are kept only in
`PreDbHandleStore` (process memory), 15-minute TTL (`SETUP_PRE_DB_HANDLE_TTL_MIN`), wiped on
expiry/revoke, unpicklable, redacted in `repr`/`str`. A restarted process has an empty store by
construction, so values must be re-entered (`SETUP_HANDLE_EXPIRED`).

## Transport boundary (`server/setup/transport.py`)

`evaluate_transport` is a pure function. Loopback source on the default loopback bind →
`SETUP_TRANSPORT_LOCAL`. Any other source is allowed only when **all** of TLS terminated by the
reverse proxy, client mTLS verified, source in the IP allowlist, and a passed token check hold;
the first missing condition names the denial: `SETUP_REMOTE_TLS_REQUIRED`,
`SETUP_REMOTE_MTLS_REQUIRED`, `SETUP_REMOTE_NOT_ALLOWLISTED`, `SETUP_TOKEN_*`. All 16
combinations are in `transport-truth-table.yaml`; exactly one is allowed.

## Apply order (`server/setup/order.py`)

`DB_MIGRATION(1) → KEY_PROVIDER(2) → OWNER_TOTP(3) → INTEGRATIONS(4) → COMMIT(5)`. `begin(step)`
requires every earlier step complete (`SETUP_ORDER_VIOLATION`); `fail(step)` clears that step
and later ones only. `owner_created_visible` is true only when steps 1–3 are complete, so the UI
can never show "owner created" before the DB and key provider are ready (V-P4-28).

## Reconciliation (`server/setup/reconcile.py`)

Inputs: the local document (or none) and the DB `setup_state` record (or none).

| Situation | Result | Local file afterwards |
|---|---|---|
| neither | `UNINITIALIZED` | new initial document |
| local only | local state | unchanged |
| DB `CONFIGURED`/`LOCKED`/`RECONFIGURING` | DB state (higher) | lock marker only |
| DB pre-configuration, local higher or equal | local state (`BOOTSTRAP_FAILED` wins over `BOOTSTRAPPING`) | unchanged / failure kept |
| DB pre-configuration, DB higher | DB state | advanced to DB stage |

`RECONCILE_CONFLICT` is raised only when both sides carry different `instance_id`s or the local
file claims a stage above a configured DB (a file from another installation); nothing is written
in that case. The result never lowers the stage and every produced document passes the secret
scan.
