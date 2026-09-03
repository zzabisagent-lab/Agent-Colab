-- Phase 5 (P5-01/P5-02/P5-03): Schedules, immutable ScheduleVersions, durable ScheduleRuns with
-- occurrence keys and runner leases, attempts, planner notes (development plan §6.6, §10A).
-- No secret values are stored here: action templates hold Secret references only (validated).

CREATE TABLE IF NOT EXISTS schedules (
  id uuid PRIMARY KEY,
  schedule_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  name text NOT NULL,
  status text NOT NULL DEFAULT 'DRAFT'
    CHECK (status IN ('DRAFT','ENABLED','PAUSED','DISABLED')),
  current_version_id uuid,
  next_run_at timestamptz,
  last_planned_until timestamptz,
  last_event_id text,
  created_by uuid NOT NULL REFERENCES accounts(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schedule_versions (
  id uuid PRIMARY KEY,
  schedule_version_id text NOT NULL UNIQUE,
  schedule_id text NOT NULL REFERENCES schedules(schedule_id),
  version integer NOT NULL CHECK (version >= 1),
  name text NOT NULL,
  channel_id uuid NOT NULL REFERENCES channels(id),
  cron_expression text NOT NULL,
  timezone text NOT NULL,
  execution_principal_id uuid NOT NULL REFERENCES accounts(id),
  agent_selection jsonb NOT NULL,
  action_template jsonb NOT NULL,
  concurrency_policy text NOT NULL CHECK (concurrency_policy IN ('FORBID','ALLOW','REPLACE')),
  missed_run_policy text NOT NULL CHECK (missed_run_policy IN ('SKIP','RUN_ONCE','BACKFILL_LIMITED')),
  backfill_limit integer NOT NULL CHECK (backfill_limit >= 0 AND backfill_limit <= 1000),
  backfill_window_seconds integer NOT NULL CHECK (backfill_window_seconds >= 0),
  max_duration_seconds integer NOT NULL CHECK (max_duration_seconds >= 0 AND max_duration_seconds <= 86400),
  min_interval_minutes integer NOT NULL DEFAULT 5 CHECK (min_interval_minutes >= 1),
  retry_policy jsonb NOT NULL,
  budget_policy jsonb NOT NULL,
  documentation_policy jsonb NOT NULL,
  starts_at timestamptz,
  ends_at timestamptz,
  snapshot_hash text NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
  created_by uuid NOT NULL REFERENCES accounts(id),
  event_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (schedule_id, version),
  CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at)
);

-- ScheduleVersions are immutable snapshots (§6.6): only inserts are allowed.
CREATE OR REPLACE FUNCTION agent_colab_forbid_schedule_version_change() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'schedule_versions is immutable (%)', TG_OP USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_schedule_versions_immutable ON schedule_versions;
CREATE TRIGGER trg_schedule_versions_immutable
  BEFORE UPDATE OR DELETE ON schedule_versions
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_schedule_version_change();

CREATE TABLE IF NOT EXISTS schedule_runs (
  id uuid PRIMARY KEY,
  run_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  schedule_id text NOT NULL REFERENCES schedules(schedule_id),
  schedule_version_id uuid NOT NULL REFERENCES schedule_versions(id),
  run_kind text NOT NULL CHECK (run_kind IN ('SCHEDULED','MANUAL','RETRY')),
  occurrence_key text,
  scheduled_for timestamptz NOT NULL,
  local_scheduled_for timestamp,
  retry_of_run_id text REFERENCES schedule_runs(run_id),
  request_key text,                       -- caller idempotency key of MANUAL/RETRY requests
  status text NOT NULL CHECK (status IN (
    'PENDING','DUE','CLAIMED','TASK_CREATED','RUNNING','VERIFYING',
    'SUCCEEDED','FAILED','SKIPPED','TIMED_OUT','CANCEL_REQUESTED','CANCELLED'
  )),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  task_id text,
  idempotency_key text NOT NULL,
  version_hash text NOT NULL,             -- snapshot_hash of the pinned version (V-P5-33)
  claimed_by text,
  claimed_at timestamptz,
  lease_expires_at timestamptz,
  heartbeat_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  result_event_id text,
  error_code text,
  cancel_requested_at timestamptz,
  cancelled_at timestamptz,
  planner_note text,                      -- e.g. BACKFILL_TRUNCATED / MISSED_RUN_ONCE
  requested_by uuid REFERENCES accounts(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (schedule_id, idempotency_key),
  UNIQUE (schedule_id, occurrence_key),
  CHECK (
    (run_kind = 'SCHEDULED' AND occurrence_key IS NOT NULL AND retry_of_run_id IS NULL) OR
    (run_kind = 'MANUAL' AND occurrence_key IS NULL AND retry_of_run_id IS NULL) OR
    (run_kind = 'RETRY' AND occurrence_key IS NULL AND retry_of_run_id IS NOT NULL)
  )
);
CREATE INDEX IF NOT EXISTS ix_schedule_runs_due ON schedule_runs (status, scheduled_for, run_id);
CREATE INDEX IF NOT EXISTS ix_schedule_runs_schedule ON schedule_runs (schedule_id, scheduled_for DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_schedule_runs_request_key
  ON schedule_runs (schedule_id, run_kind, request_key) WHERE request_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS schedule_run_attempts (
  id uuid PRIMARY KEY,
  run_id text NOT NULL REFERENCES schedule_runs(run_id),
  attempt_no integer NOT NULL CHECK (attempt_no >= 1),
  started_at timestamptz,
  finished_at timestamptz,
  result text,
  error_code text,
  runner_id text,
  UNIQUE (run_id, attempt_no)
);

-- planner findings that never become Runs (DST gaps, missed occurrences skipped/truncated)
CREATE TABLE IF NOT EXISTS schedule_planner_notes (
  id bigserial PRIMARY KEY,
  schedule_id text NOT NULL REFERENCES schedules(schedule_id),
  occurrence_key text NOT NULL,
  local_time text,
  scheduled_for timestamptz,
  reason text NOT NULL,
  detail text,
  noted_at timestamptz NOT NULL,
  UNIQUE (schedule_id, occurrence_key, reason)
);

-- deferred ownership FK: the current version must belong to the same Schedule
ALTER TABLE schedules DROP CONSTRAINT IF EXISTS fk_schedules_current_version;
ALTER TABLE schedules ADD CONSTRAINT fk_schedules_current_version
  FOREIGN KEY (current_version_id) REFERENCES schedule_versions(id) DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION agent_colab_check_schedule_version_owner() RETURNS trigger AS $$
DECLARE owner text;
BEGIN
  IF NEW.current_version_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT schedule_id INTO owner FROM schedule_versions WHERE id = NEW.current_version_id;
  IF owner IS NOT NULL AND owner <> NEW.schedule_id THEN
    RAISE EXCEPTION 'current_version_id belongs to schedule %, not %', owner, NEW.schedule_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_schedules_version_owner ON schedules;
CREATE CONSTRAINT TRIGGER trg_schedules_version_owner
  AFTER INSERT OR UPDATE OF current_version_id ON schedules
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION agent_colab_check_schedule_version_owner();

-- Runs pin their version: schedule_version_id and version_hash never change after creation
CREATE OR REPLACE FUNCTION agent_colab_forbid_run_version_change() RETURNS trigger AS $$
BEGIN
  IF NEW.schedule_version_id <> OLD.schedule_version_id OR NEW.version_hash <> OLD.version_hash
     OR NEW.run_kind <> OLD.run_kind OR NEW.occurrence_key IS DISTINCT FROM OLD.occurrence_key
     OR NEW.idempotency_key <> OLD.idempotency_key THEN
    RAISE EXCEPTION 'schedule_runs identity/version pin is immutable' USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_schedule_runs_pin ON schedule_runs;
CREATE TRIGGER trg_schedule_runs_pin
  BEFORE UPDATE ON schedule_runs
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_run_version_change();
