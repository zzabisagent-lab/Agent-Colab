-- Phase 3 (P3-11/P3-12): work delivery transports.
-- Durable one-time nonces for signed inbound webhook callbacks (development plan §7.5, §7B.2:
-- 5-minute timestamp window, nonces kept 24 h). Rows older than the retention are pruned by the
-- verifier on insert.
CREATE TABLE IF NOT EXISTS webhook_nonces (
  nonce text PRIMARY KEY,
  agent_id text NOT NULL,
  seen_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_webhook_nonces_seen_at ON webhook_nonces (seen_at);

-- Structured work messages (P3-12) are channel posts of subject_type 'work_item' / role
-- 'work_message'; the Phase 2 check constraints are widened accordingly.
ALTER TABLE channel_posts DROP CONSTRAINT IF EXISTS channel_posts_subject_type_check;
ALTER TABLE channel_posts ADD CONSTRAINT channel_posts_subject_type_check
  CHECK (subject_type IN ('task','brainstorm','approval','schedule_run','notification','work_item'));
ALTER TABLE channel_posts DROP CONSTRAINT IF EXISTS channel_posts_role_check;
ALTER TABLE channel_posts ADD CONSTRAINT channel_posts_role_check
  CHECK (role IN ('card','reply','link_card','ephemeral','work_message'));
