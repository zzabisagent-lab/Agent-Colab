-- Phase 4 (P4-03/P4-04/P4-13): setup state, settings versions, MFA enrollment storage,
-- recovery codes, maintenance mode. No secret value is stored in plaintext anywhere here.

CREATE TABLE IF NOT EXISTS setup_state (
  id smallint PRIMARY KEY CHECK (id = 1),
  state text NOT NULL CHECK (state IN ('UNINITIALIZED','PREFLIGHT_PASSED','BOOTSTRAPPING',
    'BOOTSTRAP_FAILED','CONFIGURED','LOCKED','RECONFIGURING')),
  stage_ordinal integer NOT NULL,
  instance_id text NOT NULL,
  configured_at timestamptz,
  locked_at timestamptz,
  endpoint_lock jsonb NOT NULL DEFAULT '{}',   -- {"bind": "loopback", "setup_path": "/setup", ...}
  last_failure jsonb,                            -- step/error_code/fingerprint only
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS setup_reconfiguration_sessions (
  session_id text PRIMARY KEY,
  owner_account_id uuid NOT NULL REFERENCES accounts(id),
  opened_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  closed_at timestamptz,
  close_reason text
);

-- versioned runtime settings (development plan §8.2); secret values are envelope-encrypted
CREATE TABLE IF NOT EXISTS settings_versions (
  id bigserial PRIMARY KEY,
  setting_key text NOT NULL,
  version integer NOT NULL,
  secret boolean NOT NULL DEFAULT false,
  value_json jsonb,                 -- non-secret values
  value_ciphertext bytea,           -- secret values (AES-GCM, DEK in sensitive_keys)
  key_ref text,
  value_fingerprint text,           -- sha256 prefix of the canonical value (diff/rollback links)
  changed_by uuid REFERENCES accounts(id),
  changed_at timestamptz NOT NULL,
  reason text NOT NULL DEFAULT '',
  layer text NOT NULL DEFAULT 'runtime' CHECK (layer IN ('setup_default','runtime')),
  audit_id text,
  event_id text REFERENCES events(event_id),
  UNIQUE (setting_key, version),
  CHECK ((secret AND value_ciphertext IS NOT NULL AND value_json IS NULL)
      OR (NOT secret AND value_json IS NOT NULL AND value_ciphertext IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_settings_versions_key ON settings_versions (setting_key, version DESC);

-- MFA enrollments (read by the MFA package, P4-09); the TOTP secret is envelope-encrypted
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

CREATE TABLE IF NOT EXISTS maintenance_mode (
  id smallint PRIMARY KEY CHECK (id = 1),
  active boolean NOT NULL DEFAULT false,
  reason text NOT NULL DEFAULT '',
  retry_after_s integer NOT NULL DEFAULT 300,
  entered_by uuid REFERENCES accounts(id),
  entered_at timestamptz,
  exited_by uuid REFERENCES accounts(id),
  exited_at timestamptz
);
INSERT INTO maintenance_mode (id, active) VALUES (1, false) ON CONFLICT (id) DO NOTHING;
