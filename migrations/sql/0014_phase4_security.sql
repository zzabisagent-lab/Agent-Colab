-- Phase 4 (P4-08/P4-09/P4-10/P4-14): MFA proofs, break-glass sessions, auth rate limits.
-- Secrets: TOTP secrets are envelope-encrypted (sensitive_keys DEK); recovery codes are hashes.
-- CSRF uses a stateless double-submit token (cookie + header), so no table is needed.

-- Mirrors of the setup package's tables (migration 0013 may land before or after this one).
CREATE TABLE IF NOT EXISTS mfa_enrollments (
  account_id uuid NOT NULL REFERENCES accounts(id),
  method text NOT NULL CHECK (method IN ('totp')),
  secret_ciphertext bytea NOT NULL,   -- nonce || AES-GCM ciphertext of {"secret_b32": ...}
  key_ref text NOT NULL,              -- DEK reference in sensitive_keys (wrapped by the master key)
  enrolled_at timestamptz NOT NULL,
  confirmed_at timestamptz,
  PRIMARY KEY (account_id, method)
);
CREATE TABLE IF NOT EXISTS recovery_codes (
  id bigserial PRIMARY KEY,
  account_id uuid NOT NULL REFERENCES accounts(id),
  code_hash text NOT NULL UNIQUE,     -- sha256 of the code; the code is shown once
  created_at timestamptz NOT NULL,
  used_at timestamptz
);
CREATE INDEX IF NOT EXISTS ix_recovery_codes_account ON recovery_codes (account_id);

-- Re-authentication proofs: one row per successful MFA verification (session-bound or, for
-- Bearer API clients, account-bound with session_id NULL). require_recent_mfa reads the latest.
CREATE TABLE IF NOT EXISTS session_mfa (
  id bigserial PRIMARY KEY,
  account_id uuid NOT NULL REFERENCES accounts(id),
  session_id uuid REFERENCES account_sessions(id),
  method text NOT NULL CHECK (method IN ('totp','recovery_code','oidc')),
  verified_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_session_mfa_lookup ON session_mfa (account_id, session_id, verified_at DESC);

ALTER TABLE account_sessions ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;

CREATE TABLE IF NOT EXISTS breakglass_sessions (
  session_id text PRIMARY KEY,                 -- bg-<hex>
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  account_id uuid NOT NULL REFERENCES accounts(id),
  scope text NOT NULL,
  reason text NOT NULL,
  started_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  ended_at timestamptz,
  ended_reason text,
  start_event_id text REFERENCES events(event_id),
  end_event_id text REFERENCES events(event_id),
  posthoc_task_id text
);
CREATE TABLE IF NOT EXISTS breakglass_actions (
  id bigserial PRIMARY KEY,
  session_id text NOT NULL REFERENCES breakglass_sessions(session_id),
  occurred_at timestamptz NOT NULL,
  method text NOT NULL,
  path text NOT NULL,
  status_code integer,
  correlation_id text NOT NULL,
  audit_id text
);

-- Failure counters per IP and per credential fingerprint (§8.1 pattern): 6 failures within
-- 15 minutes block the key for 15 minutes; rows carry no secret material.
CREATE TABLE IF NOT EXISTS auth_rate_limits (
  scope_key text PRIMARY KEY,
  window_start timestamptz NOT NULL,
  failures integer NOT NULL DEFAULT 0,
  blocked_until timestamptz
);
