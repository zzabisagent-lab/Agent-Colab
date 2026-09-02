-- Phase 1 core schema (development plan §6.1–6.8). Authority tables are append-only; projections
-- are rebuildable. Runtime/admin application roles have no UPDATE/DELETE on authority tables and
-- triggers block modification regardless of role (V-P1-05, V-P1-25).

-- ---------------------------------------------------------------- application roles
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_colab_runtime') THEN
    CREATE ROLE agent_colab_runtime NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_colab_admin') THEN
    CREATE ROLE agent_colab_admin NOLOGIN;
  END IF;
END $$;

-- ---------------------------------------------------------------- immutability helpers
CREATE OR REPLACE FUNCTION agent_colab_forbid_modification() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'IMMUTABLE_ROW: % on % is forbidden', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------- events (authority)
CREATE TABLE events (
  id uuid PRIMARY KEY,
  recorded_seq bigserial NOT NULL UNIQUE,
  event_id text NOT NULL UNIQUE,
  schema_version smallint NOT NULL,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  aggregate_type text NOT NULL,
  aggregate_id text NOT NULL,
  aggregate_seq bigint NOT NULL,
  channel_id uuid,
  task_id text,
  type text NOT NULL,
  actor_account_id uuid NOT NULL REFERENCES accounts(id),
  caused_by text REFERENCES events(event_id),
  correlation_id text NOT NULL,
  idempotency_scope text NOT NULL,
  idempotency_key text NOT NULL,
  request_body_hash text NOT NULL,
  policy_version text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  sensitive_payload_ciphertext bytea,
  sensitive_payload_key_ref text,
  previous_hash text,
  content_hash text NOT NULL,
  occurred_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, aggregate_type, aggregate_id, aggregate_seq),
  UNIQUE (workspace_id, actor_account_id, idempotency_scope, idempotency_key),
  CHECK (aggregate_seq > 0),
  CHECK ((sensitive_payload_ciphertext IS NULL) = (sensitive_payload_key_ref IS NULL)),
  CHECK ((aggregate_seq = 1) = (previous_hash IS NULL)),
  CHECK (content_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX events_aggregate_idx ON events (workspace_id, aggregate_type, aggregate_id, aggregate_seq);
CREATE INDEX events_task_idx ON events (task_id) WHERE task_id IS NOT NULL;
CREATE INDEX events_type_idx ON events (workspace_id, type, recorded_seq);
CREATE TRIGGER trg_events_immutable BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

-- ---------------------------------------------------------------- sensitive keys / tombstones
CREATE TABLE sensitive_keys (
  key_ref text PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  target_type text NOT NULL,
  target_id text NOT NULL,
  wrapped_dek bytea,
  master_key_id text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','destroyed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  destroyed_at timestamptz,
  CHECK ((status = 'destroyed') = (wrapped_dek IS NULL))
);
CREATE TABLE key_tombstones (
  id bigserial PRIMARY KEY,
  key_ref text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL,
  target_type text NOT NULL,
  target_id text NOT NULL,
  reason text NOT NULL,
  requested_by uuid NOT NULL REFERENCES accounts(id),
  audit_event_id text,
  previous_hash text,
  content_hash text NOT NULL,
  destroyed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_key_tombstones_immutable BEFORE UPDATE OR DELETE ON key_tombstones
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

-- ---------------------------------------------------------------- audit (authority)
CREATE TABLE audit_events (
  id bigserial PRIMARY KEY,
  audit_id text NOT NULL UNIQUE,
  workspace_id uuid REFERENCES workspaces(id),
  actor_account_id uuid REFERENCES accounts(id),
  actor_label text NOT NULL,
  action text NOT NULL,
  target_type text NOT NULL,
  target_id text NOT NULL,
  result text NOT NULL,
  error_code text,
  correlation_id text NOT NULL,
  redacted_metadata jsonb NOT NULL DEFAULT '{}',
  previous_hash text,
  content_hash text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_events_actor_idx ON audit_events (actor_account_id, occurred_at);
CREATE INDEX audit_events_action_idx ON audit_events (action, occurred_at);
CREATE TRIGGER trg_audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

CREATE TABLE audit_hash_anchors (
  id bigserial PRIMARY KEY,
  chain text NOT NULL,
  anchor_date date NOT NULL,
  last_row_id bigint NOT NULL,
  last_hash text NOT NULL,
  anchor_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (chain, anchor_date)
);
CREATE TRIGGER trg_audit_hash_anchors_immutable BEFORE UPDATE OR DELETE ON audit_hash_anchors
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

-- ---------------------------------------------------------------- verification (authority)
ALTER TABLE verification_runs ADD COLUMN current_revision integer NOT NULL DEFAULT 0;
ALTER TABLE verification_runs ADD COLUMN result text;
ALTER TABLE verification_runs ADD CONSTRAINT ck_vr_result
  CHECK (result IS NULL OR result IN ('PASSED','FAILED','BLOCKED'));

CREATE TABLE verification_revisions (
  id bigserial PRIMARY KEY,
  revision_id text NOT NULL UNIQUE,
  verification_id text NOT NULL REFERENCES verification_runs(verification_id),
  revision integer NOT NULL,
  result text NOT NULL CHECK (result IN ('PASSED','FAILED','BLOCKED','CANCELLED')),
  submitted_by_account_id uuid NOT NULL REFERENCES accounts(id),
  submitter_credential_fingerprint text NOT NULL,
  report jsonb NOT NULL,
  report_sha256 text NOT NULL,
  event_id text NOT NULL REFERENCES events(event_id),
  previous_hash text,
  content_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (verification_id, revision)
);
CREATE TRIGGER trg_verification_revisions_immutable BEFORE UPDATE OR DELETE ON verification_revisions
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

CREATE TABLE verification_evidence (
  id bigserial PRIMARY KEY,
  verification_id text NOT NULL REFERENCES verification_runs(verification_id),
  revision integer,
  evidence_ref text NOT NULL,
  sha256 text,
  submitted_by_account_id uuid NOT NULL REFERENCES accounts(id),
  submitted_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_verification_evidence_immutable BEFORE UPDATE OR DELETE ON verification_evidence
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

CREATE TABLE verification_findings (
  id bigserial PRIMARY KEY,
  finding_id text NOT NULL UNIQUE,
  verification_id text NOT NULL REFERENCES verification_runs(verification_id),
  revision integer NOT NULL,
  severity text NOT NULL CHECK (severity IN ('Critical','High','Medium','Low')),
  summary text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_verification_findings_immutable BEFORE UPDATE OR DELETE ON verification_findings
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

CREATE TABLE credential_identity_snapshots (
  id bigserial PRIMARY KEY,
  verification_id text NOT NULL REFERENCES verification_runs(verification_id),
  snapshot jsonb NOT NULL,
  snapshot_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_credential_identity_snapshots_immutable BEFORE UPDATE OR DELETE ON credential_identity_snapshots
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

-- ---------------------------------------------------------------- identity
CREATE TABLE account_sessions (
  id uuid PRIMARY KEY,
  account_id uuid NOT NULL REFERENCES accounts(id),
  session_token_hash text NOT NULL UNIQUE,
  fingerprint text NOT NULL,
  mfa_verified_at timestamptz,
  reauth_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz
);
CREATE TABLE provider_instances (
  id uuid PRIMARY KEY,
  provider_instance_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  provider text NOT NULL CHECK (provider IN ('mattermost','telegram')),
  base_url text,
  team_or_bot_ref text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE external_identity_links (
  id uuid PRIMARY KEY,
  link_id text NOT NULL UNIQUE,
  provider_instance_id uuid NOT NULL REFERENCES provider_instances(id),
  external_user_id text NOT NULL,
  account_id uuid NOT NULL REFERENCES accounts(id),
  verification_method text NOT NULL CHECK (verification_method IN ('signed_challenge','admin_approval')),
  status text NOT NULL CHECK (status IN ('pending','pending_admin','active','suspended','revoked')),
  verified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider_instance_id, external_user_id)
);
CREATE UNIQUE INDEX external_identity_links_one_active_account_idx
  ON external_identity_links (provider_instance_id, external_user_id) WHERE status = 'active';
CREATE TABLE identity_link_challenges (
  id uuid PRIMARY KEY,
  provider_instance_id uuid NOT NULL REFERENCES provider_instances(id),
  external_user_id text NOT NULL,
  code_hash text NOT NULL,
  account_id uuid REFERENCES accounts(id),
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  failures integer NOT NULL DEFAULT 0,
  locked_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- roles / capabilities
CREATE TABLE roles (
  id uuid PRIMARY KEY,
  role_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  display_name text NOT NULL,
  current_version integer NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE role_versions (
  id uuid PRIMARY KEY,
  role_id text NOT NULL REFERENCES roles(role_id),
  version integer NOT NULL,
  permissions jsonb NOT NULL,
  deny jsonb NOT NULL DEFAULT '[]',
  constraints jsonb NOT NULL DEFAULT '{}',
  policy_hash text NOT NULL,
  event_id text REFERENCES events(event_id),
  created_by uuid NOT NULL REFERENCES accounts(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (role_id, version)
);
CREATE TRIGGER trg_role_versions_immutable BEFORE UPDATE OR DELETE ON role_versions
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();
CREATE TABLE principal_role_assignments (
  id uuid PRIMARY KEY,
  account_id uuid NOT NULL REFERENCES accounts(id),
  role_id text NOT NULL REFERENCES roles(role_id),
  scope jsonb NOT NULL DEFAULT '{}',
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz,
  assigned_by uuid NOT NULL REFERENCES accounts(id),
  event_id text REFERENCES events(event_id),
  revoked_at timestamptz,
  revoke_event_id text REFERENCES events(event_id)
);
CREATE INDEX principal_role_assignments_account_idx ON principal_role_assignments (account_id) WHERE revoked_at IS NULL;
CREATE TABLE capabilities (
  id uuid PRIMARY KEY,
  capability_id text NOT NULL UNIQUE,
  tool text NOT NULL,
  domain text,
  resource text,
  side_effect boolean NOT NULL DEFAULT false,
  schema_ref text,
  limits jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE agent_capabilities (
  agent_id text NOT NULL REFERENCES agents(agent_id),
  capability_id text NOT NULL REFERENCES capabilities(capability_id),
  PRIMARY KEY (agent_id, capability_id)
);

-- ---------------------------------------------------------------- channels (minimal, Phase 2 extends)
CREATE TABLE channels (
  id uuid PRIMARY KEY,
  channel_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  provider_instance_id uuid REFERENCES provider_instances(id),
  external_channel_id text,
  channel_type text NOT NULL CHECK (channel_type IN ('work','brainstorm','approval','ops','custom')),
  display_name text NOT NULL,
  policy jsonb NOT NULL DEFAULT '{}',
  policy_version text NOT NULL DEFAULT 'policy-v1',
  documentation_template text,
  language text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived','deleted')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE channel_members (
  channel_id uuid NOT NULL REFERENCES channels(id),
  account_id uuid NOT NULL REFERENCES accounts(id),
  permissions jsonb NOT NULL DEFAULT '["read","write"]',
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','removed')),
  PRIMARY KEY (channel_id, account_id)
);

-- ---------------------------------------------------------------- tasks (projection + append-only history)
CREATE TABLE tasks_projection (
  task_id text PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  root_task_id text NOT NULL,
  parent_task_id text,
  channel_id uuid REFERENCES channels(id),
  title text NOT NULL,
  domain text NOT NULL,
  risk text NOT NULL CHECK (risk IN ('LOW','MEDIUM','HIGH','CRITICAL')),
  status text NOT NULL CHECK (status IN ('OPEN','DELEGATED','ACCEPTED','RUNNING','WAITING','IMPLEMENTED','VERIFYING','VERIFIED','COMPLETED','CANCEL_REQUESTED','CANCELLED')),
  verification_status text,
  assignee_account_id uuid REFERENCES accounts(id),
  delegated_by uuid REFERENCES accounts(id),
  delegation_depth integer NOT NULL DEFAULT 0,
  join_policy jsonb NOT NULL DEFAULT '{}',
  policy_snapshot jsonb NOT NULL DEFAULT '{}',
  policy_snapshot_hash text,
  criteria_revision integer NOT NULL DEFAULT 0,
  latest_progress text,
  last_event_id text,
  last_aggregate_seq bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);
CREATE TABLE task_edges (
  child_task_id text PRIMARY KEY,
  parent_task_id text NOT NULL,
  root_task_id text NOT NULL,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  depth integer NOT NULL CHECK (depth > 0),
  created_event_id text NOT NULL REFERENCES events(event_id),
  CHECK (child_task_id <> parent_task_id)
);
CREATE TRIGGER trg_task_edges_immutable BEFORE UPDATE OR DELETE ON task_edges
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();
CREATE OR REPLACE FUNCTION agent_colab_task_edges_no_cycle() RETURNS trigger AS $$
DECLARE cur text := NEW.parent_task_id; hops integer := 0;
BEGIN
  WHILE cur IS NOT NULL LOOP
    IF cur = NEW.child_task_id THEN
      RAISE EXCEPTION 'TASK_GRAPH_CYCLE' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT parent_task_id INTO cur FROM task_edges WHERE child_task_id = cur;
    hops := hops + 1;
    IF hops > 64 THEN RAISE EXCEPTION 'TASK_GRAPH_TOO_DEEP' USING ERRCODE = 'integrity_constraint_violation'; END IF;
  END LOOP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_task_edges_no_cycle BEFORE INSERT ON task_edges
  FOR EACH ROW EXECUTE FUNCTION agent_colab_task_edges_no_cycle();
CREATE TABLE task_assignments (
  task_id text NOT NULL,
  revision integer NOT NULL,
  delegator_account_id uuid NOT NULL REFERENCES accounts(id),
  assignee_account_id uuid NOT NULL REFERENCES accounts(id),
  reason_code text NOT NULL,
  policy_snapshot_hash text NOT NULL,
  resume_context jsonb,
  event_id text NOT NULL REFERENCES events(event_id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, revision)
);
CREATE TRIGGER trg_task_assignments_immutable BEFORE UPDATE OR DELETE ON task_assignments
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();
CREATE TABLE task_acceptance_criteria (
  criteria_id text PRIMARY KEY,
  task_id text NOT NULL,
  revision integer NOT NULL,
  statement text NOT NULL,
  check_type text NOT NULL CHECK (check_type IN ('evidence','test_command','artifact_hash','human_attest')),
  required boolean NOT NULL DEFAULT true,
  event_id text NOT NULL REFERENCES events(event_id),
  UNIQUE (task_id, revision, criteria_id)
);
CREATE TRIGGER trg_task_acceptance_criteria_immutable BEFORE UPDATE OR DELETE ON task_acceptance_criteria
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

-- ---------------------------------------------------------------- work items (durable inbox)
CREATE TABLE work_items (
  work_item_id text PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  kind text NOT NULL CHECK (kind IN ('task_assignment','subtask_assignment','invoke','cancel','brainstorm_turn','verification_assignment')),
  agent_id text NOT NULL,
  task_id text,
  brainstorm_id text,
  correlation_id text NOT NULL,
  deadline timestamptz NOT NULL,
  payload jsonb NOT NULL,
  secret_handles jsonb NOT NULL DEFAULT '[]',
  expected_result_schema text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  status text NOT NULL CHECK (status IN ('QUEUED','DELIVERED','ACKED','IN_PROGRESS','RESULT_RECEIVED','REJECTED','EXPIRED','CANCELLED')),
  delivery_count integer NOT NULL DEFAULT 0,
  delivered_at timestamptz,
  acked_at timestamptz,
  accepted_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  CHECK ((kind = 'brainstorm_turn') = (brainstorm_id IS NOT NULL))
);
CREATE INDEX work_items_agent_status_idx ON work_items (agent_id, status);
CREATE TABLE work_item_receipts (
  id bigserial PRIMARY KEY,
  work_item_id text NOT NULL REFERENCES work_items(work_item_id),
  receipt_kind text NOT NULL CHECK (receipt_kind IN ('delivery','ack','accept','reject','result','duplicate_result','cancel_ack')),
  delivery_no integer,
  result_ref text,
  result_sha256 text,
  usage jsonb,
  detail jsonb NOT NULL DEFAULT '{}',
  received_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX work_item_receipts_one_result_idx ON work_item_receipts (work_item_id) WHERE receipt_kind = 'result';
CREATE TRIGGER trg_work_item_receipts_immutable BEFORE UPDATE OR DELETE ON work_item_receipts
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

-- ---------------------------------------------------------------- approvals (grant authority + consumption ledger + projection)
CREATE TABLE approval_grants (
  id uuid PRIMARY KEY,
  approval_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  subject_type text NOT NULL CHECK (subject_type IN ('task','schedule','run','action')),
  subject_id text NOT NULL,
  action text NOT NULL,
  resource_scope jsonb NOT NULL DEFAULT '{}',
  risk text NOT NULL CHECK (risk IN ('LOW','MEDIUM','HIGH','CRITICAL')),
  status text NOT NULL CHECK (status IN ('PENDING','APPROVED','PARTIALLY_CONSUMED','CONSUMED','REJECTED','CANCELLED','EXPIRED','REVOKED')),
  requested_by uuid NOT NULL REFERENCES accounts(id),
  implementing_agent_account_id uuid REFERENCES accounts(id),
  channel_id uuid REFERENCES channels(id),
  valid_from timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  max_uses integer,
  quorum_required integer NOT NULL DEFAULT 1,
  aggregate_seq bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (expires_at > valid_from),
  CHECK (max_uses IS NULL OR max_uses > 0)
);
CREATE TABLE approval_decisions (
  id bigserial PRIMARY KEY,
  approval_id text NOT NULL REFERENCES approval_grants(approval_id),
  decided_by uuid NOT NULL REFERENCES accounts(id),
  decision text NOT NULL CHECK (decision IN ('APPROVE','REJECT')),
  credential_fingerprint text NOT NULL,
  reauth_verified boolean NOT NULL DEFAULT false,
  event_id text NOT NULL REFERENCES events(event_id),
  decided_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (approval_id, decided_by)
);
CREATE TRIGGER trg_approval_decisions_immutable BEFORE UPDATE OR DELETE ON approval_decisions
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();
CREATE TABLE approval_consumptions (
  id bigserial PRIMARY KEY,
  approval_id text NOT NULL REFERENCES approval_grants(approval_id),
  consumption_key text NOT NULL,
  consumed_by uuid NOT NULL REFERENCES accounts(id),
  consumed_for_type text NOT NULL,
  consumed_for_id text NOT NULL,
  event_id text NOT NULL REFERENCES events(event_id),
  consumed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (approval_id, consumption_key)
);
CREATE TRIGGER trg_approval_consumptions_immutable BEFORE UPDATE OR DELETE ON approval_consumptions
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();
CREATE TABLE approvals_projection (
  approval_id text PRIMARY KEY,
  workspace_id uuid NOT NULL,
  subject_type text NOT NULL,
  subject_id text NOT NULL,
  action text NOT NULL,
  risk text NOT NULL,
  status text NOT NULL,
  used_count integer NOT NULL DEFAULT 0,
  max_uses integer,
  expires_at timestamptz NOT NULL,
  requested_by uuid NOT NULL,
  decided_by jsonb NOT NULL DEFAULT '[]',
  last_event_id text,
  updated_at timestamptz NOT NULL
);

-- ---------------------------------------------------------------- artifacts / documents
CREATE TABLE artifacts (
  id uuid PRIMARY KEY,
  artifact_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  creator_account_id uuid NOT NULL REFERENCES accounts(id),
  storage_uri text NOT NULL,
  mime text NOT NULL,
  size bigint NOT NULL CHECK (size >= 0),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  acl jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'registered' CHECK (status IN ('registered','verified','quarantined','archived')),
  source_event_id text NOT NULL REFERENCES events(event_id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE artifact_links (
  artifact_id text NOT NULL REFERENCES artifacts(artifact_id),
  subject_type text NOT NULL CHECK (subject_type IN ('task','schedule_run','brainstorm','decision')),
  subject_id text NOT NULL,
  relation text NOT NULL,
  linked_by uuid NOT NULL REFERENCES accounts(id),
  linked_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (artifact_id, subject_type, subject_id, relation)
);
CREATE TABLE documents (
  id uuid PRIMARY KEY,
  document_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  doc_type text NOT NULL CHECK (doc_type IN ('task','brainstorm','schedule_run','period')),
  source_type text NOT NULL,
  source_id text NOT NULL,
  current_version integer NOT NULL DEFAULT 0,
  status text NOT NULL CHECK (status IN ('DRAFT_PRE_VERIFICATION','ATTEMPT_FINALIZED','FINALIZED','REVIEWED','PUBLISHED')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE document_versions (
  id uuid PRIMARY KEY,
  document_id text NOT NULL REFERENCES documents(document_id),
  version integer NOT NULL,
  status text NOT NULL CHECK (status IN ('DRAFT_PRE_VERIFICATION','ATTEMPT_FINALIZED','FINALIZED','REVIEWED','PUBLISHED')),
  verification_id text REFERENCES verification_runs(verification_id),
  verification_result text,
  storage_uri text NOT NULL,
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  manifest jsonb NOT NULL,
  source_freeze_event_seq bigint NOT NULL,
  event_id text NOT NULL REFERENCES events(event_id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, version)
);
CREATE TRIGGER trg_document_versions_immutable BEFORE UPDATE OR DELETE ON document_versions
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();
CREATE TABLE document_publications (
  id bigserial PRIMARY KEY,
  document_id text NOT NULL REFERENCES documents(document_id),
  version integer NOT NULL,
  publisher text NOT NULL,
  external_ref text,
  status text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- usage / budget / notifications / outbox
CREATE TABLE pricing_versions (
  pricing_version text PRIMARY KEY,
  table_json jsonb NOT NULL,
  table_sha256 text NOT NULL,
  activated_by uuid REFERENCES accounts(id),
  activated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE usage_records (
  id bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  agent_id text,
  account_id uuid NOT NULL REFERENCES accounts(id),
  task_id text,
  run_id text,
  brainstorm_id text,
  document_id text,
  work_item_id text,
  model text,
  input_tokens bigint NOT NULL DEFAULT 0,
  output_tokens bigint NOT NULL DEFAULT 0,
  tool_calls integer NOT NULL DEFAULT 0,
  wall_ms bigint NOT NULL DEFAULT 0,
  cost_units bigint NOT NULL CHECK (cost_units >= 0),
  source text NOT NULL CHECK (source IN ('reported','computed','estimated','unavailable')),
  unavailable_reason text,
  pricing_version text NOT NULL REFERENCES pricing_versions(pricing_version),
  reported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_usage_records_immutable BEFORE UPDATE OR DELETE ON usage_records
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();
CREATE TABLE budget_reservations (
  id uuid PRIMARY KEY,
  reservation_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  scope_type text NOT NULL CHECK (scope_type IN ('agent_daily','agent_task','channel_daily','schedule_run','schedule_daily')),
  scope_id text NOT NULL,
  work_item_id text,
  estimated_cost_units bigint NOT NULL CHECK (estimated_cost_units >= 0),
  settled_cost_units bigint,
  status text NOT NULL CHECK (status IN ('reserved','settled','released','exceeded')),
  created_at timestamptz NOT NULL DEFAULT now(),
  settled_at timestamptz
);
CREATE TABLE notification_rules (
  rule_id text PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  event_type text NOT NULL,
  recipient_selector jsonb NOT NULL,
  channels jsonb NOT NULL,
  dedupe_window_seconds integer NOT NULL DEFAULT 0,
  quiet_hours jsonb,
  enabled boolean NOT NULL DEFAULT true,
  version integer NOT NULL DEFAULT 1
);
CREATE TABLE notification_preferences (
  account_id uuid PRIMARY KEY REFERENCES accounts(id),
  muted boolean NOT NULL DEFAULT false,
  digest boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE notifications (
  notification_id text PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  rule_id text NOT NULL REFERENCES notification_rules(rule_id),
  source_event_id text NOT NULL REFERENCES events(event_id),
  recipient_account_id uuid NOT NULL REFERENCES accounts(id),
  channel text NOT NULL,
  dedupe_key text NOT NULL,
  status text NOT NULL CHECK (status IN ('queued','sent','suppressed','failed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz,
  UNIQUE (dedupe_key)
);
CREATE TABLE delivery_outbox (
  id bigserial PRIMARY KEY,
  outbox_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  kind text NOT NULL,
  destination text NOT NULL,
  dedupe_key text NOT NULL UNIQUE,
  payload jsonb NOT NULL,
  source_event_id text REFERENCES events(event_id),
  status text NOT NULL CHECK (status IN ('pending','sent','failed','dead')),
  attempts integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz
);
CREATE INDEX delivery_outbox_pending_idx ON delivery_outbox (next_attempt_at) WHERE status = 'pending';
CREATE TABLE projection_checkpoints (
  projection text PRIMARY KEY,
  last_recorded_seq bigint NOT NULL DEFAULT 0,
  snapshot_hash text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE system_settings (
  setting_key text PRIMARY KEY,
  value_json jsonb,
  value_ref text,
  scope text NOT NULL DEFAULT 'instance',
  secret boolean NOT NULL DEFAULT false,
  version integer NOT NULL DEFAULT 1,
  source text NOT NULL DEFAULT 'setup',
  changed_by uuid REFERENCES accounts(id),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- grants (deny-by-default per table)
GRANT USAGE ON SCHEMA public TO agent_colab_runtime, agent_colab_admin;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO agent_colab_runtime, agent_colab_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agent_colab_runtime, agent_colab_admin;
-- mutable tables (projections, lifecycle state) may be updated by the application roles
GRANT UPDATE ON accounts, agents, workspaces, account_sessions, provider_instances,
  external_identity_links, identity_link_challenges, roles, principal_role_assignments,
  capabilities, channels, channel_members, tasks_projection, work_items, approval_grants,
  approvals_projection, artifacts, artifact_links, documents, document_publications,
  budget_reservations, notification_rules, notification_preferences, notifications,
  delivery_outbox, projection_checkpoints, system_settings, sensitive_keys, verification_runs,
  service_credentials TO agent_colab_runtime, agent_colab_admin;
GRANT DELETE ON tasks_projection, approvals_projection, projection_checkpoints, identity_link_challenges
  TO agent_colab_runtime, agent_colab_admin;
-- explicitly no UPDATE/DELETE on authority tables (events, audit_events, audit_hash_anchors,
-- verification_revisions/evidence/findings, credential_identity_snapshots, key_tombstones,
-- role_versions, task_edges, task_assignments, task_acceptance_criteria, work_item_receipts,
-- approval_decisions, approval_consumptions, document_versions, usage_records)
REVOKE UPDATE, DELETE ON events, audit_events, audit_hash_anchors, verification_revisions,
  verification_evidence, verification_findings, credential_identity_snapshots, key_tombstones,
  role_versions, task_edges, task_assignments, task_acceptance_criteria, work_item_receipts,
  approval_decisions, approval_consumptions, document_versions, usage_records
  FROM agent_colab_runtime, agent_colab_admin;
