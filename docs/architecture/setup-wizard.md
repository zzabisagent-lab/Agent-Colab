# Setup Wizard (P4-03)

Development plan §8.1-§8.3, spec §12. HTTP surface `server/api/setup.py`, service
`server/setup/wizard.py`, Phase 0 primitives under `server/setup/`.

## State machine

`UNINITIALIZED → PREFLIGHT_PASSED → BOOTSTRAPPING → CONFIGURED → LOCKED`, failure →
`BOOTSTRAP_FAILED` (retry token, no secrets), reconfiguration `LOCKED → RECONFIGURING → LOCKED`
(30-minute session). The stage ordinal never regresses; a process that dies during
`BOOTSTRAPPING` is reconciled on restart: the DB record wins when it is CONFIGURED/LOCKED (the
local file keeps only the LOCKED marker), otherwise the interrupted bootstrap becomes
`BOOTSTRAP_FAILED` (`SETUP_INTERRUPTED`) and the operator retries with a new token.

## Endpoints (`/setup`, transport-guarded on every request)

| Method | Path | Purpose |
|---|---|---|
| GET | `/setup/state` | state, stage, preflight results, apply log, failure (no secrets) |
| POST | `/setup/token` | issue the single-use 256-bit token (30 min); `{token, expires_in_s, single_use, shown_once}` |
| POST | `/setup/configure` | `{section: db|keys|owner|integrations, values}`; secrets become 15-minute in-memory handles |
| POST | `/setup/preflight` | DB connect + migration dry-run, key/secret provider, Mattermost, storage; per-step `code`/`guidance` |
| GET | `/setup/diff` | redacted view of what bootstrap will apply (secrets as presence flags) |
| POST | `/setup/bootstrap` | `{token}` → success: `{state: LOCKED, owner: {service_token, totp_secret_b32, otpauth_uri, recovery_code}, shown_once}`; failure: `{state: BOOTSTRAP_FAILED, failed_step, error_code, owner_created: false, retry_token}` |
| POST | `/setup/reconfigure` | LOCKED only; Owner (`admin.settings`) + maintenance mode + recovery code + MFA re-auth → `{session_id, expires_at, recovery_code_next}` (the used code is rotated) |
| PUT | `/setup/reconfigure/{session_id}/settings` | `{changes}` validated before apply; 403 `SETUP_SESSION_EXPIRED` after 30 min |
| POST | `/setup/reconfigure/{session_id}/close` | back to LOCKED |

After LOCKED: `token`, `configure`, `preflight`, `diff`, `bootstrap` answer **404**.

## Transport boundary (V-P4-27)

Loopback is allowed by default. A remote client is allowed only when all four hold:
`AGENT_COLAB_SETUP_TRUST_PROXY=1` with `X-Forwarded-Proto: https`, `X-Client-Cert-Verified:
SUCCESS`, the address is in `AGENT_COLAB_SETUP_ALLOWLIST` (CIDRs), and `X-Setup-Token` is a valid
(unconsumed, unexpired) token. Every other combination → 403 with `SETUP_REMOTE_TLS_REQUIRED |
SETUP_REMOTE_MTLS_REQUIRED | SETUP_REMOTE_NOT_ALLOWLISTED | SETUP_TOKEN_*` and zero side effects.

## Token guard (V-P4-02)

Invalid/expired/reused → 403 (`SETUP_TOKEN_INVALID|EXPIRED|USED`); 5 failures within 15 minutes
per (ip, token fingerprint) block the source: 429 `SETUP_TOKEN_BLOCKED` from the 6th request for
15 minutes. Each rejection writes one redacted entry (`at, ip, token_fingerprint, code`) to the
sealed store's `rejection_log` and, when a DB exists, an `setup.token_rejected` audit row.

## Apply order (V-P4-04, V-P4-28)

`DB/migration → master key/secret provider → [Owner+TOTP+recovery code → integration settings →
CONFIGURED/LOCKED]`. The bracketed steps share one transaction, so a kill after any step leaves
no Owner and no CONFIGURED record; the key file (`secrets.master_key_path`, 0600 in a 0700 dir)
is the only artefact of the key step. `owner_created_visible` is true only after DB, key and
Owner steps completed. The Owner's service token, TOTP secret (`otpauth://` URI) and recovery
code are returned once; only hashes/ciphertext are stored (`mfa_enrollments`, `recovery_codes`).

## Sealed local store (V-P4-24)

`bootstrap_state_path` (`/var/lib/agent-colab/bootstrap/state.json` by default): owner-only,
schema-validated, secret-scanned, stage-monotonic. Before migration it holds the state, the token
hash/fingerprint/expiry, non-secret pointers (host/port/db/user, paths, provider name) and the
redacted rejection log; after LOCKED only the lock marker and `instance_id`.

## Tables (migration 0013)

`setup_state` (single row), `setup_reconfiguration_sessions`, `settings_versions`,
`mfa_enrollments`, `recovery_codes`, `maintenance_mode`.
