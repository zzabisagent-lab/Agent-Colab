-- Phase 2 (P2-05/P2-06): per-channel Telegram Bridges, message mappings, dead letters.
-- Spec §10, development plan §6.5: one Telegram target (chat + optional topic) is bound to one
-- Mattermost channel by default (partial unique index); explicit administrator exceptions are
-- recorded on the row. Mappings are unique per (bridge, source platform, source message id).

CREATE TABLE telegram_bridges (
  id uuid PRIMARY KEY,
  bridge_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  channel_id uuid NOT NULL REFERENCES channels(id),
  provider_instance_id text NOT NULL,
  telegram_chat_id text NOT NULL,
  telegram_thread_id text,
  thread_mode text NOT NULL DEFAULT 'topic_per_root'
    CHECK (thread_mode IN ('topic_per_root','general','fixed_topic')),
  direction text NOT NULL
    CHECK (direction IN ('mattermost_to_telegram','telegram_to_mattermost','bidirectional')),
  content_policy jsonb NOT NULL DEFAULT '{"text": true, "attachment": true, "system_event": false, "approval_notice": true, "mention": true}',
  redaction_policy jsonb NOT NULL DEFAULT '{"secret_patterns": true, "private_messages": true, "restricted_artifacts": true}',
  identity_display jsonb NOT NULL DEFAULT '{"show_sender": true, "show_source": true}',
  rate_limit jsonb NOT NULL DEFAULT '{"per_minute": 20, "max_attachment_bytes": 20971520}',
  allow_commands boolean NOT NULL DEFAULT false,
  admin_exception boolean NOT NULL DEFAULT false,
  admin_exception_reason text,
  status text NOT NULL DEFAULT 'enabled' CHECK (status IN ('enabled','disabled')),
  secret_ref text,
  created_by uuid NOT NULL REFERENCES accounts(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((thread_mode = 'fixed_topic') = (telegram_thread_id IS NOT NULL))
);
CREATE UNIQUE INDEX telegram_bridges_one_channel_per_target_idx
  ON telegram_bridges (provider_instance_id, telegram_chat_id, COALESCE(telegram_thread_id, ''))
  WHERE admin_exception = false;
CREATE INDEX telegram_bridges_channel_idx ON telegram_bridges (channel_id) WHERE status = 'enabled';
CREATE INDEX telegram_bridges_target_idx ON telegram_bridges (provider_instance_id, telegram_chat_id)
  WHERE status = 'enabled';

CREATE TABLE message_mappings (
  id bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  bridge_id text NOT NULL REFERENCES telegram_bridges(bridge_id),
  source_platform text NOT NULL CHECK (source_platform IN ('mattermost','telegram')),
  source_message_id text NOT NULL,
  destination_platform text NOT NULL CHECK (destination_platform IN ('mattermost','telegram')),
  destination_message_id text,
  mm_channel_id text NOT NULL,
  mm_post_id text,
  mm_root_post_id text,
  tg_chat_id text NOT NULL,
  tg_message_id bigint,
  tg_thread_id bigint,
  tg_reply_to_message_id bigint,
  origin_platform text NOT NULL CHECK (origin_platform IN ('mattermost','telegram')),
  origin_message_id text NOT NULL,
  origin_marker text NOT NULL,
  hop_count integer NOT NULL CHECK (hop_count >= 0 AND hop_count <= 1),
  redaction_status text NOT NULL DEFAULT 'clean',
  delivery_status text NOT NULL DEFAULT 'pending'
    CHECK (delivery_status IN ('pending','sent','failed','dead')),
  dedupe_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  UNIQUE (bridge_id, source_platform, source_message_id)
);
CREATE INDEX message_mappings_bridge_idx ON message_mappings (bridge_id, created_at);
CREATE INDEX message_mappings_mm_post_idx ON message_mappings (bridge_id, mm_post_id);
CREATE INDEX message_mappings_tg_msg_idx ON message_mappings (bridge_id, tg_message_id);

CREATE TABLE bridge_dead_letters (
  id bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  bridge_id text NOT NULL REFERENCES telegram_bridges(bridge_id),
  dedupe_key text NOT NULL UNIQUE,
  outbox_id text NOT NULL,
  reason text NOT NULL,
  payload jsonb NOT NULL,
  event_id text REFERENCES events(event_id),
  created_at timestamptz NOT NULL DEFAULT now(),
  replayed_at timestamptz
);

GRANT SELECT, INSERT, UPDATE ON telegram_bridges, message_mappings, bridge_dead_letters
  TO agent_colab_runtime, agent_colab_admin;
GRANT USAGE, SELECT ON SEQUENCE message_mappings_id_seq, bridge_dead_letters_id_seq
  TO agent_colab_runtime, agent_colab_admin;
