-- Phase 2 renderer/outbox delivery records (P2-03, P2-11): which provider post represents which
-- subject (Task card root post, thread replies), so cards are edited in place and deliveries are
-- exactly-once per dedupe key.
CREATE TABLE channel_posts (
  id bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  provider_instance_id text NOT NULL,
  external_channel_id text NOT NULL,
  subject_type text NOT NULL CHECK (subject_type IN ('task','brainstorm','approval','schedule_run','notification')),
  subject_id text NOT NULL,
  role text NOT NULL CHECK (role IN ('card','reply','link_card','ephemeral')),
  dedupe_key text NOT NULL UNIQUE,
  post_id text,
  root_post_id text,
  source_event_id text REFERENCES events(event_id),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed','dead')),
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz
);
CREATE UNIQUE INDEX channel_posts_card_idx ON channel_posts (provider_instance_id, subject_type, subject_id)
  WHERE role = 'card';
CREATE INDEX channel_posts_subject_idx ON channel_posts (subject_type, subject_id);
GRANT SELECT, INSERT, UPDATE ON channel_posts TO agent_colab_runtime, agent_colab_admin;
GRANT USAGE, SELECT ON SEQUENCE channel_posts_id_seq TO agent_colab_runtime, agent_colab_admin;
