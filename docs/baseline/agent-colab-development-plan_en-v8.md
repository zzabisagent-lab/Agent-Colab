# Agent-Colab Development Plan v8 (EN)

> Document version: 8.0  
> Product baseline: [[agent-colab-project-spec_en-v8]]  
> Verification baseline: [[agent-colab-validation-plan_en-v8]]  
> Execution principle: implement phase by phase, then a **different Agent verifies independently**; no human gates between phases.  
> Supersedes: this document replaces development plan versions v1–v7. It is the English canonical text; the Korean v7 is the last Korean edition.

## 1. Purpose

This document converts Agent-Colab v8 into an implementable architecture, work packages, interfaces, completion criteria, and per-phase Exit Gates. Test IDs, verification Agent assignment, and verdict formats are governed by [[agent-colab-validation-plan_en-v8]].

## 2. Current State and Direction

### 2.1 Confirmed Current State

- Only the product specification and earlier plans exist; no actual repository or application code has been confirmed.
- Earlier concepts pre-assigned roles to specific Agents and machines.
- Actual endpoints, credentials, and versions of Mattermost, Telegram, Hub, PostgreSQL, and NAS are undetermined.
- Separate review documents and policy originals assumed by earlier plans are not present in the vault.

### 2.2 v8 Transition

- Remove all fixed Agent/machine roles and generalize into Agent Registry + Adapter + Role + Capability.
- Implement Mattermost as the first channel provider and connect Telegram as per-channel Bridges.
- Include the Web Admin Console, Setup Wizard, Secret Broker, and Documentation Service in the Colab Server.
- Separate implementation results into `IMPLEMENTED` and `VERIFIED` so that the same Agent cannot confirm both states.
- Build the Event Store and policy enforcement first; connect external Agents and UI afterwards.
- Manage recurring work with Schedules and durable ScheduleRuns; never allow arbitrary shell cron.
- Implement the Approval/Artifact/Document draft Core required by Scheduled Work in Phase 1; Phase 6 extends collaboration UX, finalization, and publishing.
- The product surface (Mattermost commands and cards, Agent work delivery, approvers, Task acceptance criteria and Verifier assignment, Brainstorm progression, document narrative, cost units) is fixed by the designs in §6.9, §7A–§7H, §10.4 and by Phase 0 contracts/spikes, never designed ad hoc by an implementing Agent.
- Every work package has prerequisites, an initial size, and mapped Test IDs (§12.1); progress inside a phase is judged per package.
- **Autonomous execution (v8):** the pipeline runs from Phase 0 to Phase 7 and on to deployment readiness without human phase gates. Phase transitions are triggered solely by an automated independent Verifier PASS (§12.2).

## 3. Target Architecture

```text
                        ┌─ Telegram Chat/Topic
                        │
Human/Agent ─ Mattermost Channel ─ Channel Gateway
                                      │
Agent Adapter ─ MCP/REST/Webhook ─────┤
Admin Browser ─ HTTPS ────────────────┤
                                      ▼
┌────────────────────── Agent-Colab Server ──────────────────────┐
│ API Gateway / AuthN / Request Validation                       │
│ Application Services                                           │
│ ├─ Workspace/Account/Agent/Role/Capability                     │
│ ├─ Channel/Bridge/Conversation/Task/Event                      │
│ ├─ Brainstorm/Decision/Approval/Artifact/Verification          │
│ ├─ Setup/Settings/Operations/Schedules                         │
│ ├─ Secret Broker                                               │
│ └─ Documentation/Publisher                                     │
│ Policy Engine ─ Event Store ─ Projections ─ Outbox/Scheduler   │
│ Web Admin Console ─ SSE                                        │
└───────┬──────────────┬──────────────┬──────────────┬───────────┘
        │              │              │              │
  PostgreSQL     Secret Provider  Artifact/NAS  Document Publisher
```

### 3.1 Module Boundaries

| Module | Responsibility | Forbidden |
|---|---|---|
| Identity | Human/Agent/service authentication, session/token | trusting the actor in the body |
| External Identity Link | verified link between Mattermost/Telegram users and Accounts | side effects by unlinked external users |
| Policy | RBAC, capability, scope, deny precedence | enforcing permissions via prompts |
| Event Store | append/idempotency/sequence/causality | UPDATE/DELETE |
| Projection | current state and rebuild | manual editing of authoritative data |
| Channel Gateway | Mattermost input/Renderer | interpreting ordinary chat as implicit state |
| Telegram Bridge | per-channel mapping/dedupe/retry | indiscriminate workspace-wide relay |
| Agent Runtime | registry, adapter, conformance, heartbeat, multi-Agent task graph/routing | product-specific core branching |
| Setup Service | bootstrap/preflight/settings | unattended re-run after completion |
| Secret Broker | reference/grant/lease/audit | plaintext Event/log storage |
| Documentation | source collection, draft, review, publish | automatic publishing without provenance |
| Verification | independence check, criteria/evidence/result | implementer self-pass |
| Schedule Service | cron/timezone, Run creation, concurrency/missed/retry policy | direct shell command execution |
| Durable Scheduler | due claim, DB lease, recovery, lag metric | memory-only timer |
| Command Router | `/colab` grammar parsing, thread context, ephemeral responses | guessing commands from free text |
| Work Delivery | work item inbox/push, ack/accept timeout, exactly-once result collection | using chat messages as the only delivery path |
| Usage/Budget | usage records, cost_units conversion, limit reservation/settlement | unit-less cost comparison |
| Notification | rule-based notification generation/delivery/dedupe | using notifications as state authority |
| Brainstorm Engine | turn distribution, limits, summary/decision/taskify | automatic decisions without a facilitator |

## 4. Repository Structure

```text
agent-colab/
├── README.md
├── AGENTS.md
├── pyproject.toml
├── uv.lock
├── package.json
├── pnpm-lock.yaml
├── compose.yaml
├── .env.example
├── policy/
│   ├── default-roles.yaml
│   ├── capabilities.yaml
│   ├── permissions.yaml
│   ├── pricing.yaml
│   ├── risk-rules.yaml
│   └── verification-rules.yaml
├── schemas/
│   ├── events/
│   ├── api/
│   ├── adapters/
│   └── documents/
├── server/
│   ├── main.py
│   ├── config.py
│   ├── api/v1/
│   ├── application/
│   ├── domain/
│   ├── db/
│   ├── identity/
│   │   ├── external_links.py
│   │   └── credential_snapshots.py
│   ├── policy/
│   ├── events/
│   ├── projections/
│   ├── agents/
│   │   ├── registry.py
│   │   ├── contracts.py
│   │   ├── orchestration.py
│   │   └── adapters/{mcp,webhook,mattermost}.py
│   ├── channels/{mattermost,telegram,outbox,commands,renderer,actions}.py
│   ├── work/{inbox,push,receipts}.py
│   ├── usage/{records,pricing,budget}.py
│   ├── notifications/{rules,dispatch}.py
│   ├── brainstorm/{engine,summarize}.py
│   ├── setup/
│   ├── secrets/{broker,providers,injection}.py
│   ├── documents/{builder,publishers,templates}.py
│   ├── verification/
│   ├── schedules/{models,parser,planner,runner,recovery,attempts}.py
│   ├── operations/
│   └── observability/
├── web-admin/
│   ├── src/app/
│   ├── src/features/
│   ├── src/api/
│   └── tests/
├── sidecar/
├── i18n/{ko,en}/
├── migrations/versions/
├── deploy/{dev,staging,production,backup}/
├── docs/{adr,architecture,protocol,security,operations,restore}/
├── monitoring/{prometheus,alerts}/
└── tests/
    ├── unit/
    ├── integration/
    ├── contract/
    ├── conformance/
    ├── security/
    ├── e2e/
    └── recovery/
```

## 5. Technology Stack

| Area | v8 choice |
|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2 |
| DB | PostgreSQL 16, SQLAlchemy 2, Alembic |
| Agent protocol | MCP + REST/Webhook adapter contract |
| Streaming | SSE; Mattermost/Telegram provider clients |
| Scheduling | cron parser + PostgreSQL durable Run/lease; no OS cron dependency |
| Admin Web | React + TypeScript + Vite, accessible component library |
| Auth | secure session; Owner/Administrator TOTP MFA mandatory; Member policy MFA; OIDC adapter (optional); service token; optional mTLS |
| Secret | provider interface; encrypted local provider + external provider adapter |
| Document | Markdown + manifest; filesystem/NAS + Git publisher |
| Tests | pytest, Playwright, contract/conformance fixtures, disposable Postgres |
| Quality | Ruff, mypy, Bandit, pip-audit, ESLint, TypeScript, dependency/secret scan |
| Delivery | OCI images, Docker Compose, GitHub Actions or equivalent CI |
| Observability | Prometheus metrics, JSON logs, alert rules |

Exact package/container versions are locked in Phase 0 after checking official stable releases.

## 6. DB Schema Draft

### 6.1 Authority and Projections

- Authority: `events`, approval consumption ledger, immutable ScheduleVersion, policy/config version history, verification/audit revisions.
- Projections: Task, Agent presence, Approval display state, Bridge delivery, setup dashboard status. Projections are never the command authority for permissions, use counts, or duplicate prevention.
- Secret values are never stored in the DB; only `secret_metadata.provider_ref` is stored. Sensitive Event content is separated into envelope-encrypted objects/ciphertext.

### 6.2 Main Tables

| Area | Tables |
|---|---|
| Organization/accounts | `workspaces`, `accounts`, `account_sessions`, `service_credentials`, `provider_instances`, `external_identity_links`, `identity_link_challenges` |
| Agent/permissions | `agents`, `roles`, `role_versions`, `principal_role_assignments`, `capabilities` |
| Channels | `channels`, `channel_members`, `telegram_bridges`, `message_mappings`, `delivery_outbox`, `message_retention_policies` |
| Work | `tasks_projection`, `task_edges`, `task_assignments`, `task_acceptance_criteria`, `work_items`, `work_item_receipts`, `events`, `conversations`, `messages`, `brainstorms`, `brainstorm_turns`, `decisions` |
| Approval/deliverables | `approval_grants`, `approval_consumptions`, `approvals_projection`, `artifacts`, `artifact_links`, `documents`, `document_versions`, `document_publications` |
| Secret | `secret_metadata`, `secret_grants`, `secret_access_audit` |
| Verification | `verification_runs`, `verification_revisions`, `verification_evidence`, `verification_findings`, `credential_identity_snapshots` |
| Operations | `system_settings`, `setting_versions`, `setup_state`, `audit_events`, `audit_hash_anchors`, `projection_checkpoints`, `schedules`, `schedule_versions`, `schedule_runs`, `schedule_run_attempts`, `scheduler_leases`, `key_tombstones` |
| Metering/notification | `usage_records`, `pricing_versions`, `budget_reservations`, `notification_rules`, `notifications` |

### 6.3 Event DDL Core

```sql
CREATE TABLE events (
  id uuid PRIMARY KEY,
  event_id text NOT NULL UNIQUE,
  schema_version smallint NOT NULL,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  aggregate_type text NOT NULL,
  aggregate_id text NOT NULL,
  aggregate_seq bigint NOT NULL,
  channel_id uuid,
  task_id text,
  type text NOT NULL,
  actor_account_id uuid NOT NULL REFERENCES accounts(id),
  caused_by text REFERENCES events(event_id),
  correlation_id text NOT NULL,
  idempotency_scope text NOT NULL,
  idempotency_key text NOT NULL,
  policy_version text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  sensitive_payload_ciphertext bytea,
  sensitive_payload_key_ref text,
  previous_hash bytea,
  content_hash bytea NOT NULL,
  occurred_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, aggregate_type, aggregate_id, aggregate_seq),
  UNIQUE (workspace_id, actor_account_id, idempotency_scope, idempotency_key),
  CHECK (aggregate_seq > 0),
  CHECK ((sensitive_payload_ciphertext IS NULL) = (sensitive_payload_key_ref IS NULL))
);
```

UPDATE/DELETE is revoked from runtime and admin application roles and protected by triggers. `payload` allows non-sensitive data only. `content_hash` is SHA-256 over RFC 8785 canonical JSON, immutable metadata, ciphertext, and `previous_hash`. Aggregate appends are serialized by a `(workspace, aggregate_type, aggregate_id)` advisory lock or expected-sequence compare-and-swap. The application and a deferred integrity job check that `caused_by`, channel, task, and actor belong to the same workspace. Hard delete destroys the per-target DEK in the external key provider and never modifies Event rows, ciphertext, or hashes.

### 6.4 Verification Independence Constraints

```sql
CHECK (implementer_account_id <> verifier_account_id),
CHECK (implementer_agent_id IS NULL OR verifier_agent_id IS NULL OR verifier_agent_id <> implementer_agent_id)
```

Additional application rules:

- The same service credential, the same immutable agent identity, and alias accounts of the implementer cannot be selected as verifier.
- Phase 4 security verification and Phase 7 release verification require a specialist verifier class. In v8 no human approval is part of a phase gate.
- Verification results are corrected only by new revisions.
- The same Verifier may cover several phases and specialties, but must differ from the implementer of each scope in Account, Agent, service credential, and alias.
- When a VerificationRun is created, implementer/verifier Account IDs, Agent IDs, credential fingerprints, owner/alias graph version, and effective policy hash are stored as an immutable snapshot.
- `verification_revisions` and `audit_events` forbid UPDATE/DELETE and chain the previous hash. A daily hash anchor is recorded in separate storage or a signed Git record.

### 6.5 Bridge Constraints

- `UNIQUE(bridge_id, source_platform, source_message_id)`
- Duplicate Telegram target connections are rejected by the default unique policy; explicit exceptions are recorded.
- Message mappings store origin, hop_count, redaction status, and delivery status.
- `external_identity_links` is unique on `(provider_instance_id, external_user_id)` and points to exactly one active Account. Link creation is verified by administrator approval or a signed challenge; suspend/revoke blocks command permissions immediately.
- Raw external-input canaries are treated only as isolated test evidence. Only redacted values may exist in Agent-Colab's persisted normalized messages, Events, logs, and Bridge destinations.

### 6.6 Schedule Schema and Constraints

```sql
CREATE TABLE schedules (
  id uuid PRIMARY KEY,
  schedule_id text NOT NULL UNIQUE,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  name text NOT NULL,
  status text NOT NULL DEFAULT 'DRAFT'
    CHECK (status IN ('DRAFT','ENABLED','PAUSED','DISABLED')),
  current_version_id uuid,
  next_run_at timestamptz,
  created_by uuid NOT NULL REFERENCES accounts(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE schedule_versions (
  id uuid PRIMARY KEY,
  schedule_id text NOT NULL REFERENCES schedules(schedule_id),
  version integer NOT NULL,
  channel_id uuid NOT NULL REFERENCES channels(id),
  cron_expression text NOT NULL,
  timezone text NOT NULL,
  execution_principal_id uuid NOT NULL REFERENCES accounts(id),
  agent_selection jsonb NOT NULL,
  action_template jsonb NOT NULL,
  concurrency_policy text NOT NULL,
  missed_run_policy text NOT NULL,
  backfill_limit integer NOT NULL,
  backfill_window_seconds integer NOT NULL,
  max_duration_seconds integer NOT NULL,
  retry_policy jsonb NOT NULL,
  budget_policy jsonb NOT NULL,
  documentation_policy jsonb NOT NULL,
  starts_at timestamptz,
  ends_at timestamptz,
  snapshot_hash bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(schedule_id, version)
);

CREATE TABLE schedule_runs (
  id uuid PRIMARY KEY,
  run_id text NOT NULL UNIQUE,
  schedule_id text NOT NULL REFERENCES schedules(schedule_id),
  schedule_version_id uuid NOT NULL REFERENCES schedule_versions(id),
  run_kind text NOT NULL CHECK (run_kind IN ('SCHEDULED','MANUAL','RETRY')),
  occurrence_key text,
  scheduled_for timestamptz NOT NULL,
  local_scheduled_for timestamp,
  retry_of_run_id text REFERENCES schedule_runs(run_id),
  status text NOT NULL CHECK (status IN (
    'PENDING','DUE','CLAIMED','TASK_CREATED','RUNNING','VERIFYING',
    'SUCCEEDED','FAILED','SKIPPED','TIMED_OUT','CANCEL_REQUESTED','CANCELLED'
  )),
  attempt_count integer NOT NULL DEFAULT 0,
  task_id text,
  idempotency_key text NOT NULL,
  claimed_by text,
  lease_expires_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  result_event_id text,
  error_code text,
  cancel_requested_at timestamptz,
  cancelled_at timestamptz,
  UNIQUE(schedule_id, idempotency_key),
  UNIQUE(schedule_id, occurrence_key)
);

CREATE TABLE schedule_run_attempts (
  id uuid PRIMARY KEY,
  run_id text NOT NULL REFERENCES schedule_runs(run_id),
  attempt_no integer NOT NULL,
  started_at timestamptz,
  finished_at timestamptz,
  result text,
  error_code text,
  UNIQUE(run_id, attempt_no)
);
```

- cron accepts numeric five-field expressions only. `*`, lists, ranges, steps, field ranges, and DOM/DOW OR semantics implement spec §8.6 exactly; names, seconds, extended tokens, and DOW 7 are rejected. The default minimum interval is 5 minutes with a configurable floor of 1 minute.
- The timezone must pass IANA identifier validation.
- `action_template` allows only Agent-Colab actions under a versioned JSON Schema and forbids shell strings.
- `concurrency_policy` allows only `FORBID|ALLOW|REPLACE`; `missed_run_policy` only `SKIP|RUN_ONCE|BACKFILL_LIMITED`. Backfill, max duration, and attempt counts have DB CHECKs of ≥ 0 plus upper-bound policies.
- Due Run claims use `FOR UPDATE SKIP LOCKED` and scheduler leases.
- Scheduled Runs are materialized exactly once per `(schedule_id, occurrence_key)`, including DST folds. Manual/retry Runs have `occurrence_key = NULL` and a separate deterministic idempotency key.
- DB CHECKs require an occurrence key for `SCHEDULED` Runs, NULL for `MANUAL|RETRY`, and `retry_of_run_id` only for `RETRY`. `attempt_count` is kept equal to the number of `schedule_run_attempts` rows inside the transaction.
- Schedule modifications write a redacted snapshot/hash to immutable `schedule_versions` and only advance `current_version_id`. A Run pins the version FK at creation and reads the execution template from that version.
- The live Schedule status and current policy determine execution permission, but the action/budget/documentation snapshot of already created Runs is never overwritten.
- At the end of the migration a deferred FK `schedules.current_version_id → schedule_versions.id` and a same-schedule ownership check are added.
- An Approval stores `subject_type/subject_id`, optional `task_id/schedule_id/run_id`, validity, maximum uses, and used count; consumption is atomic in a DB transaction.

### 6.7 Account Role and Approval Constraints

- `principal_role_assignments.account_id` references `accounts.id` and applies equally to Human, Agent, and service. There is no Agent-only Role assignment table.
- `approval_grants` is the command authority for scope, validity, and maximum count; `approval_consumptions` is the actual consumption ledger. `approvals_projection` is for display only and is never used for execution decisions.
- Phase 1 activates the `task` and `action` subjects. The `schedule` and `run` subject handlers/FKs and contract tests are activated in Phase 5 when Schedule entities exist.
- `approval_grants` enforces `subject_type IN ('task','schedule','run','action')`, `expires_at > valid_from`, `max_uses IS NULL OR max_uses > 0`. Exactly one target identifier per subject type; duplicate `subject_id`/individual FKs are not kept in parallel.
- The consumption transaction locks the grant row/aggregate expected sequence and commits the `approval_consumptions` insert, the `APPROVAL_CONSUMED` Event, and the ScheduleRun claim/Task creation or action outbox together. `(approval_id, consumption_key)` unique plus a consumption count query rejects over-use.
- States are limited to `PENDING`, `APPROVED`, `PARTIALLY_CONSUMED`, `CONSUMED`, `REJECTED`, `CANCELLED`, `EXPIRED`, `REVOKED`; terminal states cannot be consumed.

### 6.8 Task Graph and ArtifactLink Constraints

- Task state is limited to the enum and transition table of spec §8.2, and writes in `COMPLETED|CANCELLED` are rejected. Verification FAILED/BLOCKED/PASSED transitions produce only RUNNING/WAITING/VERIFIED respectively.
- `task_edges(child_task_id UNIQUE, parent_task_id, root_task_id, depth, created_event_id)` is never modified after creation. Parent/root/child must share a Workspace and the child inherits the parent's root. Only new children are added under existing parents, so re-parenting is forbidden, and self/ancestor cycles are rejected by command validation and a DB trigger.
- `task_assignments` is an append-only history unique on `(task_id, revision)` storing delegator, assignee, reason, policy snapshot, and Event. Reassignment is a new revision plus a `TASK_REASSIGNED` Event, never a row update.
- Join evaluation reads the children's latest Event sequence and PASSED Verification/FINALIZED Document, not the child projection, and appends `TASK_JOIN_SATISFIED` inside the transaction. ALL/ANY/QUORUM quorum values and the required child set are fixed in the parent policy snapshot.
- `artifact_links(artifact_id, subject_type, subject_id, relation, linked_by, linked_at)` is unique on `(artifact_id, subject_type, subject_id, relation)` and subject type is limited to `task|schedule_run|brainstorm|decision`. A subject handler registry checks existence, Workspace, and ACL; Task activates in Phase 1, ScheduleRun in Phase 5, Brainstorm/Decision in Phase 6.

### 6.9 Permission and Risk Catalog

`policy/permissions.yaml` is the authority for the permission vocabulary. Minimum set: `task.create|read|delegate|accept|progress|submit|complete|cancel`, `approval.request|decide|revoke`, `verification.assign|submit`, `artifact.write|read`, `document.draft|finalize|publish`, `secret.grant|lease`, `schedule.manage|run`, `agent.manage`, `channel.manage`, `bridge.manage`, `brainstorm.open|contribute|facilitate|summarize`, `admin.settings|accounts|break_glass|hard_delete`. A Role cannot hold a permission outside this vocabulary; schema validation rejects it.

`policy/risk-rules.yaml` maps action class → risk level. Risk levels are `LOW|MEDIUM|HIGH|CRITICAL` with the following default classification.

| action class | examples | default risk | default Approval |
|---|---|---|---|
| read/query | task_get, document_get | LOW | none |
| internal write | task_progress, artifact_register | LOW | none |
| delegation/routing | task_delegate, reassign, subtask | MEDIUM | channel policy |
| external_send | external API call, mail, delivery outside the Bridge | HIGH | Human 1 |
| destructive | delete, overwrite, revoke, hard-delete request | HIGH | Human 1 |
| secret_exposure | llm_exposure, secret grant scope expansion | CRITICAL | Human 2 |
| production_change | settings apply, policy rollback, schedule enable | HIGH | Human 1 |

When a Capability's `side_effect` flag conflicts with the action class, the higher risk applies. Unclassified actions are treated as `HIGH`, and the policy fixture (V-P0-18) enforces zero unclassified actions. Approver eligibility and quorum follow §7E.

## 7. Core API

### 7.1 Common

- Base `/api/v1`; setup uses `/setup/api/v1`.
- Humans use secure session/OIDC; Agents use service tokens or mTLS.
- The actor is determined from the credential.
- Every write carries `Idempotency-Key`; every request carries a correlation ID.
- Errors use Problem Details plus stable error codes.

### 7.2 Endpoint Groups

| Group | Main endpoints |
|---|---|
| Setup | `GET /setup/state`, `POST /setup/preflight`, `POST /setup/bootstrap` |
| Accounts | CRUD/suspend/invite/roles, credential rotate/revoke |
| External Identities | provider instance CRUD, link challenge/approve/list/suspend/revoke |
| Agents | register/update/test/activate/suspend/revoke/heartbeat/conformance |
| Roles | role/version CRUD, assignment, effective-permissions preview |
| Channels | import/configure/members/policy/document template |
| Bridges | Telegram Bridge CRUD/test/enable/disable/delivery status |
| Tasks | create/list/get/create-subtask/delegate/reassign/progress/submit/complete/cancel; graph/join queries |
| Events | append/query/SSE; low-level append restricted to designated services |
| Approval | request/get/decide/cancel/revoke/consumption history; consume is an internal command API |
| Artifact | upload intent/register/read/verify/archive |
| Brainstorm | start/guide/summarize/decide/taskify/close |
| Secret | metadata/register/grant/lease/revoke/audit; value read is a restricted route |
| Document | draft/attempt-finalize/finalize/review/publish/version/get |
| Verification | assign/submit-evidence/run/result/findings/recheck |
| Schedules | create/preview/update/enable/pause/resume/disable/run-now/history |
| Schedule Runs | get/list/retry/cancel; terminal retry creates a new Run with `retry_of_run_id`, terminal cancel is an immutable conflict |
| Operations | health/metrics/jobs/backup/maintenance/audit/settings |
| Admin | projection rebuild, policy diff/apply/rollback, break-glass session, hard-delete workflow |

### 7.3 Agent Adapter Contract

Every Adapter implements this interface.

```text
probe() -> identity/runtime/capabilities/delivery_modes[push|pull]/limits
deliver(work_item) -> delivery_receipt(work_item_id, accepted_at | rejection_code)
invoke(tool, input, deadline, secret_handles[]) -> result/events/artifacts/usage
cancel(task_id | work_item_id) -> acknowledgement
heartbeat() -> health/capacity/usage_since_last
normalize_error() -> stable error
```

Mandatory conformance:

- stable identity, idempotent delivery, timeout/cancel
- capability advertisement with explicit unsupported declarations
- secret handles never printed in logs/messages
- correlation/task/event IDs preserved
- no duplicate side effects on retry
- §7C usage reported, or a `usage_unavailable` reason code, on every result and heartbeat
- compliance with the §7B work item protocol (ack/accept/reject/result) and the advertised delivery mode

Runtime baseline norms:

- Heartbeat interval 30 s; `offline` after 3 consecutive misses or 90 s without heartbeat; capabilities are re-confirmed after a returning heartbeat.
- Task cancel acknowledgement within 10 s, safe cleanup within 60 s. Adapters that cannot comply fail conformance or do not advertise that capability.
- The routing eligible set is the intersection of active/online, channel membership, required capability, current capacity, and policy allow. Score ties are broken by ascending `agent_id` for reproducibility.

### 7.4 MCP Tool Surface

Core: `task_create`, `task_get`, `task_delegate`, `task_accept`, `task_progress`, `implementation_submit`, `approval_request`, `artifact_register`, `verification_submit`, `verification_evidence_submit`, `document_get`, `work_poll`, `work_ack`, `work_result`, `brainstorm_contribute`, `usage_report`.

Management tools are hidden by default and exposed to an Agent only with a separate admin capability: `agent_register`, `principal_role_assign`, `channel_configure`, `bridge_configure`, `secret_grant_create`.

Schedule tools are `schedule_create`, `schedule_preview`, `schedule_get`, `schedule_pause`, `schedule_resume`, `schedule_disable`, `schedule_run_now`, `schedule_run_cancel` and require the separate `schedule.manage`/`schedule.run` capabilities. They are not provided to ordinary Agents by default.

### 7.5 Interface Contract Rules

- OpenAPI and MCP JSON Schema are generated from the same application command/query handlers; no separate path bypasses state transitions, Policy, or idempotency.
- Write requests carry `Idempotency-Key`, `If-Match` or an expected aggregate sequence, and a correlation ID, and return the created resource ID, Event ID, and aggregate sequence.
- List/query enforce workspace scope, stable sort, cursor pagination, and an upper bound of `limit=100`. "Not found" and "forbidden" are normalized to the same 404 according to the information-disclosure policy.
- The SSE envelope includes `event_id`, workspace, aggregate type/id/seq, schema version, type, occurred/recorded time, correlation/causation, and redacted payload, and supports `Last-Event-ID` resume.
- MCP tool inputs and outputs include a versioned schema ID and return the same stable error codes as REST. Contract changes ship only as backward-compatible additive changes or a new major schema.
- Provider callbacks are validated for signature, a 5-minute timestamp tolerance, a one-time nonce, provider instance, and body hash before any normalization/outbox command is invoked.

## 7A. Mattermost Interaction Model

### 7A.1 Integration Method

- Inbound: the Agent-Colab bot account subscribes to the Mattermost WebSocket event stream (`posted`, `post_edited`, `reaction_added`) and receives interactive action callbacks. Outgoing webhooks are not used.
- Commands: the slash command `/colab` is registered, and the same grammar is accepted through an `@colab` mention. Free text without the prefix is never interpreted as a command (product principle 4).
- Outbound: posts are created and edited in place, ephemeral responses and DMs are sent through the REST API. Interactive action callbacks are received at `/api/v1/providers/mattermost/actions` and validated per §7.5 for integration token, 5-minute timestamp, one-time nonce, and body hash.
- A provider instance is `Mattermost base URL + team`; several teams are separate instances.

### 7A.2 Command Grammar

`/colab <resource> <verb> [positional...] [--key value ...]`

| resource | verb | minimum arguments | required permission |
|---|---|---|---|
| task | create, delegate, accept, reject, progress, submit, complete, cancel, reassign, show, list | create: `"title" --criteria "..."` (one or more, §7D.1) | task.* |
| approve | request, grant, reject, show, list | grant/reject: `<approval_id>` | approval.* |
| verify | assign, pass, fail, block, show | pass/fail: `<task_id> --evidence <ref>` | verification.* |
| brainstorm | start, contribute, summarize, decide, taskify, pause, resume, close, show | start: `"topic" --participants @a,@b` | brainstorm.* |
| doc | show, review, publish | `<task_id \| brainstorm_id>` | document.* |
| schedule | show, list, run-now, pause, resume, cancel-run | `<schedule_id \| run_id>` | schedule.* |
| link | start, confirm | — | self |
| notify | mute, unmute, digest | — | self |
| help | — | — | everyone |

- Arguments are validated by JSON Schema in `schemas/api/commands/*.json`. Errors are returned as ephemeral messages with the cause and a correct example and create no side effects. Successful responses are posted publicly in the thread.
- A command executed inside a Task thread targets that thread's Task when `<task_id>` is omitted. Brainstorm threads behave the same way.
- Every command goes Command Router → the same application command handler (§7.5) and applies the same Policy and idempotency (`provider_instance + post_id`) as REST/MCP.
- The command principal is the Account linked to the Mattermost user's active ExternalIdentityLink. Unlinked users can execute only `link` and `help`.

### 7A.3 Task Thread and Card

- One Task = one root post in the channel (the "Task card"). `Conversation.source_thread` is the root post ID. Sub-Tasks post a link card in the parent thread and have their own root post and thread.
- The card is edited in place: title, status badge, assignee, verification_status, pending Approvals, latest progress, Artifact/Document links, sub-Task join status. Every state transition edits the card and leaves one thread reply, forming an immutable log.
- Progress posts are coalesced per Task in 10-second windows. Bodies over 16k characters are stored as an Artifact and only linked.
- The card exposes buttons according to the actor's permissions: Accept, Submit, Approve/Reject, Verify pass/fail, Cancel. Buttons are conveniences; the server re-evaluates permissions at callback time and processes duplicate clicks of the same button once by idempotency key.
- A Brainstorm session is also one root post plus thread; its card shows participants, remaining turns, budget consumption, and status.

### 7A.4 Agent Identity Display

- Utterances of MCP/Webhook Agents are posted by the Agent-Colab bot with `override_username`/`override_icon_url` (the Agent display name). If the Mattermost configuration does not allow overrides, a `[agent-name]` prefix is the fallback; Setup preflight and the P0-10 spike determine which applies.
- Override values are set only by the server; an Agent cannot specify them in a result payload. Display identity contained in a payload is ignored and audited.
- A Mattermost bot adapter Agent posts with its own bot account, and that bot user ID is pinned by the Agent Account's ExternalIdentityLink.

### 7A.5 Linking a Human Account (challenge)

1. The user runs `/colab link start`; the bot sends a one-time code with a 10-minute TTL by DM.
2. The user enters the code in the logged-in web console or runs `/colab link confirm <code>`. The latter runs without a web session and therefore enters administrator-approval pending (`pending_admin`).
3. On success `ExternalIdentityLink.status=active`, `verification_method=signed_challenge|admin_approval`. After 5 failures the user is blocked for 15 minutes, and `IDENTITY_LINK_CHALLENGED`/`IDENTITY_LINK_VERIFIED` Events are recorded.

### 7A.6 Telegram Commands

Telegram is read/reply only by default. When channel policy allows commands, the §7A.2 grammar is accepted as a `/colab` bot command, with resources limited to `task show|list`, `approve show`, `doc show`, and any write verbs opened by policy. The principal is the Telegram user's ExternalIdentityLink (provider instance = bot token ID), linked by the same challenge as §7A.5.

## 7B. Agent Work Delivery Protocol

### 7B.1 Work Item

Every piece of work the server gives an Agent is a durable `work_item`.

```yaml
work_item_id: wi-...
kind: task_assignment | subtask_assignment | invoke | cancel | brainstorm_turn | verification_assignment
agent_id: ...
task_id: ...            # brainstorm_turn carries brainstorm_id
correlation_id: ...
deadline: ISO-8601
payload_ref: colab://work/wi-.../payload   # body fetched separately, 1 MB limit
secret_handles: [...]   # §9 lease handles
expected_result_schema: schema-id
idempotency_key: ...
```

States: `QUEUED → DELIVERED → ACKED → IN_PROGRESS → RESULT_RECEIVED | REJECTED | EXPIRED | CANCELLED`. If no `ACKED` arrives within 60 s of `DELIVERED`, the item is redelivered (at most 3 times); if a `task_assignment` is not `ACCEPTED` within 120 s, §7D.3 re-routing applies. Results are accepted exactly once per `work_item_id`; duplicate results are ignored and audited. Work item transitions are recorded as `WORK_ITEM_*` Events.

### 7B.2 Delivery Modes

Adapters advertise `delivery_modes` in `probe()`; the server prefers push and falls back to pull.

| Adapter | Delivery | Result return | Secret handle | Notes |
|---|---|---|---|---|
| MCP client | pull: the Agent long-polls the MCP tool `work_poll(max_wait≤30s)` or subscribes to resource `colab://inbox/{agent_id}` | MCP tool `work_result` | supported (sidecar or in-memory handle) | Agent-Colab is the MCP server |
| REST/Webhook | push: the server POSTs to the Agent endpoint with HMAC-SHA256 (signing key is a Secret Broker reference) + timestamp + nonce; 202 + receipt | REST `POST /api/v1/work/{id}/result` (service token) | supported | 5-minute timestamp window, nonces kept 24 h |
| Mattermost bot | push: a structured work message (JSON code block + bot mention) in the Task thread | the bot replies in the thread with structured JSON or a `/colab` command | unsupported (`secret_handles: unsupported` advertised) | parse failures are ephemeral errors with zero side effects |

local process and remote gateway adapters are out of scope for v8; the §7.3 contract and the work item schema are preserved so they can be added later.

### 7B.3 MCP Transport

- Agent-Colab exposes an MCP server over Streamable HTTP (`/mcp`). Authentication is a service token (Bearer) bound to the Agent Account or mTLS; stdio is for local development only.
- Tools are listed in §7.4; resources are `colab://inbox/{agent_id}`, `colab://task/{task_id}`, `colab://document/{document_id}`. If the client supports `resources/subscribe`, inbox change notifications are sent; otherwise long-polling only.
- On reconnect, items not yet `ACKED` are redelivered, and `work_result` is idempotent. One concurrent `work_poll` per session; rate limits follow Agent Limits.

### 7B.4 Accept, Reject, and Re-routing

- The Agent signals receipt with `work_ack` and acceptance with `task_accept` (MCP/REST/command). Rejection is possible immediately with a reason code (`CAPABILITY_UNSUPPORTED|CAPACITY|POLICY|OTHER`).
- Rejection, accept timeout, offline, revocation, and budget overrun follow the §7D.3 re-routing rule, and history is kept as `task_assignments` revisions and `TASK_REASSIGNED` Events.

## 7C. Usage Metering and Budget

- Unit: integer `cost_units`, 1 credit = 1,000,000 cost_units. The conversion table `policy/pricing.yaml` (per model/tool input/output token rates, tool-call rate, wall-time rate) is versioned, edited in Admin Settings, and audited.
- Reporting: every `work_result`, `invoke` result, and heartbeat includes `usage {model, input_tokens, output_tokens, tool_calls, wall_time_ms, cost_units?}`. If the Adapter does not provide cost_units, the server computes them from pricing; if the model is unknown, it computes with the default rate and marks `source=estimated`. If no usage is present at all, a `usage_unavailable` reason code is required, and conformance (V-P3-26) measures the ratio.
- Storage: `usage_records(agent_id, account_id, task_id?, run_id?, brainstorm_id?, document_id?, work_item_id, model, input_tokens, output_tokens, tool_calls, wall_ms, cost_units, source, pricing_version, reported_at)`. The document section "Inputs and Resources Used" is generated from this table.
- Where budgets are defined: Agent Limits (concurrent Tasks, requests per minute, brainstorm turns, daily and per-Task cost_units, per-Task wall time), Channel (daily cost_units), Schedule `budget_policy` (per-Run and daily cost_units, per-Run wall time).
- Enforcement: before delivering a work item, an estimate (recent average for the same Agent and kind, else the policy default) is reserved in `budget_reservations`. If the estimate would exceed the limit, the item is not delivered, the Task goes to `WAITING`, and a `BUDGET_EXCEEDED` Event and notification are produced. After the result arrives, the reservation is settled with actual usage, and an overrun blocks the next side effect. Limit values are integer cost_units; the "100" in V-P3-15/V-P5-28 means cost_units.

## 7D. Task Acceptance Criteria and Verifier Assignment

### 7D.1 Acceptance Criteria

- A Task must have at least one `acceptance_criteria` entry before it can be delegated. Each entry: `criteria_id, statement, check_type(evidence|test_command|artifact_hash|human_attest), required(bool)`. Default templates per channel type are provided, and criteria are accepted via the `--criteria` argument or REST/MCP fields.
- `implementation_submit` must attach an evidence ref per criterion; if a required entry is empty it is rejected with `EVIDENCE_REQUIRED`.
- Criteria are pinned in the Task policy snapshot; changes are possible only through a new revision and Event.

### 7D.2 Verifier Assignment

- eligible = `verification.submit` permission ∧ Task domain capability ∧ not the implementer/alias/credential (§6.4) ∧ (for Agents) online with capacity ∧ Human requirement (a Human Verifier is mandatory for risk HIGH and above or when channel policy says so).
- score = domain match (2) + inverse of recent load (1) + Human preference (1 when policy requires); ties are broken by ascending `account_id`.
- The Verifier receives a `verification_assignment` work item (criteria, evidence manifest, artifact refs, target commit/digest, read-only access information). If not accepted within 10 minutes, the next candidate is assigned; if none remains, the Task goes to `WAITING` with an Administrator notification.
- Human Verifiers submit verdicts through the web console or `/colab verify pass|fail|block`.

### 7D.3 Re-routing Rule

On rejection, accept timeout, offline, revocation, or budget overrun, the next-scored eligible candidate is assigned once; if none exists the Task stays `WAITING` and the delegator and channel are notified. Side effects already started are passed to the new assignee as `resume_context` (completed steps, Artifacts, last progress), and duplicate execution is forbidden.

## 7E. Approval Approver Model

- Eligibility: `approval.decide` permission ∧ membership in the target channel ∧ Role `max_risk ≥ action risk` ∧ not the requester, the implementing Agent, or their aliases. `requires_human_approval` actions and risk HIGH and above are approved by Humans only.
- Quorum (risk-rules defaults): LOW 0, MEDIUM Human 1, HIGH Human 1, CRITICAL two different Humans.
- Request delivery: Task card button + post in the approval-type channel + DM to eligible approvers (§7G). Decisions can also be made in the web console Approvals queue.
- Decision path: LOW/MEDIUM may be decided by Mattermost buttons; HIGH and above are decided in the web console after MFA re-authentication. Pressing a button for HIGH only shows guidance to the web path and does not approve.
- Default expiry 24 hours, reminder at 50%, and on expiry `APPROVAL_EXPIRED` and `APPROVAL_ESCALATED` (Administrator notification).
- Every decision records the `decided_by` Account and credential snapshot; self-approval attempts are rejected with `SELF_APPROVAL_FORBIDDEN` and audited.

## 7F. Brainstorm Engine

- Start: `/colab brainstorm start "topic" --participants ... [--turns-per-agent 5] [--max-consecutive 1] [--total-turns 40] [--budget <cost_units>] [--time <min>]`. The starter is the facilitator and needs the `brainstorm.facilitate` permission.
- Turn distribution: the server delivers `brainstorm_turn` work items (current transcript ref, remaining turns, expected contribution type) to participating Agents round-robin. Human participants speak freely; utterances without a command are recorded as `IDEA`. Agent contributions are accepted only through `brainstorm_contribute(type=IDEA|CHALLENGE|QUESTION|GUIDANCE)`.
- Limits: if per-Agent turns, consecutive same-Agent turns, total turns, budget, or time is exceeded, the session becomes `PAUSED` (`BRAINSTORM_PAUSED`) and guidance is requested from the facilitator. The facilitator either `resume`s (including limit adjustments, `BRAINSTORM_RESUMED`) or `close`s.
- Summary: `summarize` prefers an Agent with the `brainstorm.summarize` capability that is not a participant (otherwise the best-scored participant), creates a `SUMMARY` draft (`SUMMARY_RECORDED`), and the facilitator approves/edits before posting.
- Decision: the facilitator records `decide "statement" --rationale ... --source <event ids>`. `--vote` may attach a tally of participating Humans' reactions (👍/👎), but the facilitator remains the decision maker.
- Taskify: each action item of a Decision creates a Task with bidirectional `Decision → Task` provenance. Created Tasks follow §7D (acceptance criteria mandatory).
- Close: `close` produces `BRAINSTORM_CLOSED` and `DOCUMENT_DRAFTED`.

## 7G. Notification Service

| Event | Recipients | Channel | Default |
|---|---|---|---|
| APPROVAL_REQUESTED | eligible approvers | Task thread mention + DM, approval channel | immediate, reminders at 50% and expiry |
| VERIFIER_ASSIGNED | Verifier | work item + DM | immediate, re-notify if not accepted in 10 min |
| TASK_WAITING / BUDGET_EXCEEDED | delegator, channel | thread + ops channel (budget) | immediate |
| RUN result/delay | Schedule channel | §10A | immediate |
| BREAK_GLASS, HARD_DELETE, dependency failure | ops channel, Administrators | channel + SMTP (if configured) | immediate |
| AGENT_MARKED_OFFLINE | Agent owner | DM | immediate, 1-hour dedupe |

- Notifications are defined by `notification_rules` (event, recipient selector, channels, dedupe window, quiet hours) and edited by Administrators.
- Delivery reuses the delivery outbox for exactly-once sending (`NOTIFICATION_SENT`); whether to relay externally follows the Telegram Bridge policy. Notifications are not state authority; losing one never affects Event/Task state.
- Per-user mute and 1-hour digest are configured with `/colab notify` or in the web console.

## 7H. Message Ingestion, Retention, and i18n

- Ingestion scope: every post in Task and Brainstorm threads, messages relayed by Bridges, and the whole channel when the channel documentation policy is `full_channel`. Storage is normalized after redaction; original attachments follow the Artifact policy.
- Retention: per-channel `retention_days` (default 365), suspended under `legal_hold`. A daily retention job deletes expired Messages by DEK destruction and leaves tombstones. Document provenance marks deleted Messages as `REDACTED_BY_RETENTION`.
- i18n: `i18n/{ko,en}/` resource bundles. The instance default language is set in Setup step 1 and can be overridden per channel. Mattermost/Telegram posts, ephemeral errors, the web console, and Document template headings are localized; Event types, error codes, and IDs are never translated.
- Single Workspace: v8 creates one Workspace per instance during Setup. Entities and scope checks are kept, but a Workspace CRUD API is out of scope.

## 8. Setup and Settings Implementation

### 8.1 State Machine

`UNINITIALIZED → PREFLIGHT_PASSED → BOOTSTRAPPING → CONFIGURED → LOCKED`

On failure the state moves to `BOOTSTRAP_FAILED` and a retry token stripped of sensitive data is issued. The setup token is CSPRNG ≥ 256 bits, 30-minute TTL, single-use, and 5 failures within a 15-minute window per IP and token fingerprint block the source for 15 minutes. After `LOCKED`, `/setup/bootstrap` responds 404 or 403. In `LOCKED`, when the System Owner enables maintenance mode and re-authenticates with the recovery code and MFA, a 30-minute `RECONFIGURING` session opens by default; on completion or expiry it returns to `LOCKED`, and every reconfiguration action is audited.

The pre-DB state is held by the sealed local store `/var/lib/agent-colab/bootstrap/state.json`. Only the service owner can read/write it, and it stores only setup state, one-time token hash, and non-secret configuration pointers. DB credentials and initial key material are kept only as 15-minute TTL handles in process memory or the OS credential store and re-entered after a restart. After DB connection and migration, the state moves to `setup_state` in a transaction and the local file keeps only a `LOCKED` marker and minimal recovery metadata. On failure, both states are reconciled without regressing to a lower stage, and secret values are written to neither.

The internal apply order is `DB/migration → master key/Secret provider → Owner account/TOTP/recovery code → integration settings → atomic CONFIGURED/LOCKED commit`. Even if the UI collects information earlier, Owner and TOTP records are not shown as created before the DB and key provider are ready. `/setup` binds to loopback by default; remote access is enabled only with a preconfigured HTTPS/TLS proxy, client mTLS, and an IP allowlist all present.

### 8.2 Settings Layers

Precedence is `emergency env > encrypted runtime setting > setup default > built-in default`. Each setting has scope, type, validation, restart requirement, secret flag, version, and changed_by.

### 8.3 Web Requirements

- Per-step preflight and error recovery guidance
- Secret fields never re-displayed
- Redacted diff before final apply
- DB migration/connection, Mattermost, storage, and secret provider tests
- Setup transport/bind/HTTPS-TLS/client-mTLS/allowlist/token tests and Owner/TOTP creation-order validation
- Default IANA timezone, scheduler polling/lease, minimum interval, and missed-run defaults
- Recovery code shown once after completion
- Admin Settings modify with the same validation and audit

## 9. Secret Broker Implementation

### 9.1 Provider Interface

```text
put(name, value, metadata) -> secret_ref/version
lease(secret_ref, subject, ttl, scope) -> one_time_handle
resolve(handle, authenticated_adapter) -> secret bytes/derived credential
revoke(grant_or_lease)
rotate(secret_ref)
health()
```

### 9.2 v8 Providers

- Mandatory: encrypted local provider. The master key is separated from DB/backups and protected by OS secret/file permissions.
- Optional: one external provider implemented in Phase 4 or provided as a verified adapter skeleton.
- Production recommendation: an external provider that supports short-lived credentials.

### 9.3 Delivery Rules

- Secret values never appear in chat/Mattermost/Telegram/MCP ordinary responses.
- An Adapter sidecar or a separate authenticated resolve channel is used.
- TTL default 5 minutes, single-use by default, revoked at Task end. After revocation, new resolves are rejected immediately and already-issued handles/sidecar leases are invalidated and cleaned up within 5 seconds.
- Passing a secret into LLM context requires the separate exposure flag and Human approval.
- Leak tests for logs/Events/Documents are automated with redaction canaries.
- Key tombstones are recorded in an append-only KMS or signed ledger separated from runtime and backups. Restore performs tombstone reconciliation before the service opens, and backups containing destroyed DEKs are never re-registered as decryption targets.

### 9.4 Secret Sidecar

- A separately deployed component `sidecar/` (Python package `agent-colab-sidecar`, also shipped as an OCI image). It runs on the Agent host and authenticates to the Broker with the Agent Account's mTLS or service token.
- Behavior: receives the work item's secret handle, `resolve`s it, injects the value through a Unix domain socket or process environment/fd, and never writes to disk. It detects revocation through Broker revoke push (SSE) or 5-second polling and, within 5 seconds, clears memory and invalidates child process environments.
- A handle is bound to the sidecar instance ID and cannot be resolved from another host.
- Logs record only handle IDs and results, never values, lengths, or hashes. The Mattermost bot adapter does not support the sidecar.

## 10. Documentation Service Implementation

### 10.1 Pipeline

`SOURCE_FREEZE → COLLECT → DRAFT_PRE_VERIFICATION → LINK_PROVENANCE → REDACT → INDEPENDENT_VERIFY → FINALIZE_NEW_VERSION → HUMAN_REVIEW? → PUBLISH → ARCHIVE`

The pre-verification draft never contains the final verification verdict. If the VerificationRun is `FAILED`/`BLOCKED`, an immutable `ATTEMPT_FINALIZED` version containing result, evidence, and residual risks is created and the Task is reopened/blocked. Only when the latest VerificationRun is `PASSED` is a new `FINALIZED` version created and the Task completion/publish gate opened.

### 10.2 Sources

- Mattermost/Telegram mapped messages
- Task/Event/Approval/Decision/Schedule/ScheduleRun
- Artifact metadata and selected contents
- Verification evidence/results
- resource usage: Agent/model/runtime/tool/time/token/cost (where available)

### 10.3 Publisher Contract

```text
publish(document, manifest, destination) -> external_ref/version
update(document_id, new_version) -> external_ref/version
verify(external_ref, checksum) -> result
archive(external_ref) -> result
```

v8 implements canonical Markdown + JSON manifest, filesystem/NAS storage, and a Git-compatible publisher. Gitea can be used as a Git remote. BookStack/Wiki.js keep a common adapter contract, and a reference connector for one of the two is implemented and verified with contract/integration tests.

### 10.4 Document Generation Method

- Layer 1 (deterministic skeleton): every section is generated by a template engine from Events, Artifacts, Verification, usage_records, and Decisions. No LLM is used, and the same source freeze always yields the same bytes (hash-reproducible).
- Layer 2 (narrative, optional): a Documentation Agent with the `document.narrate` capability writes the narrative paragraphs of "Discussion, Alternatives, Decisions and Rationale" and "Shortcomings, Risks and Open Questions". Every paragraph must include at least one citation (`[[evt:<event_id>]]`, `[[art:<artifact_id>]]`, `[[dec:<decision_id>]]`, `[[vr:<verification_id>]]`); the linter rejects sentences without citations, non-existent IDs, and figures contradicting the skeleton.
- If no Documentation Agent is available or it declines, the skeleton-only document is a valid draft. The narrative layer cannot overwrite the skeleton's structured facts.
- Generation cost is recorded in usage_records under the `document_id` scope and counted against the Task/Brainstorm budget.

## 10A. Schedule Service Implementation

### 10A.1 Schedule Definition

- `cron_expression`: the numeric five-field grammar of spec §8.6
- `timezone`: IANA timezone
- `action_template`: allowed action schema, defaulting to `task_create`
- `channel_id`: the Mattermost channel where execution notices and results are posted
- `execution_principal_id`: the principal evaluated for permissions on every Run
- `agent_selection`: fixed agent or capability query; fixed product names forbidden
- `concurrency_policy`: `FORBID`, `ALLOW`, `REPLACE`
- `missed_run_policy`: `SKIP`, `RUN_ONCE`, `BACKFILL_LIMITED`
- retry/backoff, max duration, start/end, status lifecycle, budget policy, documentation policy
- scheduler defaults: polling 15 s, claim lease 60 s, running lease heartbeat 15 s; operational settings limited to poll 5–60 s with the lease at least 3× the poll

### 10A.2 Execution Algorithm

1. The planner materializes due times within the horizon in UTC.
2. Using the Schedule's current version snapshot, the `occurrence_key` and Run are created exactly once and the `schedule_version_id` is pinned.
3. A runner claims the due Run with a DB lease.
4. The action template is read from the Run's pinned ScheduleVersion, and the current Schedule status, execution principal, Role/Capability, Channel, Approval, and Secret references are re-checked.
5. If policy allows, a Task is created with a deterministic idempotency key and committed in the same transaction as the Approval consumption.
6. Task lifecycle, independent verification, and result documentation reuse the normal flow.
7. Success, failure, skip, and timeout are recorded as Events and Run history and posted to the channel through the Renderer.
8. On server restart, recovery applies lease expiry and the missed-run policy.
9. `RUN_ONCE` creates only the most recent missed occurrence; `BACKFILL_LIMITED` creates occurrences within the window, oldest first, up to the limit, preserving the original `scheduled_for`.
10. Transient retries default to at most 3 attempts with 1/5/25 s backoff plus 0–20% jitter and are recorded in `schedule_run_attempts`. Permanent errors are terminal `FAILED` immediately.
11. Cancel moves a pending Run to `CANCELLED` immediately, and a running Run to `CANCEL_REQUESTED` and then `CANCELLED` within Adapter ack 10 s and cleanup 60 s. Finished Runs are never changed.
12. `REPLACE` starts the new Run only after confirming cancel/cleanup of the existing Run. If not confirmed within 60 s, the new Run ends with `status=SKIPPED`, `error_code=SKIPPED_REPLACE_CANCEL_TIMEOUT` and does not execute concurrently.
13. A manual retry of a terminal Run creates a new `RETRY` Run linked to the original by `retry_of_run_id`.

### 10A.3 Time and DST

- Storage and comparison in UTC; the user's schedule timezone is preserved as an IANA name.
- UI/API preview at least 10 upcoming run times in local time and UTC.
- Non-existent DST local times skip the occurrence and record the reason.
- Duplicated local times run once per wall-clock occurrence by default.
- `occurrence_key = SHA256(schedule_id | timezone | YYYY-MM-DDTHH:mm)`. The DST fold offset difference is not part of the key, and the first chosen UTC instant is stored as `scheduled_for`.
- If a timezone DB update changes the next run time, the administrator is shown a diff.

### 10A.4 Permissions and Secrets

- The creator's permissions are never cloned permanently; an explicit execution principal is used.
- If at execution time the principal is suspended/revoked or has lost the capability, the Run is `status=SKIPPED`, `error_code=SKIPPED_POLICY`.
- Secret values cannot be placed in templates; only Secret references are allowed.
- Long-lived Schedules never receive a permanent Secret Grant automatically; each Run creates a short lease.
- High-risk actions require an Approval per Run or within the limited period/count allowed by policy.

### 10A.5 Web Management

- cron builder and raw cron input, timezone, next-10 preview
- channel, Task template, capability-based Agent selection
- concurrency/missed run/retry/timeout/budget
- enable/pause/resume/disable/run now and individual Run cancel
- Run history with Task/Artifact/Document/Verification links
- failure trends, next run, lag, policy denials, backfill warnings

## 11. Web Admin Console

### 11.1 Screens

- Setup Wizard
- Overview/health/alerts
- Accounts & Roles
- Agents & Capabilities & Conformance
- Channels & Telegram Bridges
- Tasks/Approvals/Verification
- Schedules/Run history/cron preview
- Secrets metadata/grants/audit
- Documents/Publishers
- Settings/Policy versions
- Backup/Restore/Maintenance
- Audit Explorer
- Approvals queue (§7E)
- Work inbox / usage / budget consumption

### 11.2 Security/UX

- Server-side authorization is mandatory; UI hiding alone never controls access.
- CSRF, CSP, secure cookies, session expiry, re-authentication for critical actions.
- Destructive/revoke/apply actions show target and impact and require confirmation.
- WCAG 2.1 AA target with keyboard, labels, contrast, and error summaries.
- Secrets are shown as metadata only in lists.

## 12. Phase Operating Rules

Each Phase is split into two Tasks.

1. `P<n>-IMPLEMENT`: the implementing Agent submits code, migrations, docs, and an evidence manifest.
2. `P<n>-VERIFY`: a different Agent executes the validation plan in a fresh context.

Mandatory rules:

- Implementer and verifier are assigned with different Agent IDs and credentials.
- The implementing Agent cannot modify verification results.
- The Verifier does not, as a rule, fix product code; it submits Findings and reproduction steps.
- On FAIL the same Phase's implementation Task is reopened, and after the fix a new VerificationRun is created.
- BLOCKED is used only for external conditions such as environment or permissions and is never treated as PASS.
- Phase N+1 starts automatically as soon as Phase N has a Verifier PASS. In v8 there is no human approval between phases.

### 12.1 Work Package Operating Rules

- Size: `S` (≤ 2 days for one implementing Agent), `M` (≤ 5 days), `L` (≤ 10 days). Table values are initial estimates confirmed in P0-14. An `L` package is broken into at least two sub-items before it starts.
- Prerequisites: a package does not start until every package in its `Prereq` column is `IMPLEMENTED`. Packages whose prerequisites are satisfied may be assigned in parallel within a Phase. The prerequisite DAG must be acyclic (V-P0-20).
- Verification: the Test IDs in the `Tests` column are the package's minimum self-test set. The implementer must submit `SELF-*` evidence for those IDs to mark the package `IMPLEMENTED`, and the Phase Verifier re-runs the same IDs independently.
- Progress: Phase progress is the size-weighted sum of `IMPLEMENTED` packages (S=1, M=2.5, L=5) and is subject to the spot rechecks of validation plan §7.4.

### 12.2 Autonomous Execution Rules (v8)

- Roles: the implementing Agent is Claude Code; the verifying Agent is Codex, invoked as a separate process with a fresh context for every VerificationRun. Neither role may be swapped or merged during the run.
- No human phase gates: the implementer proceeds from one package to the next and from one Phase to the next without asking for confirmation. Verifier PASS is the only transition trigger.
- Questions to the user are allowed only when something cannot be determined with certainty from the three documents, the repository, `.env`, or the environment (for example a missing credential, an unreachable external system, or a genuine contradiction in the documents). Preferences, style, and design choices already fixed by the documents are never asked about.
- Source control: at the end of every Phase, after Verifier PASS, the branch is merged and pushed to the designated GitHub repository. Push access must be confirmed before Phase 0 starts.
- Completion: when Phase 7 has PASSED, the implementer produces the final development report (§27A), pushes everything to GitHub, and then asks the user for exactly one decision — whether to deploy. Deployment starts only after an explicit "yes".
- Criteria are never weakened by the implementer. If a Test cannot be executed, the Phase is FAILED or BLOCKED, never PASSED by exception.

## 13. Phase 0 — Baseline and Bootstrap

### Work packages

| ID | Work | Completion criteria | Prereq | Size | Tests |
|---|---|---|---|---|---|
| P0-01 | repo/branch/CI skeleton | lint/test/build commands run from a clean clone | — | S | V-P0-03 |
| P0-02 | ADRs and requirement IDs | v8 principles, scope, Requirement registry (spec Appendix A) linkage, owners for open items | — | S | V-P0-01, V-P0-02, V-P0-10, V-P0-14, V-P0-15, V-P0-20 |
| P0-03 | schema/policy/Event contract | aggregate streams, canonical JSON/hash, valid/invalid fixtures, version rules | P0-01 | M | V-P0-05, V-P0-06, V-P0-13 |
| P0-04 | Compose dev stack | Postgres/server/web-admin/ClamAV health | P0-01 | S | V-P0-04 |
| P0-05 | Setup state skeleton | uninitialized/locked states and token guard | P0-03 | S | V-P0-12 |
| P0-06 | threat model | external identity, Bridge, Setup transport/order, Secret/sidecar, hard-delete/backup resurrection boundaries | P0-02 | S | V-P0-08, V-P0-09 |
| P0-07 | verification harness | implementer≠verifier DB/application constraints | P0-03 | S | V-P0-07 |
| P0-08 | Schedule contract | cron/timezone/occurrence key/version/status/action/concurrency/missed/retry/cancel schema and fixtures | P0-03 | M | V-P0-11 |
| P0-09 | pre-DB bootstrap store contract | loopback/remote HTTPS-TLS+client-mTLS+allowlist+token conditions, sealed state, memory/OS TTL handles, DB→key→Owner/TOTP order, reconciliation fixtures | P0-05 | M | V-P0-12 |
| P0-10 | Mattermost interaction contract and spike | §7A command grammar JSON Schema, Task card/thread rules, action callback contract, override identity and slash-command registration spike result (possible/not possible with fallback) | P0-03 | M | V-P0-16 |
| P0-11 | Agent work-item/usage contract and MCP spike | §7B work item schema and state machine, HMAC webhook contract, §7C usage schema and pricing.yaml schema, Streamable HTTP long-poll/subscribe spike | P0-03 | M | V-P0-17 |
| P0-12 | permission/risk catalog | §6.9 permissions.yaml, risk-rules.yaml, §7E approver quorum defaults, zero unclassified actions | P0-03 | S | V-P0-18 |
| P0-13 | Telegram API spike | forum topic/reply/edit/rate-limit constraints confirmed and Bridge thread mapping rules fixed | — | S | V-P0-19 |
| P0-14 | plan operating baseline | sizes confirmed, prerequisite DAG acyclic, risk→package mapping (§25A), dependency owners/deadlines (§25), package↔Test mapping completeness | P0-02 | S | V-P0-20 |

### Implementer deliverables

commit SHA, build manifest, dependency lock, schema/policy diff, test results, known gaps, setup screenshots or API evidence.

### Independent verification

The Architecture Verifier executes `V-P0-*`. Clean-environment reproduction, removal of names/fixed roles, schema consistency, and self-verification rejection must all PASS.

### Exit Gate

- The clean install skeleton and CI succeed.
- No specific Agent product or machine role is hard-coded in core schema/policy.
- A verification pass submitted by the same Agent as the implementer is rejected.

## 14. Phase 1 — Core Event/Policy

### Work packages

| ID | Work | Completion criteria | Prereq | Size | Tests |
|---|---|---|---|---|---|
| P1-01 | DB migration/roles | Event/Audit/Verification revision UPDATE/DELETE blocked, workspace FKs | P0-03 | M | V-P1-05, V-P1-25 |
| P1-02 | aggregate Event append | aggregate seq/CAS, scoped idempotency, causality, canonical hash chain, schema validation | P1-01 | L | V-P1-01~04, V-P1-06, V-P1-21 |
| P1-03 | Policy Engine | role/capability/scope/deny precedence, §6.9 catalog applied | P1-01, P0-12 | M | V-P1-07 |
| P1-04 | Task state/projection | explicit enum/transition/cancel/recheck, read-after-write and rebuild equivalence | P1-02 | M | V-P1-09, V-P1-10, V-P1-27 |
| P1-05 | identity/service token/external link core | actor cannot be spoofed, rotate/revoke, provider identity link lifecycle | P1-01 | M | V-P1-08, V-P1-23 |
| P1-06 | VerificationRun core | Account/Agent/credential/alias snapshot, assignment/evidence/result/immutable revision | P1-02, P1-05 | M | V-P1-12~14, V-P1-24 |
| P1-07 | REST/MCP/SSE | common command handler, versioned schema, scoped idempotency and optimistic concurrency, cursor resume and ACL | P1-02, P1-03 | L | V-P1-11, V-P1-26 |
| P1-08 | Approval Core | Phase 1 task/action subjects, explicit states, scope, expiry, cancel/revoke, authoritative bounded-use consume, §7E approver eligibility, self-approval ban, quorum, expiry escalation | P1-02, P1-03 | L | V-P1-15, V-P1-16, V-P1-22, V-P1-32 |
| P1-09 | Artifact Core | metadata, checksum, ACL, `artifact_links`; Task subject active, Run/Brainstorm/Decision handlers activated in their phases | P1-02 | M | V-P1-17 |
| P1-10 | Document lifecycle Core | draft, failed/blocked attempt-finalized, finalization after PASSED, provenance, immutable versions, §10.4 layer-1 deterministic skeleton | P1-04, P1-06, P1-09 | M | V-P1-18~20 |
| P1-11 | Task acceptance criteria | §7D.1 model, mandatory before delegate, per-criterion evidence on submit, snapshot pinning | P1-04 | S | V-P1-28 |
| P1-12 | Work item inbox core | §7B.1 durable inbox, state machine, ack 60 s/accept 120 s timeouts, 3 redeliveries, exactly-once results | P1-02, P1-04 | M | V-P1-29 |
| P1-13 | Notification core | §7G rules engine, outbox reuse, recipient selectors, dedupe/quiet hours; providers stubbed | P1-02 | S | V-P1-31 |
| P1-14 | Usage/Budget core | §7C usage_records, pricing.yaml versions, estimated computation, reservation/settlement API | P1-02 | M | V-P1-30 |

### Independent verification

The Core Verifier verifies concurrent append, duplicate retry, invalid transitions, policy deny, projection rebuild, token actor spoofing, and self-verification with `V-P1-*`.

### Exit Gate

- Task create→delegate→Approval?→submit→Artifact→draft→independent verify→finalize→complete works without Agents or Mattermost.
- Zero Event duplicates/modifications and zero verification-independence bypasses.

## 15. Phase 2 — Mattermost and Telegram Bridge

### Work packages

| ID | Work | Completion criteria | Prereq | Size | Tests |
|---|---|---|---|---|---|
| P2-01 | Mattermost provider | WebSocket/REST client, bot account, slash command registration, 4 template channels | P1-07, P0-10 | M | V-P2-01, V-P2-19 |
| P2-02 | Channel/external identity config | membership/policy/document template CRUD, Mattermost/Telegram Account links | P2-01, P1-05 | M | V-P2-19, V-P2-20~22 |
| P2-03 | Renderer/outbox | Event commit and delivery enqueue in one transaction, zero loss/duplicates on rollback and replay | P2-01 | M | V-P2-02, V-P2-23 |
| P2-04 | Telegram provider | bot/chat/topic send/receive, P0-13 constraints applied | P1-07, P0-13 | M | V-P2-09, V-P2-11 |
| P2-05 | per-channel Bridge | direction/filter/redaction/thread mapping, duplicate Telegram target rejected by default with explicit exception | P2-03, P2-04 | M | V-P2-03, V-P2-05, V-P2-06, V-P2-13, V-P2-14, V-P2-17 |
| P2-06 | dedupe/loop/retry | echo 0, outage replay, dead-letter | P2-05 | M | V-P2-04, V-P2-07, V-P2-08, V-P2-10, V-P2-15 |
| P2-07 | Bridge Admin UI | create/test/enable/disable/status | P2-05 | S | V-P2-12, V-P2-13 |
| P2-08 | Telegram command policy | read/reply only by default, §7A.6 restricted grammar, execution only with a verified active ExternalIdentityLink and channel permission | P2-05, P2-10 | S | V-P2-16, V-P2-20 |
| P2-09 | Channel lifecycle | soft delete after archive/mapping check, referential integrity kept | P2-02 | S | V-P2-18 |
| P2-10 | Command Router | §7A.2 `/colab`/`@colab` parser, JSON Schema argument validation, thread-context target resolution, ephemeral errors/help, provider idempotency, unlinked-user restriction | P2-01, P1-07 | M | V-P2-24 |
| P2-11 | Task card/thread Renderer | §7A.3 root post/thread binding, in-place card editing, per-transition thread log, 10-second coalescing, Artifact link over 16k | P2-03, P2-10 | M | V-P2-25 |
| P2-12 | Interactive actions | button callback endpoint, §7.5 signature validation, server-side authz at callback time, exactly-once duplicate clicks | P2-11 | S | V-P2-26 |
| P2-13 | Mattermost link challenge | §7A.5 `/colab link` DM code, TTL/single-use/lockout, web confirmation and administrator approval | P2-02, P2-10 | S | V-P2-27 |
| P2-14 | Agent identity display | §7A.4 override posting, prefix fallback, override values server-only | P2-11 | S | V-P2-28 |
| P2-15 | Message ingestion/retention | §7H ingestion scope, normalized storage, retention job, legal hold, tombstones, provenance marks | P2-03 | M | V-P2-29 |
| P2-16 | i18n | §7H ko/en bundles, instance/channel language, applied to Mattermost/Telegram/Web/Document headings | P2-10 | S | V-P2-30 |
| P2-17 | Notification providers | Mattermost mention/DM and optional SMTP providers connected to P1-13 rules, mute/digest | P1-13, P2-01 | S | V-P2-31 |

### Independent verification

The Integration Verifier configures 2+ Mattermost channels and different Telegram chats/topics and verifies with `V-P2-*` cross-channel leakage, echo loops, duplicates, thread mapping, outage recovery, secret redaction, plus command grammar, cards/threads, interactive actions, link challenge, identity display, retention, i18n, and notification delivery.

### Exit Gate

- Mattermost works as the official first conversation channel.
- Each channel Bridge activates/deactivates independently.
- Zero echo, duplicates, and cross-deliveries in the 100-message bidirectional test.

## 16. Phase 3 — Generic Agents and Role Management

### Work packages

| ID | Work | Completion criteria | Prereq | Size | Tests |
|---|---|---|---|---|---|
| P3-01 | Agent Registry | register/update/activate/suspend/revoke | P1-05 | M | V-P3-01, V-P3-08, V-P3-11, V-P3-17 |
| P3-02 | Role/Capability | create/version/assign/effective preview | P1-03 | M | V-P3-02, V-P3-09, V-P3-16 |
| P3-03 | Adapter SDK/contract | §7.3 probe/deliver/invoke/cancel/heartbeat, delivery_modes, usage | P1-12 | M | V-P3-05, V-P3-06, V-P3-07 |
| P3-04 | default Adapters | MCP, REST/Webhook, Mattermost bot (3 types) | P3-03, P3-10, P3-11, P3-12 | M | V-P3-05, V-P3-12 |
| P3-05 | conformance suite | automated execution and report of validation plan §11.1 CS-01~12 | P3-03 | M | V-P3-05 |
| P3-06 | routing | capability/channel/capacity candidate selection, §7.3 deterministic tie-break | P3-01, P3-02 | M | V-P3-03, V-P3-04, V-P3-10 |
| P3-07 | Agent Admin UI | connection test, role/channel/limit editing | P3-01 | M | V-P3-13 |
| P3-08 | Limits enforcement | server-enforced concurrent Task/rate/turn/cost_units/time limits (§7C reservation/settlement) with overrun audit | P1-14, P3-01 | M | V-P3-15 |
| P3-09 | multi-Agent orchestration | same-Workspace acyclic parent/root Task graph, depth/fan-out/concurrency limits, ALL/ANY/QUORUM join, reassignment history, parent completion gate | P3-06 | L | V-P3-18~20 |
| P3-10 | MCP server transport | §7B.3 Streamable HTTP, Bearer/mTLS, work_poll/ack/result, inbox resource/subscribe, redelivery on reconnect | P1-07, P1-12, P0-11 | L | V-P3-21 |
| P3-11 | Webhook push delivery | §7B.2 HMAC/timestamp/nonce POST, receipt, retry/backoff, REST result intake | P1-12, P0-11 | M | V-P3-22 |
| P3-12 | Mattermost bot adapter delivery | §7B.2 structured work message, JSON/command reply parsing, secret-unsupported advertisement | P1-12, P2-11 | M | V-P3-23 |
| P3-13 | Verifier assignment engine | §7D.2 eligibility/score/Human requirement, verification work item, 10-minute timeout reassignment | P1-06, P3-06 | M | V-P3-14, V-P3-24 |
| P3-14 | accept timeout/re-routing | §7B.4/§7D.3 rejection codes, 120-second timeout, one reassignment, WAITING/notification, resume_context | P3-06, P1-12 | S | V-P3-25 |
| P3-15 | usage reporting conformance | §7C usage included, estimated fallback, usage_unavailable reason, applied to all 3 Adapters | P1-14, P3-03 | S | V-P3-26 |

### Independent verification

The Agent Conformance Verifier registers 3 different adapter types and verifies with `V-P3-*` the identical Task protocol, role changes, revocation, offline, unsupported capability, work item delivery for all 3 types (MCP/webhook/bot), Verifier assignment, accept-timeout re-routing, and usage reporting.

### Exit Gate

- 3 Adapter types participate without specific Agent product names.
- Agents and Roles can be added, changed, and suspended in the web console.
- After permission revocation, new requests are rejected immediately and in-flight Task policy is handled per rule.
- Requests exceeding Agent Limits are rejected server-side and audited.
- Parallel sub-Tasks of 3+ Agents join per policy, and cycles, depth overruns, and unverified sub-results cannot open parent completion.

## 17. Phase 4 — Admin, Setup, Secrets

### Work packages

| ID | Work | Completion criteria | Prereq | Size | Tests |
|---|---|---|---|---|---|
| P4-01 | Account Admin | Human/Agent/service CRUD, suspend, common principal role assignment | P1-05 | M | V-P4-07, V-P4-26 |
| P4-02 | Operations/Audit dashboard | dependencies, Tasks, Agents, outbox, backup, audit search/export | P1-07 | M | V-P4-16, V-P4-23 |
| P4-03 | Setup Wizard | loopback default, remote HTTPS/TLS+client mTLS+allowlist+token, DB→key→Owner/TOTP order, Mattermost/storage/provider preflight and persistence, sealed state/reconciliation, endpoint lock | P0-09, P4-05 | L | V-P4-01~04, V-P4-19, V-P4-24, V-P4-27, V-P4-28, V-P4-30 |
| P4-04 | Settings | typed validation, diff, audit, rollback | P4-02 | M | V-P4-05, V-P4-06 |
| P4-05 | local Secret provider | encryption, key separation, metadata | P1-01 | M | V-P4-10, V-P4-17 |
| P4-06 | Grant/Lease/Broker | Task/action/Agent/TTL/single-use | P4-05 | M | V-P4-11, V-P4-12, V-P4-15 |
| P4-07 | Adapter injection | sidecar/in-memory, cleanup/revoke | P4-06, P3-03 | M | V-P4-13, V-P4-14 |
| P4-08 | admin security | re-auth, CSRF/CSP, rate limits | P4-02 | M | V-P4-02, V-P4-08, V-P4-09, V-P4-18 |
| P4-09 | MFA/OIDC | Owner/Administrator TOTP MFA mandatory, Member policy, Agent/service excluded, OIDC adapter interface (optional) | P1-05 | M | V-P4-20 |
| P4-10 | break-glass | recovery code + MFA re-auth, time-limited session, immediate announcement, automatic post-hoc verification Task | P4-09 | M | V-P4-21 |
| P4-11 | hard delete workflow | dual approval, waiting period, DEK destruction, immutable hash, separate key tombstone ledger, resurrection blocked after restore | P4-05, P1-02 | L | V-P4-22, V-P4-25, V-P4-29 |
| P4-12 | Secret sidecar | §9.4 package/OCI image, Agent authentication, socket/env/fd injection, revoke push/5-second poll, no disk, host-bound handles | P4-06, P4-07 | L | V-P4-31 |
| P4-13 | maintenance mode | non-admin writes 503+Retry-After, scheduler pause, outbox drain continues, enter/exit audit and announcement | P4-02 | S | V-P4-32 |
| P4-14 | Web Approvals queue and re-authentication | §7E queue screen, MFA re-auth decisions for HIGH and above, quorum display, escalation | P1-08, P4-09 | M | V-P4-33 |

### Independent verification

The Security/Ops Verifier verifies clean install, setup replay, privilege escalation, secret canary leakage, expired/reused grants, revoked Agents, UI/API authz parity, and backup key separation with `V-P4-*`.

### Exit Gate

- A clean environment is configured through the Web Wizard.
- Bootstrap cannot be re-run after Setup completes.
- Zero secret canary exposures in chat/Events/logs/Documents.
- Agent/account management and server status are available in the web console.
- break-glass, reconfiguration, and hard delete run only through the defined workflows.

## 18. Phase 5 — Scheduled Work

### Work packages

| ID | Work | Completion criteria | Prereq | Size | Tests |
|---|---|---|---|---|---|
| P5-01 | Schedule schema/API | status enum, immutable version FK, occurrence/manual/retry Runs, Run ArtifactLink handler, CRUD/preview/lifecycle/history/cancel | P0-08, P1-08, P1-09 | L | V-P5-01, V-P5-22, V-P5-26, V-P5-31, V-P5-32, V-P5-36 |
| P5-02 | cron/timezone planner | normative five-field grammar/ranges/DOM-DOW OR, IANA timezone, next-10 preview, DST | P0-08 | M | V-P5-02~05, V-P5-29 |
| P5-03 | durable Run/lease | occurrence-key unique materialization, version snapshot, claim, attempts, recovery | P5-01, P5-02 | L | V-P5-06~08, V-P5-24, V-P5-33, V-P5-35 |
| P5-04 | execution policy | principal/Role/Channel/Approval re-checked on every Run | P5-03, P1-08 | M | V-P5-15~18, V-P5-30 |
| P5-05 | concurrency/missed run | FORBID/ALLOW/REPLACE, SKIP/latest-one RUN_ONCE/window-limit oldest-first BACKFILL_LIMITED | P5-03 | M | V-P5-09~14 |
| P5-06 | retry/timeout/Run cancel | 3-attempt default, 10-second ack/60-second cleanup, REPLACE timeout skip, terminal retry as new linked Run | P5-03 | M | V-P5-11, V-P5-19, V-P5-20, V-P5-34 |
| P5-07 | channel notification | Mattermost start/result/failure and Bridge policy, §7G rules connected | P5-03, P2-11 | S | V-P5-23 |
| P5-08 | Schedule Admin UI | builder, preview, Run now, history, pause/resume/disable, individual Run cancel/retry | P5-01 | M | V-P5-21, V-P5-22 |
| P5-09 | metrics/alerts | lag, duplicates prevented, failures, stuck leases | P5-03 | S | V-P5-25, V-P5-27 |
| P5-10 | budget/latency targets | per-Run usage_records aggregation, per-Run/daily cost_units budget enforcement (§7C); start delay p95 ≤ 60 s under normal load (100 active Schedules, ≤ 20 due/min, 2 runners, DB CPU < 70%) with alerts | P1-14, P5-03 | M | V-P5-27, V-P5-28, V-P5-37 |

### Independent verification

The Scheduler Verifier verifies cron preview, timezone/DST, server restart, dual schedulers, concurrency, missed runs, permission revocation, secret leases, retry/timeout, and channel notices with `V-P5-*`.

### Exit Gate

- Zero duplicate scheduled Tasks/Runs for the same occurrence key; manual/retry Runs are explicitly linked to their originals.
- After server stop/restart, behavior matches the selected missed-run policy exactly.
- Scheduled work of a revoked execution principal does not run.
- Schedule Runs connect to the normal Task, verification, Artifact, and `DRAFT_PRE_VERIFICATION` document Core. Final document publishing UX is completed in Phase 6.
- Under normal load the Run start delay p95 is ≤ 60 s and budget policy is enforced.

## 19. Phase 6 — Collaboration and Documentation

### Work packages

| ID | Work | Completion criteria | Prereq | Size | Tests |
|---|---|---|---|---|---|
| P6-01 | Approval collaboration UX | Mattermost card-button request/decision (LOW/MEDIUM) on the Phase 1 Core, web guidance for HIGH, Schedule/Run scopes, interactive actions | P1-08, P2-12, P4-14 | M | V-P6-01, V-P6-02, V-P6-22, V-P6-29 |
| P6-02 | Brainstorm turn engine | §7F start command, round-robin `brainstorm_turn` work items, per-Agent/consecutive/total turn, budget, and time limits, PAUSED/resume, card | P1-12, P2-10, P2-11 | L | V-P6-03, V-P6-26 |
| P6-03 | Artifact extension | safe upload/ClamAV quarantine/provenance UX on the Phase 1 Core plus Brainstorm/Decision ArtifactLink handlers | P1-09, P0-04 | M | V-P6-05, V-P6-06, V-P6-25 |
| P6-04 | Document finalizer | canonical template, source freeze, verification results reflected in a new FINALIZED version | P1-10 | M | V-P6-07~09, V-P6-12, V-P6-19, V-P6-20, V-P6-23, V-P6-24 |
| P6-05 | redaction/provenance | secret/PII scan, IDs/links/checksums | P6-04 | M | V-P6-10, V-P6-13, V-P6-14 |
| P6-06 | Publisher | filesystem/NAS + Git-compatible publisher, one reference connector for BookStack or Wiki.js | P6-04 | M | V-P6-15, V-P6-16, V-P6-21 |
| P6-07 | publish review | Verifier/Human approval, versions/archive | P6-06 | S | V-P6-17, V-P6-18 |
| P6-08 | recurring summaries | per-Schedule period documents of Run results with provenance | P6-04, P5-03 | S | V-P6-09 |
| P6-09 | Brainstorm summary/decision/taskify | §7F summarizer selection (non-participant preferred), facilitator approval, decide/vote, taskify Decision↔Task provenance with mandatory criteria | P6-02, P1-11 | M | V-P6-04, V-P6-08, V-P6-27 |
| P6-10 | Documentation narrative layer | §10.4 layer 2, Documentation Agent selection, citation linter, skeleton immutability, usage recording | P6-04, P1-14 | M | V-P6-11, V-P6-28 |

### Independent verification

The Workflow/Docs Verifier closes Work, Brainstorm, and Schedule scenarios and verifies with `V-P6-*` mandatory document sections, source completeness, factual consistency, shortcomings, resource lists, secret redaction, checksums, republish/versioning, plus the Brainstorm turn engine, summary/decision/taskify, narrative citations, and Mattermost approval buttons.

### Exit Gate

- Task/Brainstorm/Schedule result documents are drafted automatically and published after independent review.
- Source Event/Artifact/Decision/Verification/ScheduleRun provenance is 100% linked.
- No secret/restricted information leaks to publishers.

## 20. Phase 7 — Release Hardening

### Work packages

| ID | Work | Completion criteria | Prereq | Size | Tests |
|---|---|---|---|---|---|
| P7-01 | CI/CD | lint/type/unit/integration/e2e/scan/SBOM/image | P0-01 | M | V-P7-11, V-P7-15 |
| P7-02 | observability | metrics/logs/alerts/runbook links | P4-02 | M | V-P7-14 |
| P7-03 | backup/restore | full-scope consistent backup, key tombstone reconciliation, default RPO 24 h/RTO 4 h, retention applied | P4-11, P5-03 | L | V-P7-07, V-P7-08, V-P7-19, V-P7-20 |
| P7-04 | load/soak | 3× the §21 baseline load for 30 minutes + normal load for 24 h, zero Event/Run loss | P5-10 | M | V-P7-03, V-P7-04 |
| P7-05 | security hardening | threat controls, dependency/container scans | P7-01 | M | V-P7-05, V-P7-06, V-P7-11, V-P7-12 |
| P7-06 | upgrade/rollback | staging rehearsal and forward-fix DB strategy | P7-03 | M | V-P7-09, V-P7-10 |
| P7-07 | release package | immutable digests, changelog, operations docs, final development report (§27A) | P7-01 | S | V-P7-01, V-P7-15~18 |
| P7-08 | runbooks | 7 runbooks: secret leak/NAS full/Bridge loop/Scheduler storm/DB restore/credential rotation/hard-delete restore; every critical alert links to a runbook | P7-02 | M | V-P7-13, V-P7-21 |
| P7-09 | Human-path acceptance automation | Task creation→delegation→approval→verification→document viewing using Mattermost only, automated with Playwright/Mattermost API | P2-11, P6-01, P6-04 | M | V-P7-02, V-P7-22 |

### Independent verification

The Release Verifier is a different Agent and cross-reviews the Security/Ops verification results. After Phase 7 PASSES, the implementer delivers the final development report and asks the user for deployment approval (§12.2, §27A).

### Exit Gate

- [[agent-colab-validation-plan_en-v8#16. Final Acceptance Criteria]] all PASS.
- Clean install, upgrade, backup restore, and incident tabletop succeed.
- Zero high/critical security findings; unresolved medium findings have owners and deadlines.

## 21. Test and Quality Strategy

- Unit: state machines, policy matrix, role conflicts, renderer, document templates.
- Property: Event idempotency/sequence, Bridge loop invariants, grant TTL.
- Integration: real PostgreSQL, Mattermost/Telegram sandboxes, Secret provider.
- Contract: OpenAPI, JSON Schema, MCP, Agent Adapter, Publisher.
- Conformance: the same suite for every Agent Adapter.
- UI: Playwright for Setup/Admin critical paths and accessibility.
- E2E: Mattermost→Agent→Approval→Artifact→Document→Verifier.
- Security: authz negative matrix, callback replay, injection, DLP canaries.
- Recovery: projection rebuild, outbox replay, backup restore.
- Soak: bridge, heartbeat, scheduler, lease cleanup.
- Scheduling: cron/DST property fixtures, dual-runner, restart/missed-run, authorization-at-run.
- Time-dependent tests use an injectable `Clock` and fixed tzdb fixtures. Retention is verified with a virtual clock/accelerated scheduler; real month-long waits are forbidden.
- Limits/Budget: rejection beyond Agent limits and Schedule budgets, start delay p95 measurement.

### 21.1 Deterministic Verification Defaults

Phase 0 ADRs may tighten these; loosening requires System Owner and independent Verifier approval and is not done during an autonomous run.

| Item | Default / PASS criterion |
|---|---|
| Normal functional load | Human Accounts 50, Agents 20, Channels 100, Bridges 20, API writes 20 rps, messages 10 rps, active Schedules 100, due ≤ 20/min |
| Peak load | 3× normal functional load sustained 30 minutes; error < 1%, zero Event/Run loss or duplicates |
| API | excluding health, write/read p95 ≤ 500 ms/300 ms, 5xx < 1% |
| Agent heartbeat | 30-second interval, offline after 90 s without heartbeat, back within 30 s of return |
| Adapter cancel | ack within 10 s, cleanup within 60 s |
| Secret revoke | new resolves rejected immediately, existing leases/handles invalidated within 5 s |
| Dashboard fault reflection | status and alert consistent within 60 s of probe failure |
| Schedule retry | max 3 attempts, 1/5/25 s + 0–20% jitter; permanent errors fail immediately |
| Schedule REPLACE | existing cancel/cleanup confirmed within 60 s, otherwise the new Run is skipped |
| Backup | default RPO 24 hours, RTO 4 hours |
| Accessibility | WCAG 2.1 AA automated violations 0, keyboard critical flows 100%, manual blockers 0 |
| DLP scope | isolated raw fixtures excluded; zero canaries in normalized storage, Events, logs, outputs, destinations, Documents, and backup plaintext |
| Task closure | rejected unless both the latest VerificationRun PASSED and a FINALIZED Document exist |
| Work item | ack 60 s, 3 redeliveries; task_assignment accept 120 s then one re-route then WAITING |
| Verifier accept | reassignment if not accepted within 10 minutes |
| Approval expiry | default 24 hours, reminder at 50%, Administrator escalation on expiry |
| Renderer | progress coalesced per Task at 10 s, bodies over 16k characters linked as Artifacts |
| MCP long-poll | `work_poll` waits at most 30 s, un-acked items redelivered on reconnect |
| cost_units | integer, 1 credit = 1,000,000 cost_units; unknown models use the pricing.yaml default rate and are marked `estimated` |
| Message retention | default 365 days, suspended under legal hold |
| Brainstorm default limits | 5 turns per Agent, 1 consecutive same-Agent turn, 40 total turns per session |
| link challenge | code TTL 10 minutes, single-use, 15-minute lockout after 5 failures |

## 22. CI/CD

PR gate:

1. Backend/frontend lint, formatting, type checks
2. Unit/property tests and coverage
3. Schema/API/adapter/publisher contract tests
4. Disposable PostgreSQL integration/migration/rebuild
5. Policy/authz/verification-independence negative tests
6. Secret scan, SAST, dependency/license audit
7. OCI build, container scan, SBOM
8. Verifier Agent review report

Protected branches and production deployment require approval by a reviewer/Verifier other than the implementer plus the required checks.

## 23. Deployment, Monitoring, and Backup

### 23.1 Environments

- dev: local/disposable dependencies
- staging: separate DB, Mattermost team/channels, Telegram test bot/chat, secret namespace
- production: independent credentials, storage prefix, backups, network policy

### 23.2 Key Metrics

- API latency/error, Event append/conflict
- Task/Approval/Verification states and wait times
- Agent heartbeat/capacity/conformance errors
- channel/bridge delivery/duplicate/loop/dead-letter
- secret lease/grant/reject/revoke; never over-expose values or IDs
- schedule due/claimed/running/succeeded/failed/skipped/lag/duplicate-prevented
- document draft/review/publish/failure
- backup last success, restore rehearsal, disk/DB usage

### 23.3 Backup

- PostgreSQL logical backup + checksum
- Mattermost DB/config/upload consistent backup
- Artifact/Document snapshots/versioning
- policy/config Git history
- per-provider encrypted secret backups with separate key custody
- key tombstone ledger/KMS separated from the normal restore set, reconciled after restore before the service opens
- quarterly empty-environment restore, monthly read/checksum test
- restore rehearsal verifies RPO 24 h/RTO 4 h, non-decryptability of destroyed keys, and ExternalIdentity/Approval/ScheduleVersion/Event hash equality together

## 24. Security Checklist

- [ ] Agent/Role/Capability are deny-by-default.
- [ ] No permission is granted automatically by Agent product name.
- [ ] The actor is determined from the credential identity.
- [ ] Unlinked or suspended Mattermost/Telegram identities cannot create command side effects.
- [ ] The implementing Agent cannot PASS its own result.
- [ ] Mattermost/Telegram callback replay and spoofing are blocked.
- [ ] Bridge echo, duplicates, and cross-channel delivery are blocked.
- [ ] The setup token is single-use and the endpoint locks after completion.
- [ ] The pre-DB bootstrap file holds only the token hash and non-secret pointers, has owner-only permission, and locks after DB migration.
- [ ] Setup binds to loopback by default; remote access never opens without prior HTTPS/TLS, client mTLS, allowlist, and a valid token.
- [ ] Admin critical actions require re-authentication and audit.
- [ ] Secret values are absent from DB metadata, Events, messages, logs, and Documents.
- [ ] Secret leases are bound to Agent/Task/action/TTL/single-use.
- [ ] LLM exposure requires explicit policy and Human Approval.
- [ ] Artifact/Document path, MIME, size, malware, and ACL are validated.
- [ ] PostgreSQL/admin/metrics/secret provider are not publicly exposed.
- [ ] Backup encryption keys are separated from runtime credentials.
- [ ] Hard-delete key tombstones block resurrection of destroyed keys after restore.
- [ ] Zero high/critical scan findings.
- [ ] Schedule templates use only allowed actions and cannot contain shell commands.
- [ ] Execution principal and Secret Grants are re-checked at Schedule Run start.
- [ ] No duplicate Runs for the same scheduled time even with dual schedulers and retries.
- [ ] Event hard delete destroys only the DEK and never changes Event bytes or hashes.
- [ ] Hash chains and UPDATE/DELETE defenses of Events, AuditEvents, and Verification revisions are verified.
- [ ] Human/Agent/service Roles are granted to the common Account principal.
- [ ] Schedule approval use-count consumption and Run claim/cancel transitions are atomic.

## 25. Dependencies and Prerequisites

Owners are roles. During an autonomous run the implementer first checks `.env`, the repository, and the environment for each dependency, and asks the user only for items that cannot be obtained or determined from them.

| Dependency | Needed by | Owner | Deadline | If missing |
|---|---|---|---|---|
| Deployment host/container runtime/inventory | Phase 0 | System Owner | Phase 0 start | Compose/capacity cannot be decided |
| Git repo and protected branch permissions | Phase 0 | System Owner | Phase 0 start | independent review/CI impossible |
| Phase 0 approved load profile and RPO/RTO | Phase 0 | System Owner | before Phase 0 ends | performance/recovery PASS cannot be judged |
| Initial pricing.yaml rate table | Phase 1 | System Owner | 1 week before Phase 1 | cost_units computation/budget verification impossible |
| Mattermost administrator and test team | Phase 2 | Mattermost administrator | 1 week before Phase 2 | channel integration impossible |
| Mattermost slash command registration and override_username permission | Phase 0 spike, Phase 2 | Mattermost administrator | P0-10 start | command/identity display method cannot be fixed |
| Telegram bot and test chat/topic | Phase 0 spike, Phase 2 | Operator | P0-13 start | Bridge E2E impossible |
| Mattermost/Telegram test users and Account link approver | Phase 2 | Operator | 1 week before Phase 2 | external identity authz cannot be verified |
| SMTP (optional) | Phase 2 | Operator | Phase 2 start | mail notifications unverified (optional) |
| Distinct verification Agent identity | every Phase | System Owner | each Phase start | Exit Gate PASS impossible |
| 3 Agent Adapter test endpoints | Phase 3 | Implementer Lead | 1 week before Phase 3 | genericity cannot be proven |
| System Owner/OIDC/MFA decision | Phase 4 | System Owner | 1 week before Phase 4 | setup/admin security cannot be fixed |
| Secret master key custody/provider | Phase 4 | System Owner | 1 week before Phase 4 | Secret feature cannot be released |
| Sidecar execution host (Agent host) | Phase 4 | Operator | P4-12 start | sidecar cannot be verified |
| KMS/key tombstone custody separated from normal backups | Phase 4–7 | System Owner | Phase 4 start | hard delete/restore cannot be accepted |
| IANA timezone DB and cron parser fixtures | Phase 5 | Implementer Lead | Phase 5 start | timezone/DST cannot be verified |
| Scheduler multi-instance staging | Phase 5 | Operator | 1 week before Phase 5 | duplicate execution cannot be verified |
| NAS/object/Git publisher destination | Phase 6 | Operator | 1 week before Phase 6 | document preservation E2E impossible |
| At least one Agent with `brainstorm.summarize`/`document.narrate` capability | Phase 6 | System Owner | 1 week before Phase 6 | summary/narrative layer unverified (limited skeleton-only acceptance) |
| ClamAV (or equivalent scanner) image | Phase 0 Compose, Phase 6 | Operator | Phase 0 start | malware quarantine cannot be verified |
| DNS/TLS/firewall permissions | Phase 4–7 | Operator | Phase 4 start | safe deployment impossible |
| Backup destination/recovery operator | Phase 7 | Operator | 1 week before Phase 7 | operational handover impossible |

## 25A. Risk–Work Mapping

Every risk in spec §18 is pinned to mitigating packages and judging Tests. New risks update this table and Appendix A together.

| Risk | Mitigating packages | Judging Tests |
|---|---|---|
| Feature differences between Agent products | P0-11, P3-03~05, P3-15 | V-P3-05, V-P3-12, V-P3-26 |
| Multi-Agent delegation storms, cycles, lost results | P3-09, P3-14 | V-P3-18~20, V-P3-25 |
| Permission confusion during Role changes | P1-03, P1-04 | V-P3-02 |
| Mattermost–Telegram echo/duplicates | P2-05, P2-06 | V-P2-04, V-P2-07 |
| Weak context/permissions on Telegram | P0-13, P2-08, P2-13 | V-P2-16, V-P2-20~22 |
| Web console privilege escalation | P4-08, P4-14 | V-P4-08, V-P4-09, V-P4-33 |
| Initial Setup takeover | P0-09, P4-03 | V-P4-02, V-P4-27, V-P4-28 |
| Secrets leaking into LLM context | P4-06, P4-07, P4-12 | V-P4-14, V-P4-15, V-P4-31 |
| Factual errors in automatic documents | P1-10, P6-05, P6-10 | V-P6-10, V-P6-28 |
| Bias/collusion of the verifying Agent | P1-06, P3-13 | V-P1-12, V-P1-24, V-P3-24 |
| Dependence on external document systems | P6-06 | V-P6-15, V-P6-16, V-P6-21 |
| Duplicate Scheduler execution | P5-03 | V-P5-06~08 |
| DST/timezone malfunction | P5-02 | V-P5-02~05, V-P5-29 |
| Missed-run storms | P5-05 | V-P5-12~14 |
| Execution with stale permissions/secrets | P5-04 | V-P5-15~18 |
| Overlapping scheduled work/cost explosion | P5-06, P5-10, P1-14 | V-P5-09~11, V-P5-28, V-P5-37 |
| Deleted data resurrected from backups | P4-11, P7-03 | V-P4-29, V-P7-20 |
| Ad hoc design of the product surface (commands, delivery, approvers) | P0-10~12, P2-10~12 | V-P0-16~18, V-P2-24~26 |
| Budget rendered ineffective by Adapters not reporting usage | P1-14, P3-15 | V-P1-30, V-P3-26 |

## 26. Definition of Done

Work becomes `IMPLEMENTED` only when all of the following hold.

- code/schema/migrations/docs updated together
- normal, rejection, and failure tests implemented
- test evidence and change manifest submitted as immutable references
- secrets and external side effects declared
- known limitations and rollback recorded

A Phase is complete when a different Verifier Agent executes all criteria and `PASS`es with attached evidence.

## 27. Next 10 Actions to Start Immediately

- [ ] 1. Register the three v8 documents as the protected baseline and generate the requirement/Test ID mapping.
- [ ] 2. Confirm the deployment, Mattermost, Telegram, storage, and secret environment inventory.
- [ ] 3. Set up the repo/CI/protected branches and the implementer–verifier separation rules.
- [ ] 4. Write the Agent Adapter, Role, Capability, and VerificationRun schemas with contract tests.
- [ ] 5. Write the Channel–TelegramBridge mapping/dedupe/loop contract.
- [ ] 6. Adopt the pre-DB sealed Setup state and Secret threat model/Provider ADRs.
- [ ] 7. Write the Schedule/Run schema, cron/timezone preview, and DST/concurrency/missed/retry policies.
- [ ] 8. Write the canonical Document template/manifest/Publisher contract.
- [ ] 9. Assign the Phase 0 implementing Agent and a different Architecture Verifier and execute P0-01~P0-14/V-P0.
- [ ] 10. Start Phase 1 automatically after the PASS report.

## 27A. Final Development Report and Deployment Approval

When Phase 7 has PASSED, the implementer produces `REPORT.md` in the repository root and delivers it to the user. It contains:

- Phase-by-phase summary: packages implemented, sizes, Verifier results with links to every Verifier Report, commit SHAs and tags.
- Final acceptance status against validation plan §16, item by item, with evidence references.
- Residual risks, open Low findings with owners and deadlines, and known limitations.
- Release artifacts: image digests, SBOM references, changelog.
- Deployment plan: target read from `.env` (host/method only; no secret values), steps to be executed, post-deploy checks, rollback procedure.

The source is pushed to GitHub before the report is delivered. The implementer then asks the user a single question — whether to proceed with deployment to the target in `.env` — and performs deployment only on explicit approval.
