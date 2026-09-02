-- Phase 3 (P3-06/P3-09/P3-13/P3-14): routing decisions, verifier offers, join state.
-- No secret values are stored here.
CREATE TABLE IF NOT EXISTS routing_decisions (
  id bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  purpose text NOT NULL CHECK (purpose IN ('assignment','reroute','verification')),
  task_id text,
  verification_id text,
  required_capability text,
  domain text,
  candidates jsonb NOT NULL DEFAULT '[]',      -- ordered eligible set snapshot (agent_id/account_id/score/load)
  selected_account_id uuid REFERENCES accounts(id),
  selected_agent_id text,
  reason_code text NOT NULL,                   -- SELECTED | NO_CANDIDATE | REROUTE_LIMIT
  correlation_id text NOT NULL,
  decided_at timestamptz NOT NULL,
  audit_id text
);
CREATE INDEX IF NOT EXISTS ix_routing_decisions_task ON routing_decisions (task_id, decided_at);

CREATE TABLE IF NOT EXISTS verifier_assignments (
  id bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  task_id text NOT NULL,
  verification_id text NOT NULL,
  candidate_rank integer NOT NULL,
  account_id uuid NOT NULL REFERENCES accounts(id),
  agent_id text,
  score integer NOT NULL,
  work_item_id text,
  offered_at timestamptz NOT NULL,
  accept_deadline timestamptz NOT NULL,        -- §7D.2: 10 minutes
  status text NOT NULL CHECK (status IN ('offered','accepted','timed_out','declined','exhausted')),
  resolved_at timestamptz,
  UNIQUE (task_id, candidate_rank)
);
CREATE INDEX IF NOT EXISTS ix_verifier_assignments_open ON verifier_assignments (status, accept_deadline);

CREATE TABLE IF NOT EXISTS task_join_state (
  task_id text PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  join_policy jsonb NOT NULL DEFAULT '{}',
  satisfied boolean NOT NULL DEFAULT false,
  satisfied_children jsonb NOT NULL DEFAULT '[]',
  event_id text REFERENCES events(event_id),
  updated_at timestamptz NOT NULL
);
