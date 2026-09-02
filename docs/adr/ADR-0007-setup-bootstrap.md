# ADR-0007: Setup state, token, and pre-DB bootstrap store

- Status: Accepted (Phase 0, P0-05/P0-09)
- Date: 2026-09-02

## Decisions

1. **State ordinal as the invariant.** Each Setup state carries an ordinal (0–4); the store and
   the state machine reject any write/transition that lowers it, with one explicit retry edge
   (`BOOTSTRAP_FAILED → PREFLIGHT_PASSED`) that keeps the failure record. "Rollback never
   regresses the setup stage" is therefore a checked property, not a convention.
2. **Token storage = SHA-256 hash + 8-hex fingerprint.** No HMAC key is needed before the key
   provider exists; the token has 256 bits of entropy, so a plain hash is preimage-safe. The
   fingerprint keys failure counters together with the source IP.
3. **File layout.** One JSON document validated by a closed JSON Schema; unknown keys are schema
   errors and secret-looking keys/values are rejected by a denylist scan before writing and after
   reading. The file is written atomically and kept at 0600 inside a 0700 directory; permission
   drift is a read error.
4. **Handle TTL 15 minutes, process memory only.** Pre-DB secrets are never serialized; the
   handle store cannot be pickled and its `repr` is redacted. Restart ⇒ re-enter.
5. **Transport rule.** Loopback is open by default; remote requires all four conditions (TLS
   proxy, client mTLS, allowlist, valid token). Evaluation is pure so the Wizard can log the
   denial code without any side effect.
6. **Apply order** is enforced by a small step machine rather than by UI sequencing; Owner/TOTP
   visibility is derived from completed steps.
7. **Reconciliation** prefers the higher stage, makes the DB authoritative from `CONFIGURED`
   onward (local file reduced to a lock marker), and raises a conflict only for a foreign
   instance or an impossible local lead.

## Consequences

- P4-03 (Setup Wizard) composes these primitives; it must not persist any field outside the
  schema and must call `evaluate_transport` before any bootstrap handler.
- The DB-side `setup_state` table (P1-01/P4-03) mirrors `state`, `stage_ordinal`, `instance_id`,
  and `schema_migration_head` so reconciliation has both records.
