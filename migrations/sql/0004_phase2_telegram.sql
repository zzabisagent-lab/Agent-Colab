-- Phase 2 (P2-04): replay protection for Telegram webhook updates. One row per received update
-- per provider instance; a duplicate update_id is a replay and produces no side effect.
CREATE TABLE telegram_update_receipts (
  provider_instance_id text NOT NULL,
  update_id bigint NOT NULL,
  chat_id text,
  message_id bigint,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider_instance_id, update_id)
);
CREATE INDEX telegram_update_receipts_received_idx ON telegram_update_receipts (received_at);
GRANT SELECT, INSERT, DELETE ON telegram_update_receipts TO agent_colab_runtime, agent_colab_admin;
