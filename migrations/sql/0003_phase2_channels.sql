-- Phase 2 channel/provider additions (P2-01, P2-10). Additive only.

ALTER TABLE provider_instances ADD COLUMN bot_user_id text;
ALTER TABLE provider_instances ADD COLUMN identity_display text NOT NULL DEFAULT 'prefix'
  CHECK (identity_display IN ('override','prefix'));
ALTER TABLE provider_instances ADD COLUMN config jsonb NOT NULL DEFAULT '{}';

ALTER TABLE channels ADD COLUMN template_id text;
ALTER TABLE channels ADD COLUMN retention_days integer NOT NULL DEFAULT 365 CHECK (retention_days >= 1);
ALTER TABLE channels ADD COLUMN legal_hold boolean NOT NULL DEFAULT false;
ALTER TABLE channels ADD COLUMN archived_at timestamptz;
ALTER TABLE channels ADD COLUMN deleted_at timestamptz;

-- channel templates: the 4 defaults are protected; user templates are versioned rows
CREATE TABLE channel_templates (
  template_id text NOT NULL,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  name text NOT NULL,
  channel_type text NOT NULL CHECK (channel_type IN ('work','brainstorm','approval','ops','custom')),
  definition jsonb NOT NULL,
  protected boolean NOT NULL DEFAULT false,
  version integer NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted')),
  created_by uuid REFERENCES accounts(id),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, template_id)
);

-- root post <-> Task/Brainstorm binding (development plan §7A.2 thread context, §7A.3 card)
CREATE TABLE thread_bindings (
  provider_instance_id uuid NOT NULL REFERENCES provider_instances(id),
  root_post_id text NOT NULL,
  external_channel_id text NOT NULL,
  subject_type text NOT NULL CHECK (subject_type IN ('task','brainstorm')),
  subject_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider_instance_id, root_post_id),
  UNIQUE (provider_instance_id, subject_type, subject_id)
);

-- slash-command verification tokens are stored hashed only (spec §15.7)
CREATE TABLE provider_command_tokens (
  provider_instance_id uuid NOT NULL REFERENCES provider_instances(id),
  trigger text NOT NULL,
  token_hash text NOT NULL,
  command_ref text,
  created_at timestamptz NOT NULL DEFAULT now(),
  rotated_at timestamptz,
  PRIMARY KEY (provider_instance_id, trigger)
);

-- one-time nonces for provider callbacks (trigger ids, action callbacks); expired rows are purged
CREATE TABLE provider_nonces (
  provider_instance_id uuid NOT NULL REFERENCES provider_instances(id),
  nonce text NOT NULL,
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (provider_instance_id, nonce)
);
CREATE INDEX provider_nonces_expiry_idx ON provider_nonces (expires_at);

GRANT SELECT, INSERT, UPDATE ON channel_templates, thread_bindings, provider_command_tokens, provider_nonces
  TO agent_colab_runtime, agent_colab_admin;
GRANT DELETE ON provider_nonces TO agent_colab_runtime, agent_colab_admin;
