-- Phase 4 (P4-05/P4-06/P4-07): Secret Broker — encrypted local provider, grants, leases, handles,
-- revocation feed, signed key ledger. No plaintext value, value length or value hash is stored
-- anywhere in this schema; handles are stored as SHA-256 hashes of the one-time handle string.

-- secret metadata (one row per secret; versions hold ciphertext)
CREATE TABLE IF NOT EXISTS secrets (
  id bigserial PRIMARY KEY,
  secret_ref text NOT NULL UNIQUE,                       -- sec-<hex>
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  name text NOT NULL,
  provider text NOT NULL,
  current_version integer NOT NULL DEFAULT 1,
  metadata jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'registered' CHECK (status IN ('registered','rotated','retired')),
  created_by uuid REFERENCES accounts(id),
  created_at timestamptz NOT NULL,
  rotated_at timestamptz,
  retired_at timestamptz,
  UNIQUE (workspace_id, name)
);

-- ciphertext per version: AES-256-GCM under a per-version DEK, DEK wrapped by the master key
CREATE TABLE IF NOT EXISTS secret_versions (
  secret_ref text NOT NULL REFERENCES secrets(secret_ref),
  version integer NOT NULL,
  dek_id text NOT NULL UNIQUE,                           -- dek://secret/<ref>/v<n>
  ciphertext bytea NOT NULL,                             -- nonce || AES-GCM(value)
  wrapped_dek bytea,                                     -- NULL once destroyed (crypto-shredding)
  master_key_id text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','destroyed')),
  created_at timestamptz NOT NULL,
  destroyed_at timestamptz,
  PRIMARY KEY (secret_ref, version),
  CHECK ((status = 'destroyed') = (wrapped_dek IS NULL))
);

-- grants: who may lease which secret, for which Task/action, with which lease defaults
CREATE TABLE IF NOT EXISTS secret_grants (
  grant_id text PRIMARY KEY,                             -- grant-<hex>
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  secret_ref text NOT NULL REFERENCES secrets(secret_ref),
  agent_id text NOT NULL REFERENCES agents(agent_id),
  task_id text,                                          -- NULL = any Task
  action text,                                           -- NULL = any action
  ttl_seconds integer NOT NULL DEFAULT 300,
  single_use boolean NOT NULL DEFAULT true,
  exposure_allowed boolean NOT NULL DEFAULT false,       -- LLM context exposure (needs approval)
  exposure_approval_id text,
  expires_at timestamptz NOT NULL,
  created_by uuid REFERENCES accounts(id),
  created_at timestamptz NOT NULL,
  revoked_at timestamptz,
  revoke_reason text
);
CREATE INDEX IF NOT EXISTS ix_secret_grants_agent ON secret_grants (agent_id, secret_ref);
CREATE INDEX IF NOT EXISTS ix_secret_grants_task ON secret_grants (task_id);

-- leases: one-time handles (hash only), scoped, short-lived, optionally bound to a sidecar
CREATE TABLE IF NOT EXISTS secret_leases (
  lease_id text PRIMARY KEY,                             -- lease-<hex>
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  grant_id text NOT NULL REFERENCES secret_grants(grant_id),
  secret_ref text NOT NULL REFERENCES secrets(secret_ref),
  handle_hash text NOT NULL UNIQUE,                      -- sha256(handle); the handle itself is never stored
  agent_id text NOT NULL,
  task_id text,
  action text,
  work_item_id text,
  sidecar_instance_id text,
  single_use boolean NOT NULL DEFAULT true,
  issued_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  use_count integer NOT NULL DEFAULT 0,
  revoked_at timestamptz,
  revoke_reason text,
  cleanup_acked_at timestamptz
);
CREATE INDEX IF NOT EXISTS ix_secret_leases_grant ON secret_leases (grant_id);
CREATE INDEX IF NOT EXISTS ix_secret_leases_task ON secret_leases (task_id);
CREATE INDEX IF NOT EXISTS ix_secret_leases_agent ON secret_leases (agent_id);

-- revocation feed consumed by sidecars (poll `since` or SSE)
CREATE TABLE IF NOT EXISTS secret_revocations (
  seq bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  kind text NOT NULL CHECK (kind IN ('grant','lease','task','agent','secret')),
  target_id text NOT NULL,
  lease_ids jsonb NOT NULL DEFAULT '[]',
  reason text NOT NULL,
  occurred_at timestamptz NOT NULL
);

-- signed key-tombstone ledger: the Phase 1 chain gains a signature made with a ledger key that is
-- separate from the master key, the DB and backups (AGENT_COLAB_LEDGER_KEY_B64)
ALTER TABLE key_tombstones ADD COLUMN IF NOT EXISTS signature text;
ALTER TABLE key_tombstones ADD COLUMN IF NOT EXISTS ledger_key_id text;
