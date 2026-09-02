-- Phase 2 message ingestion and retention (P2-15; development plan §7H, spec §9.1 Conversation/
-- Message, §11.2 crypto-shredding). Message bodies are persisted redacted; the original body only
-- as envelope ciphertext under a per-message DEK. Retention destroys the DEK and appends a chained
-- tombstone; message rows are never deleted.

CREATE TABLE conversations (
  id uuid PRIMARY KEY,
  conversation_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  channel_id uuid NOT NULL REFERENCES channels(id),
  mode text NOT NULL CHECK (mode IN ('work','brainstorm','approval','ops','custom')),
  source_thread jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_channel_idx ON conversations (channel_id);

CREATE TABLE messages (
  id uuid PRIMARY KEY,
  message_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  conversation_id text NOT NULL REFERENCES conversations(conversation_id),
  channel_id uuid NOT NULL REFERENCES channels(id),
  source text NOT NULL CHECK (source IN ('mattermost','telegram','system')),
  source_message_id text NOT NULL,
  sender_account_id uuid REFERENCES accounts(id),
  sender_label text NOT NULL,
  body_redacted text NOT NULL,
  body_ciphertext bytea,
  body_key_ref text,
  visibility text NOT NULL DEFAULT 'channel' CHECK (visibility IN ('channel','thread','private','system')),
  event_id text REFERENCES events(event_id),
  received_at timestamptz NOT NULL,
  retention_class text NOT NULL DEFAULT 'default',
  legal_hold boolean NOT NULL DEFAULT false,
  deleted_at timestamptz,
  tombstone_ref text,
  UNIQUE (source, source_message_id, conversation_id),
  CHECK ((body_ciphertext IS NULL) = (body_key_ref IS NULL))
);
CREATE INDEX messages_retention_idx ON messages (channel_id, received_at) WHERE deleted_at IS NULL;
CREATE INDEX messages_conversation_idx ON messages (conversation_id, received_at);

CREATE TABLE message_retention_policies (
  channel_id uuid PRIMARY KEY REFERENCES channels(id),
  retention_days integer NOT NULL DEFAULT 365 CHECK (retention_days > 0),
  legal_hold boolean NOT NULL DEFAULT false,
  documentation_policy text NOT NULL DEFAULT 'task_threads'
    CHECK (documentation_policy IN ('task_threads','full_channel')),
  changed_by uuid REFERENCES accounts(id),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE message_tombstones (
  id bigserial PRIMARY KEY,
  message_id text NOT NULL UNIQUE REFERENCES messages(message_id),
  channel_id uuid NOT NULL REFERENCES channels(id),
  reason text NOT NULL CHECK (reason IN ('RETENTION','HARD_DELETE')),
  key_ref text,
  deleted_at timestamptz NOT NULL,
  previous_hash text,
  content_hash text NOT NULL
);
CREATE TRIGGER trg_message_tombstones_immutable BEFORE UPDATE OR DELETE ON message_tombstones
  FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_modification();

GRANT SELECT, INSERT ON conversations, messages, message_retention_policies, message_tombstones
  TO agent_colab_runtime, agent_colab_admin;
GRANT UPDATE ON conversations, messages, message_retention_policies
  TO agent_colab_runtime, agent_colab_admin;
GRANT USAGE, SELECT ON SEQUENCE message_tombstones_id_seq TO agent_colab_runtime, agent_colab_admin;
REVOKE UPDATE, DELETE ON message_tombstones FROM agent_colab_runtime, agent_colab_admin;
