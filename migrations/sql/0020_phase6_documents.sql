-- Phase 6 (P6-04/P6-05/P6-08/P6-10): document freezes, provenance, redaction counts, narrative
-- drafts and generation failures (development plan §10.1 pipeline, §10.4 layers).
-- No table here ever stores a secret or canary value: redaction rows carry counts and a salted
-- hash of the matched text only.

CREATE TABLE IF NOT EXISTS document_freezes (
  id bigserial PRIMARY KEY,
  freeze_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  document_id text,                       -- null while a generation attempt is still failing
  subject_type text NOT NULL CHECK (subject_type IN
    ('task','brainstorm','schedule_run','schedule_period')),
  subject_id text NOT NULL,
  frozen_at timestamptz NOT NULL,
  up_to_recorded_seq bigint NOT NULL DEFAULT 0,
  source_manifest jsonb NOT NULL,         -- the exact source ids the version was built from
  manifest_hash text NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS ix_document_freezes_subject
  ON document_freezes (subject_type, subject_id, frozen_at DESC);
CREATE INDEX IF NOT EXISTS ix_document_freezes_document ON document_freezes (document_id);

CREATE TABLE IF NOT EXISTS document_provenance (
  id bigserial PRIMARY KEY,
  document_id text NOT NULL REFERENCES documents(document_id),
  version integer NOT NULL,
  ref_type text NOT NULL CHECK (ref_type IN ('evt','art','dec','vr','run','msg')),
  ref_id text NOT NULL,
  checksum text NOT NULL,                 -- content hash of the source as it was at freeze time
  resolved boolean NOT NULL DEFAULT true,
  checked_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, version, ref_type, ref_id)
);
CREATE INDEX IF NOT EXISTS ix_document_provenance_ref ON document_provenance (ref_type, ref_id);

CREATE TABLE IF NOT EXISTS document_redactions (
  id bigserial PRIMARY KEY,
  document_id text NOT NULL REFERENCES documents(document_id),
  version integer NOT NULL,
  rule text NOT NULL,                     -- canary | email | phone | card | token
  count integer NOT NULL CHECK (count > 0),
  sample_hash text NOT NULL CHECK (sample_hash ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, version, rule)
);

CREATE TABLE IF NOT EXISTS document_narratives (
  id bigserial PRIMARY KEY,
  document_id text NOT NULL REFERENCES documents(document_id),
  version integer NOT NULL,
  author_account_id uuid REFERENCES accounts(id),
  status text NOT NULL CHECK (status IN ('ACCEPTED','REJECTED','DECLINED','UNAVAILABLE')),
  body text NOT NULL DEFAULT '',
  citations jsonb NOT NULL DEFAULT '[]',
  accepted boolean NOT NULL DEFAULT false,
  reason_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, version)
);

CREATE TABLE IF NOT EXISTS document_generation_failures (
  id bigserial PRIMARY KEY,
  workspace_id uuid REFERENCES workspaces(id),
  -- deliberately unconstrained: a failure ledger must be able to record an unexpected subject
  subject_type text NOT NULL,
  subject_id text NOT NULL,
  reason_code text NOT NULL,
  detail text NOT NULL DEFAULT '',
  at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_document_generation_failures_subject
  ON document_generation_failures (subject_type, subject_id, at DESC);
