-- Phase 5 (P5-09): scheduler metrics and alerts (owned by the metrics package).
-- Metrics themselves derive from schedule_runs history (ADR-0012 decision 8); these tables hold
-- only what history cannot express: planner conflicts that never became rows, and alert
-- emissions for hourly deduplication.
CREATE TABLE IF NOT EXISTS schedule_metrics_counters (
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  schedule_id text NOT NULL,
  counter text NOT NULL CHECK (counter IN ('duplicates_prevented')),
  value bigint NOT NULL DEFAULT 0 CHECK (value >= 0),
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (workspace_id, schedule_id, counter)
);

CREATE TABLE IF NOT EXISTS schedule_alert_emissions (
  id bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  alert_key text NOT NULL,
  hour_bucket timestamptz NOT NULL,
  severity text NOT NULL CHECK (severity IN ('info','warning','critical')),
  payload jsonb NOT NULL DEFAULT '{}',
  emitted_at timestamptz NOT NULL,
  UNIQUE (workspace_id, alert_key, hour_bucket)
);
