-- Phase 3 (P3-01/P3-06/P3-08): Agent registry runtime state, heartbeats, limits, endpoints.
-- Secrets are never stored here: credential_ref is a Secret Broker reference only.
ALTER TABLE agents
  ADD COLUMN IF NOT EXISTS owner_account_id uuid REFERENCES accounts(id),
  ADD COLUMN IF NOT EXISTS endpoint jsonb NOT NULL DEFAULT '{}',          -- adapter config, no secret values
  ADD COLUMN IF NOT EXISTS credential_ref text,                           -- Secret Broker reference
  ADD COLUMN IF NOT EXISTS runtime_metadata jsonb NOT NULL DEFAULT '{}',  -- product/model/version/host (optional)
  ADD COLUMN IF NOT EXISTS limits jsonb NOT NULL DEFAULT '{}',            -- §7C: concurrent_tasks, requests_per_minute, brainstorm_turns, daily_cost_units, per_task_cost_units, per_task_wall_ms
  ADD COLUMN IF NOT EXISTS delivery_modes jsonb NOT NULL DEFAULT '["pull"]',
  ADD COLUMN IF NOT EXISTS capabilities_snapshot jsonb NOT NULL DEFAULT '{}', -- last probe() capabilities/identity
  ADD COLUMN IF NOT EXISTS capacity integer NOT NULL DEFAULT 1,           -- reported by heartbeat
  ADD COLUMN IF NOT EXISTS online boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS last_heartbeat_at timestamptz,
  ADD COLUMN IF NOT EXISTS missed_heartbeats integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS lifecycle_hash text,                           -- hash chain over lifecycle Events (V-P3-17)
  ADD COLUMN IF NOT EXISTS last_event_id text,
  ADD COLUMN IF NOT EXISTS last_aggregate_seq bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS agent_heartbeats (
  id bigserial PRIMARY KEY,
  agent_id text NOT NULL REFERENCES agents(agent_id),
  reported_at timestamptz NOT NULL,
  health text NOT NULL CHECK (health IN ('ok','degraded','draining')),
  capacity integer NOT NULL,
  usage jsonb,                      -- §7C usage_since_last or {"usage_unavailable": <reason>}
  event_id text REFERENCES events(event_id)
);
CREATE INDEX IF NOT EXISTS ix_agent_heartbeats_agent_time ON agent_heartbeats (agent_id, reported_at DESC);

-- per-minute request counters for Agent Limits (requests_per_minute); rows are ephemeral
CREATE TABLE IF NOT EXISTS agent_rate_windows (
  agent_id text NOT NULL REFERENCES agents(agent_id),
  window_start timestamptz NOT NULL,
  requests integer NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, window_start)
);
