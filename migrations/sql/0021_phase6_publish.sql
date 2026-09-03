-- Phase 6 (P6-03/P6-06/P6-07): Artifact quarantine and scan provenance, publish destinations,
-- published document versions, publish reviews and publish attempts
-- (development plan §10.3 Publisher Contract, spec §9.1/§15).
-- No secret values are stored here: destination credentials are Secret Broker references only.

-- ---------------------------------------------------------------- artifact safety (P6-03)
CREATE TABLE IF NOT EXISTS artifact_scan_results (
  id bigserial PRIMARY KEY,
  artifact_id text NOT NULL REFERENCES artifacts(artifact_id),
  scanner text NOT NULL,
  verdict text NOT NULL CHECK (verdict IN ('clean', 'infected', 'error')),
  reason_code text,
  detail text,
  scanned_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_artifact_scan_results_artifact
  ON artifact_scan_results (artifact_id, scanned_at DESC);

CREATE TABLE IF NOT EXISTS artifact_quarantine (
  artifact_id text PRIMARY KEY REFERENCES artifacts(artifact_id),
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  reason_code text NOT NULL,
  detail text,
  scanned_at timestamptz NOT NULL DEFAULT now(),
  released_by uuid REFERENCES accounts(id),
  released_at timestamptz,
  release_reason text
);
CREATE INDEX IF NOT EXISTS ix_artifact_quarantine_open
  ON artifact_quarantine (workspace_id) WHERE released_at IS NULL;

-- ---------------------------------------------------------------- publishing (P6-06/P6-07)
CREATE TABLE IF NOT EXISTS publish_destinations (
  id uuid PRIMARY KEY,
  destination_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  kind text NOT NULL CHECK (kind IN ('filesystem', 'git', 'bookstack', 'wikijs')),
  display_name text NOT NULL,
  -- non-secret configuration only; credentials are Secret Broker references (credential_ref)
  config jsonb NOT NULL DEFAULT '{}',
  credential_ref text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_by uuid NOT NULL REFERENCES accounts(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS publish_reviews (
  id uuid PRIMARY KEY,
  review_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  document_id text NOT NULL,
  version integer NOT NULL CHECK (version >= 1),
  reviewer_account_id uuid NOT NULL REFERENCES accounts(id),
  decision text NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
  reason text NOT NULL,
  event_id text REFERENCES events(event_id),
  decided_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_publish_reviews_document
  ON publish_reviews (document_id, version, decided_at DESC);

CREATE TABLE IF NOT EXISTS published_documents (
  id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  document_id text NOT NULL,
  version integer NOT NULL CHECK (version >= 1),
  destination_id text NOT NULL REFERENCES publish_destinations(destination_id),
  external_ref text NOT NULL,
  external_version text,
  checksum text NOT NULL,
  state text NOT NULL DEFAULT 'published' CHECK (state IN ('published', 'archived')),
  correction_of_version integer,
  correction_reason text,
  published_by uuid NOT NULL REFERENCES accounts(id),
  published_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  event_id text REFERENCES events(event_id),
  -- exactly once per (document, version, destination): a retry after an outage updates nothing
  UNIQUE (document_id, version, destination_id)
);
CREATE INDEX IF NOT EXISTS ix_published_documents_destination
  ON published_documents (destination_id, published_at DESC);

CREATE TABLE IF NOT EXISTS publish_attempts (
  id bigserial PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  document_id text NOT NULL,
  version integer NOT NULL CHECK (version >= 1),
  destination_id text NOT NULL REFERENCES publish_destinations(destination_id),
  attempt_no integer NOT NULL CHECK (attempt_no >= 1),
  ok boolean NOT NULL,
  error_code text,
  detail text,
  attempted_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, version, destination_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS ix_publish_attempts_document
  ON publish_attempts (document_id, version, destination_id, attempt_no);
