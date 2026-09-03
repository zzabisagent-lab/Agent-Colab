-- Phase 4 (P4-01/P4-02/P4-11): account admin, dependency probes, backups metadata, hard-delete workflow.
-- No secret values are stored here (backups hold paths/digests only; the master key never enters the DB).

CREATE TABLE IF NOT EXISTS hard_delete_requests (
  request_id text PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  target_type text NOT NULL CHECK (target_type IN ('account','conversation','artifact','document')),
  target_id text NOT NULL,
  reason text NOT NULL,
  requested_by uuid NOT NULL REFERENCES accounts(id),
  approval_id text,
  status text NOT NULL CHECK (status IN ('PENDING_APPROVAL','APPROVED_WAITING','EXECUTED','REJECTED','CANCELLED')),
  waiting_period_hours integer NOT NULL,
  approved_at timestamptz,
  executable_at timestamptz,
  executed_at timestamptz,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hard_delete_requests_target ON hard_delete_requests (workspace_id, target_type, target_id);

-- display-redaction marker + execution record; append-only (immutable like key_tombstones)
CREATE TABLE IF NOT EXISTS hard_delete_tombstones (
  id bigserial PRIMARY KEY,
  request_id text NOT NULL UNIQUE REFERENCES hard_delete_requests(request_id),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  target_type text NOT NULL,
  target_id text NOT NULL,
  executed_at timestamptz NOT NULL,
  executed_by uuid NOT NULL REFERENCES accounts(id),
  approvals jsonb NOT NULL DEFAULT '[]',        -- approver public ids + decision timestamps
  keys_destroyed jsonb NOT NULL DEFAULT '[]',   -- key_refs shredded (no key material)
  ledger_entry_hash text,                       -- last key-tombstone ledger hash written
  event_hash_before text NOT NULL,              -- digest over the Workspace Event chain before
  event_hash_after text NOT NULL,               -- ... and after execution (must be identical)
  event_id text REFERENCES events(event_id)
);
DROP TRIGGER IF EXISTS trg_hard_delete_tombstones_immutable ON hard_delete_tombstones;
CREATE TRIGGER trg_hard_delete_tombstones_immutable BEFORE UPDATE OR DELETE ON hard_delete_tombstones
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

CREATE TABLE IF NOT EXISTS dependency_probes (
  name text PRIMARY KEY,
  ok boolean,                                   -- NULL = not configured / optional
  detail text NOT NULL DEFAULT '',
  latency_ms integer,
  checked_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS backups (
  backup_id text PRIMARY KEY,
  path text NOT NULL,
  size_bytes bigint NOT NULL,
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL,
  created_by text NOT NULL,
  tool_version text NOT NULL DEFAULT '',
  includes_master_key boolean NOT NULL DEFAULT false CHECK (includes_master_key = false),
  includes_ledger boolean NOT NULL DEFAULT false
);
