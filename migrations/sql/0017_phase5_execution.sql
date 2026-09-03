-- Phase 5 (P5-04/P5-05/P5-06/P5-07/P5-10): execution policy checks, notifications, budget settlement
-- (owned by the execution package). Run rows themselves live in migration 0016 (schedule core).

-- transient retry scheduling (§10A.2 step 10): one open retry per Run
CREATE TABLE IF NOT EXISTS schedule_run_retries (
  run_id text PRIMARY KEY,
  next_attempt_no integer NOT NULL CHECK (next_attempt_no >= 2),
  next_attempt_at timestamptz NOT NULL,
  error_code text NOT NULL,
  created_at timestamptz NOT NULL
);

-- per-Run budget reservations (§7C): links a Run to its budget_reservations rows for settlement
CREATE TABLE IF NOT EXISTS schedule_run_budgets (
  run_id text NOT NULL,
  scope_type text NOT NULL CHECK (scope_type IN ('schedule_run','schedule_daily')),
  reservation_id text NOT NULL,
  limit_cost_units integer NOT NULL,
  estimate_cost_units integer NOT NULL,
  status text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','settled','exceeded','released')),
  settled_cost_units integer,
  settled_at timestamptz,
  PRIMARY KEY (run_id, scope_type)
);

-- alerts the metrics package and the dashboard read (budget overruns, start-delay p95, backfill truncation, timeouts)
CREATE TABLE IF NOT EXISTS budget_alerts (
  id bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  kind text NOT NULL CHECK (kind IN ('budget_exceeded','start_delay','backfill_truncated','timeout','cancel_timeout')),
  schedule_id text,
  run_id text,
  detail jsonb NOT NULL DEFAULT '{}',
  raised_at timestamptz NOT NULL,
  notified boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS ix_budget_alerts_ws_time ON budget_alerts (workspace_id, raised_at DESC);

-- channel notices per Run (start/result/failure/skip/late), one row per kind and Run
CREATE TABLE IF NOT EXISTS schedule_notices (
  run_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('start','result','failure','skip','late','backfill_warning')),
  dedupe_key text NOT NULL,
  outbox_id text,
  channel_id uuid,
  created_at timestamptz NOT NULL,
  PRIMARY KEY (run_id, kind)
);

-- schedule notices are channel posts of their own role (subject_type schedule_run exists since 0009)
ALTER TABLE channel_posts DROP CONSTRAINT IF EXISTS channel_posts_role_check;
ALTER TABLE channel_posts ADD CONSTRAINT channel_posts_role_check
  CHECK (role IN ('card','reply','link_card','ephemeral','work_message','notice'));
