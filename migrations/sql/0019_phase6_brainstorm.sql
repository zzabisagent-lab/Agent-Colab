-- Phase 6 (P6-02/P6-09): Brainstorm sessions, turns, summaries, decisions and taskify links.
-- Sessions are an Event-sourced aggregate (bs-...); these tables are the projection the engine
-- reads for turn order and limit accounting, plus the Decision->Task provenance both ways
-- (development plan §7F, spec §8.3).
CREATE TABLE IF NOT EXISTS brainstorms (
  id uuid PRIMARY KEY,
  brainstorm_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  channel_id uuid NOT NULL REFERENCES channels(id),
  topic text NOT NULL,
  facilitator_account_id uuid NOT NULL REFERENCES accounts(id),
  status text NOT NULL DEFAULT 'OPEN'
    CHECK (status IN ('OPEN','PAUSED','CLOSED')),
  limits jsonb NOT NULL DEFAULT '{}',
  turn_no integer NOT NULL DEFAULT 0,           -- total turns recorded so far
  turn_index integer NOT NULL DEFAULT 0,        -- round-robin cursor over agent participants
  last_contributor_account_id uuid REFERENCES accounts(id),
  consecutive_turns integer NOT NULL DEFAULT 0, -- consecutive turns by last_contributor
  paused_reason text,
  started_at timestamptz NOT NULL,
  closed_at timestamptz,
  last_event_id text,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_brainstorms_workspace ON brainstorms (workspace_id, status);

CREATE TABLE IF NOT EXISTS brainstorm_participants (
  brainstorm_id text NOT NULL REFERENCES brainstorms(brainstorm_id) ON DELETE CASCADE,
  account_id uuid NOT NULL REFERENCES accounts(id),
  role text NOT NULL CHECK (role IN ('human','agent')),
  agent_id text,                                -- registry id for agent participants
  seat integer NOT NULL,                        -- deterministic round-robin position
  turns_taken integer NOT NULL DEFAULT 0,
  joined_at timestamptz NOT NULL,
  PRIMARY KEY (brainstorm_id, account_id),
  UNIQUE (brainstorm_id, seat)
);

CREATE TABLE IF NOT EXISTS brainstorm_turns (
  turn_id text PRIMARY KEY,
  brainstorm_id text NOT NULL REFERENCES brainstorms(brainstorm_id) ON DELETE CASCADE,
  seq integer NOT NULL,
  account_id uuid NOT NULL REFERENCES accounts(id),
  contribution_type text NOT NULL
    CHECK (contribution_type IN ('IDEA','CHALLENGE','QUESTION','GUIDANCE')),
  body text NOT NULL,
  body_ref text,                                -- artifact ref when the body is stored separately
  work_item_id text,
  event_id text NOT NULL REFERENCES events(event_id),
  created_at timestamptz NOT NULL,
  UNIQUE (brainstorm_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_brainstorm_turns_account
  ON brainstorm_turns (brainstorm_id, account_id);

CREATE TABLE IF NOT EXISTS brainstorm_summaries (
  summary_id text PRIMARY KEY,
  brainstorm_id text NOT NULL REFERENCES brainstorms(brainstorm_id) ON DELETE CASCADE,
  author_account_id uuid NOT NULL REFERENCES accounts(id),
  status text NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','APPROVED')),
  body text NOT NULL,
  artifact_id text,
  posted_at timestamptz,                        -- set only after facilitator approval
  approved_by uuid REFERENCES accounts(id),
  approved_at timestamptz,
  event_id text REFERENCES events(event_id),
  created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_brainstorm_summaries_session
  ON brainstorm_summaries (brainstorm_id, created_at DESC);

CREATE TABLE IF NOT EXISTS brainstorm_decisions (
  decision_id text PRIMARY KEY,
  brainstorm_id text NOT NULL REFERENCES brainstorms(brainstorm_id) ON DELETE CASCADE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  statement text NOT NULL,
  rationale text NOT NULL,
  source_event_ids jsonb NOT NULL DEFAULT '[]',
  action_items jsonb NOT NULL DEFAULT '[]',     -- [{statement, criteria:[...]}]
  vote jsonb,                                   -- {up: n, down: n, voters: [...]}
  status text NOT NULL DEFAULT 'recorded'
    CHECK (status IN ('recorded','taskified','superseded')),
  decided_by uuid NOT NULL REFERENCES accounts(id),
  decided_at timestamptz NOT NULL,
  event_id text NOT NULL REFERENCES events(event_id)
);
CREATE INDEX IF NOT EXISTS ix_brainstorm_decisions_session
  ON brainstorm_decisions (brainstorm_id, decided_at);

CREATE TABLE IF NOT EXISTS decision_tasks (
  decision_id text NOT NULL REFERENCES brainstorm_decisions(decision_id) ON DELETE CASCADE,
  task_id text NOT NULL,
  action_item text NOT NULL,
  item_index integer NOT NULL,
  created_at timestamptz NOT NULL,
  PRIMARY KEY (decision_id, item_index),
  UNIQUE (task_id)
);
CREATE INDEX IF NOT EXISTS ix_decision_tasks_task ON decision_tasks (task_id);

-- Brainstorm summaries are channel posts of their own role once the facilitator approves them.
-- The role list is widened additively (0005 → 0009 → 0017 → here).
ALTER TABLE channel_posts DROP CONSTRAINT IF EXISTS channel_posts_role_check;
ALTER TABLE channel_posts ADD CONSTRAINT channel_posts_role_check
  CHECK (role IN ('card','reply','link_card','ephemeral','work_message','notice','summary'));
