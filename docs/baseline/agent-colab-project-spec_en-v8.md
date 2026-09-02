# Agent-Colab Product Specification v8 (EN)

> Document version: 8.0  
> Product name: **Agent-Colab**  
> Document role: baseline of product and operational requirements  
> Development plan: [[agent-colab-development-plan_en-v8]]  
> Verification baseline: [[agent-colab-validation-plan_en-v8]]  
> Supersedes: this document replaces specification versions v1–v7. It is the English canonical text; the Korean v7 is the last Korean edition.

## 1. Purpose

This document defines the vision, product scope, user experience, system structure, and operating principles of Agent-Colab. Implementation follows [[agent-colab-development-plan_en-v8]]; independent verification and the release decision follow [[agent-colab-validation-plan_en-v8]].

## 2. v8 Change Principles

1. The official product name is **Agent-Colab**, with this capitalization and hyphen.
2. Development is split into phases with deliverables and Exit Gates, and a verification Agent different from the implementation Agent verifies each result.
3. No role is fixed to a specific Agent product or machine; every Agent can be registered, modified, and deactivated dynamically.
4. Mattermost is the first and default conversation channel.
5. Each Mattermost channel can independently connect to one or more Telegram chats.
6. A web-based admin console manages servers, Agents, users, channel Bridges, policies, and settings.
7. First start uses a Setup Wizard for DB, security, Mattermost, storage, etc.; afterwards the admin console changes them.
8. A Secret Broker separated from public conversation delivers secrets to Agents.
9. When a task or discussion ends, purpose, process, results, limitations, resources, and evidence are preserved in one document structure.
10. Recurring work is registered as a Schedule with cron expression, timezone, permissions, duplicate/missed/concurrent execution policies, and run history.
11. Every mandatory requirement is registered with a Requirement ID (REQ-*) in Appendix A and requirement–implementation–verification traceability is maintained.
12. The minimum Core of Approval, Artifact, and Documentation is implemented before Scheduled Work to remove circular dependencies between phases.
13. Event rows and stored hashes are never modified. Sensitive content is deleted by separate ciphertext storage and key destruction (crypto-shredding).
14. Result documents distinguish the pre-verification draft from the post-verification final version, removing the loop of writing verification results retroactively.
15. cron grammar, missed-run, cancellation, and time computation follow this document as the norm so that implementations do not diverge.
16. The development plan serializes every state change as generic aggregate Events; projections are never the authority for permissions or approval consumption.
17. A user who executes commands from an external channel must be connected to an Account through a verified External Identity Link.
18. Hard delete is complete only when a destroyed key cannot be resurrected after a backup restore, not just on the live system.
19. Ambiguous PASS criteria are replaced by deterministic criteria with numeric, state, or time limits.
20. The product surface — Mattermost commands and cards, Agent work delivery, approvers, Task acceptance criteria and Verifier assignment, Brainstorm progression, document narrative, cost units — is fixed by Phase 0 contracts and is never designed ad hoc by an implementing Agent.
21. **Autonomous execution (v8):** development runs end to end without human phase gates. Each phase is completed by an automated independent Verifier PASS; the only human decision in the pipeline is deployment approval after the final development report.

Specific Agent names, predefined Coordinator/Worker roles, and dedicated development-server roles were removed in v1 and remain absent in v8. Agents participate through Agent Adapters and Capabilities regardless of product.

## 3. Vision and Problem Definition

### 3.1 Vision

Agent-Colab is a self-hosted collaboration operations platform where humans and diverse AI Agents deliberate in Mattermost channels, divide work, approve, verify results, and can reconstruct every process and deliverable.

### 3.2 Problems to Solve

- Differences in product and protocol make it hard to bring Agents into one collaboration space.
- Delegations and results scattered across chat make status and responsibility unclear.
- Relying on prompts alone for Agent roles and permissions cannot prevent bypass and misbehavior.
- Connecting auxiliary channels such as Telegram creates duplicate, loop, and disclosure risks.
- Passing tokens and accounts to Agents can expose them in public channels or logs.
- After tasks and discussions end, decision rationale, limitations, and resource usage are not kept as one integrated document.
- System configuration and Agent registration that depend on manual file editing are hard to operate.
- When the same Agent both implements and verifies, errors are easily missed.

### 3.3 Product Principles

1. **Mattermost first**: the default entry point of all official conversation is Mattermost.
2. **Generic Agent**: Agents are handled by identity, adapter, role, and capability, not by product name.
3. **Policy over prompt**: the server enforces permissions; prompts only explain usage.
4. **Conversation is not state**: conversation is context; state is the Event projection.
5. **Independent verification**: a phase is not complete when implementer and verifier identities are the same.
6. **Secret is not message**: secrets are never placed in plaintext in chat, Event payloads, or Artifacts.
7. **Channel isolation**: context, Bridges, Agent participation, and permissions are separated per channel.
8. **Replayability**: Task and Approval state must be rebuildable from Events.
9. **Documented outcome**: finished work and discussions remain as structured knowledge documents.
10. **Operable by web**: routine administration is performed in the web admin console.
11. **One principal model**: Humans, Agents, and services all use an Account as the permission principal.
12. **Immutable event bytes**: stored Event bodies and hashes are never modified or deleted.

## 4. Users and Roles

### 4.1 System User Types

| Type | Description | Representative permissions |
|---|---|---|
| System Owner | Responsible for initial setup and top-level policy | setup, administrator designation, break-glass |
| Administrator | Manages servers, accounts, Agents, integrations, policies | entire web admin console |
| Operator | Status checks, restarts, backup/restore, incident response | operational functions and limited settings |
| Human Member | Channel participation, Task creation, review and approval | granted workspace/channel permissions |
| Agent Account | AI/automation principal participating through an Adapter | actions allowed by Role/Capability |
| Auditor/Verifier | Read-only review and phase verification | evidence access, verification verdicts |

### 4.2 Dynamic Agent Model

Agents are not hard-coded by product name. Registration specifies:

- Identity: unique Agent ID, display name, status, owner
- Adapter Type: MCP client, REST webhook, Mattermost bot (the three mandatory types in v8). local process and remote gateway keep adapter-contract compatibility but are out of scope for v8
- Roles: a set of user-defined roles
- Capabilities: executable tools, domains, resources, side effects
- Channel Membership: Mattermost channels to join and speak/read permissions
- Model/Runtime Metadata: product, model, version, host information (optional)
- Credential Reference: credential reference in the Secret Broker
- Limits: concurrent Tasks, rate, turns, cost (cost_units)/time limits. The cost unit is the integer `cost_units` (1 credit = 1,000,000) and Adapters report usage with every result
- Lifecycle: pending, active, suspended, revoked, offline

Agent roles can be created at registration or selected from existing roles, and later modified in the web admin console or through the management API for authorized Agents. Role changes are recorded as versioned policy and Audit Events. Tasks in flight keep their existing policy snapshot by default; security revocations that must apply immediately are handled by a separate revoke Event.

When several Agents pursue one goal, Tasks form an acyclic task graph with `root_task_id` and `parent_task_id`. An authorized Agent may create sub-Tasks or delegate to other Agents only within the scope of its own Task. Channel policy limits maximum delegation depth, maximum fan-out, concurrent sub-Tasks, and join conditions (`ALL`, `ANY`, `QUORUM`). Each sub-Task has independent state, budget, Artifacts, and Verification; a parent Task cannot complete before its join condition and required sub-Task verifications are satisfied. Delegator, assignee, policy snapshot, causing Event, and reassignment history are preserved, and cyclic delegation is rejected.

### 4.3 User-Defined Roles

A Role is a policy object, not a name.

```yaml
role_id: role-research-reviewer
display_name: Research Reviewer
permissions:
  - task.read
  - artifact.read
  - verification.submit
constraints:
  domains: [research]
  side_effects: deny
  requires_human_approval: [external_send]
```

One Account can hold several Roles. Humans, Agents, and services all use the Account as the permission principal, and an Agent inherits Roles through its linked `account_id`. Permission conflicts are resolved in the order `explicit deny > scope restriction > allow`.

### 4.4 Break-glass Procedure

The System Owner's break-glass is reserved for emergencies in which the normal administration path cannot be used.

- Activation requires the recovery code and MFA re-authentication and must state scope and reason.
- The session is time-limited (default 60 minutes) and ends automatically on expiry.
- Activation, every action during the session, and termination are recorded as AuditEvents and announced immediately in the ops channel.
- After termination a post-hoc verification Task by an independent Verifier is created automatically to review justification and actions.
- Even under break-glass, destroying Event immutability and reading Secret values in plaintext are not allowed.

## 5. Key Usage Scenarios

### 5.1 Agent Registration and Role Change

1. An administrator selects an Agent Adapter in the web console.
2. Connection endpoint and Secret reference are configured.
3. Role, Capability, Channel membership, and limits are granted.
4. When the connection test and least-privilege check pass, the Agent is activated.
5. The administrator can later modify the Role or suspend/revoke the Agent.

### 5.2 Mattermost Channel Work

1. A Human creates a Task with acceptance criteria in a Mattermost channel using the `/colab` command.
2. The Colab Server finds candidate assignees from channel policy and Agent Capability.
3. The assigned Agent delegates sub-Tasks to other Agents in parallel within the allowed depth and fan-out; the server prevents cycles and evaluates join conditions.
4. Selection, delegation, reassignment, progress, approval, and result Events are stored and displayed in the channel.
5. When the Task ends, the Documentation Service generates a closing document that includes sub-Tasks and the Agents used.
6. An independent Verifier (Agent or Human), automatically assigned by the server on eligibility, independence, capability, and load, verifies evidence and completion conditions.

### 5.3 Per-Channel Telegram Connection

1. An administrator adds a Telegram Bridge in the Mattermost channel settings.
2. The Telegram bot secret and chat/thread ID are linked through a Secret Broker reference.
3. Direction, message type, mention, attachment, and redaction policies are selected.
4. The Bridge stores source mapping and deduplication keys.
5. Messages created on one side are delivered to the other according to policy, and echo loops are blocked.

### 5.4 Work That Uses Secrets

1. A Human/administrator registers the secret in the Secret Store.
2. A Secret Grant is created for the Agent and action scope the Task needs.
3. The Agent obtains a short-lived lease through the authenticated Secret Broker API, not through public messages.
4. Access time, Agent, Task, secret version, and result are audited without recording the value.
5. Leases are revoked on expiry, Task end, or Agent revocation.

### 5.5 Closing a Discussion and Documenting Knowledge

1. A channel or Brainstorm session is closed.
2. The Documentation Service collects conversation, Events, Decisions, and Artifact metadata.
3. It composes purpose, participants, process, key arguments, decisions, results, limitations, open items, resources used, and provenance.
4. A Human or Verifier reviews factual accuracy and secret leakage.
5. The canonical Markdown is stored and published through the selected Publisher.

### 5.6 Scheduling Recurring Work

1. An authorized Human or Agent creates a Schedule in the web admin console/API.
2. cron expression, IANA timezone, target Mattermost channel, Task template, and Agent selection policy are specified.
3. The system previews the next run times and checks permissions, approvals, and secret references.
4. The Scheduler creates a unique Run at the scheduled time and hands it to the normal Task/Event flow.
5. Start, success, failure, and delay are shown in the target Mattermost channel, and the channel's Telegram Bridge policy applies.
6. Run history, result Artifacts, verification, and result documents are linked to the Schedule.
7. Administrators can pause, resume, modify, or disable a Schedule, and, when authorized, execute `Run now` or cancel an individual pending/running Run. Finished Runs cannot be cancelled.

A Schedule is not a cron that runs operating-system shell commands. It invokes only allowed Agent-Colab actions or Task templates, and every execution passes the current Policy Engine and Approval rules again.

## 6. Product Scope

### 6.1 Included in v8

- Mattermost-based Work/Brainstorm/Approval/Ops/custom channels
- Multiple Telegram Bridges per channel with bidirectional/unidirectional policies
- Generic Agent Registry, Adapter, Role, Capability, lifecycle
- Task/Event/Conversation/Message/Brainstorm/Decision/Approval/Artifact model
- Agent-to-Agent delegation, Human approval, independent verification
- Web admin console and operations API
- Initial Setup Wizard and reconfiguration
- Secret Store integration, Secret Grant/Lease/Audit
- Documentation Service, canonical Markdown archive, Publisher Adapter
- PostgreSQL Event Store, projections, REST/MCP/SSE
- health, metrics, audit, backup/restore
- cron-based Schedule CRUD, versions, run/pause/resume/disable/Run now/individual Run cancel/retry/history/notification
- timezone/DST, missed run, concurrency, retry, timeout, idempotency policies
- break-glass emergency access procedure and hard-delete administrator workflow
- Verified identity link between Mattermost/Telegram external users and Accounts
- TOTP MFA for System Owner/Administrator (mandatory), policy-based MFA for Human Members, OIDC adapter interface (optional)
- Server-enforced Agent Limits and Schedule budgets

### 6.2 Excluded from v8

- Automatic installation/updates of arbitrary Agent products
- Universal federation for every messenger
- Unrestricted automatic insertion of secret values into LLM context
- Kubernetes and large message brokers
- Full replacement of enterprise IAM/PKI products
- Vector-DB-based enterprise knowledge search
- In-house development of a complex WYSIWYG document editor
- A general system-cron replacement that registers arbitrary root shell commands
- Very-high-frequency (bulk per-minute) workflow/message-broker platforms

## 7. Logical System Structure

```text
Human / Agent
      │
      ▼
Mattermost Channels ◀────▶ Channel Telegram Bridges
      │                           │
      └────────────┬──────────────┘
                   ▼
┌──────────────── Agent-Colab Server ────────────────┐
│ Channel Gateway / Renderer / Command Router         │
│ Task & Conversation Services / Brainstorm / Approval│
│ Agent Registry / Adapter Runtime / Role & Policy    │
│ Secret Broker / Artifact Registry / Documentation   │
│ Setup Service / Admin API / Web Admin Console       │
│ Schedule Service / Durable Scheduler / Run History  │
│ Event Store / Projection / Outbox / Audit           │
└────────┬──────────┬───────────┬───────────┬─────────┘
         │          │           │           │
    PostgreSQL  Secret Store  Object/NAS  Document Publishers
                                  │        Git/Gitea/BookStack/
                                  │        Wiki.js adapters
                                  ▼
                         Registered Agent Adapters
```

## 8. Channels and Collaboration Flows

### 8.1 Mattermost Channel Model

A channel is a collaboration boundary, not just UI. Each channel has:

- channel purpose/type: work, brainstorm, approval, ops, custom
- member/Agent membership with read/write/moderate permissions
- default Role/Task domain/risk policy
- 0..N Telegram Bridges
- retention, documentation template, archive destination
- Agent turn/delegation/rate limits
- allowed Secret scope and Artifact policy

Four default channel templates are provided (work, brainstorm, approval, ops); custom-type channels are created without a template. Administrators can add, modify, and delete templates. Channel deletion is a soft delete after archive and mapping checks.

### 8.2 Work Flow

`REQUEST → TASK_CREATED → ASSIGNEE_SELECTED → TASK_DELEGATED → ACCEPTED → RUNNING → PROGRESS → APPROVAL? → RESULT → ARTIFACT → DOCUMENT_DRAFTED → INDEPENDENT_VERIFICATION → PASSED → DOCUMENT_FINALIZED → COMPLETED`

The default Task state path is `OPEN → DELEGATED → ACCEPTED → RUNNING ↔ WAITING → IMPLEMENTED → VERIFYING → VERIFIED → COMPLETED`. Cancellation before execution is `CANCELLED`; cancellation during execution is `CANCEL_REQUESTED → CANCELLED`; `COMPLETED|CANCELLED` are terminal. A `FAILED` Verification returns the Task to `RUNNING`; a `BLOCKED` Verification caused by external conditions returns it to `WAITING`. A Task is `IMPLEMENTED` before verification and becomes `VERIFIED`/`COMPLETED` only when a Verifier passes it. A `FAILED` or `BLOCKED` verification still finalizes that attempt's document version but does not complete the Task. The implementing Agent cannot submit the final verdict on its own Task.

### 8.3 Brainstorm Flow

`OPEN → IDEA/CHALLENGE/QUESTION/GUIDANCE → SUMMARY → DECISION → ACTION_ITEMS → DOCUMENT_DRAFTED → TASKIFY → VERIFIED → DOCUMENT_FINALIZED → CLOSED`

Free conversation comes first, with per-channel limits on turns, delegation depth, consecutive same-Agent responses, and cost/time. The session opener is the facilitator and the server distributes Agent turns. When a limit is exceeded the session becomes `PAUSED` and the facilitator decides to resume or close. The summary is written by a non-participant Agent where possible and approved by the facilitator; Decisions are recorded by the facilitator.

### 8.4 Approval Flow

`REQUESTED → PENDING → APPROVED → PARTIALLY_CONSUMED* → CONSUMED`

`REQUESTED | PENDING | APPROVED | PARTIALLY_CONSUMED → REJECTED | CANCELLED | EXPIRED | REVOKED`

An Approval is bound to `subject_type(task|schedule|run|action)` and `subject_id`, action, resource scope, executing principal, validity period, and maximum use count. A one-time Task/Run approval cannot be reused for another subject. Recurring Schedule approvals are, by policy, either obtained anew per Run or consumed atomically from a Schedule approval with limited validity and use count. Expired, cancelled, revoked, or exhausted Approvals are invalid on every execution path; a new Approval is requested instead of an extension.

An approver must satisfy the `approval.decide` permission, membership in the target channel, and a Role maximum risk at or above the action risk; the requester, the implementing Agent, and their aliases cannot approve. Risk HIGH and above and `requires_human_approval` actions are approved by Humans only, with a per-risk quorum (LOW 0, MEDIUM 1, HIGH 1, CRITICAL two different Humans). HIGH and above are decided in the web console after MFA re-authentication. Default expiry is 24 hours; on expiry the request escalates to an Administrator.

### 8.5 Verification Flow

`IMPLEMENTATION_SUBMITTED → VERIFIER_ASSIGNED → EVIDENCE_REVIEWED → TEST_EXECUTED → PASSED | FAILED | BLOCKED`

- Implementer and Verifier Agent IDs must differ.
- The server assigns the Verifier automatically on eligibility, independence, domain capability, and load; the acceptance criteria defined at Task creation are the verification baseline. If not accepted within 10 minutes, the assignment is reassigned.
- The same verifier may cover several specialties or phases, but may not be the same Account, Agent, service credential, or alias as the implementer of that verification scope. Stricter role separation required by policy takes precedence.
- The verifying Agent does not accept the conclusions of the original implementation conversation as given; it reads the baseline documents and evidence independently.
- A failure returns to the implementation stage with a fix request and reproduction steps.
- Security, recovery, and production-related phases additionally require specialist Verifiers. In v8 no human sign-off is required between phases; the human decision is limited to deployment approval (§19).

### 8.6 Schedule Flow

Schedule definition states: `DRAFT → ENABLED ↔ PAUSED → DISABLED`

ScheduleRun states: `PENDING → DUE → CLAIMED → TASK_CREATED → RUNNING → VERIFYING → SUCCEEDED | FAILED | SKIPPED | TIMED_OUT | CANCELLED`; cancellation during execution is `CANCEL_REQUESTED → CANCELLED`

- cron accepts only numeric five-field expressions (`minute hour day-of-month month day-of-week`). Each field supports `*`, comma lists, hyphen ranges, and `/` steps. Ranges are minute 0–59, hour 0–23, day-of-month 1–31, month 1–12, day-of-week 0–6 (Sunday = 0). Names, a seconds field, `? L W #`, aliases such as `@daily`, and day-of-week 7 are rejected.
- When both day-of-month and day-of-week are restricted, Vixie cron OR semantics apply. The default minimum interval is 5 minutes; operational settings may lower it but never below 1 minute.
- Timezones are stored as IANA identifiers and never depend on the server's local timezone.
- Non-existent DST local times are skipped; duplicated local times run once per wall-clock occurrence key. Preview and run history show the computation basis.
- Execution occurrences use an `occurrence_key` built from `schedule_id + timezone + local wall-clock minute`. The two UTC instants of a DST fall-back share the same key and run once. `scheduled_for` separately preserves the actual UTC instant.
- concurrency policy is `FORBID`, `ALLOW`, or `REPLACE`; default `FORBID`.
- missed run policy is `SKIP`, `RUN_ONCE`, or `BACKFILL_LIMITED`; default `RUN_ONCE`. `RUN_ONCE` creates only the most recent missed occurrence with its original `scheduled_for`. `BACKFILL_LIMITED` creates occurrences within `backfill_window`, oldest first, up to `backfill_limit`.
- Policy/Role/Agent/Secret Grant are re-checked at every Run start, not only at Schedule creation.
- DB lease/advisory locks prevent duplicate Scheduler execution.
- Schedule definitions are controlled with pause/resume/disable. A specific pending/running Run is stopped safely after a cancel request, recording `RUN_CANCEL_REQUESTED` and `RUN_CANCELLED`. Cancelling an already finished Run is a conflict error.
- A Run uses the immutable `schedule_version_id` and the action/documentation/budget snapshot from its creation time. At execution only the current Account/Role/Capability/Approval/Secret/Channel policy is re-evaluated; live Schedule settings never overwrite an existing Run snapshot.
- Transient retries are recorded as Attempts of one Run and end before the terminal state. An administrator's re-execution of a terminal Run creates a new Run with `retry_of_run_id` without modifying the original.

### 8.7 Mattermost Interaction Principles

- Commands are executed only through the `/colab <resource> <verb>` grammar (or an `@colab` mention). Free text without the prefix is never interpreted as a command.
- A Task and a Brainstorm are each bound to one root post in the channel (the "card") and its thread. The card is edited in place to show current state, and every transition leaves a thread reply as an immutable log.
- Card buttons (Accept, Submit, Approve/Reject, Verify, Cancel) are conveniences; the server performs the permission check at callback time.
- Agent utterances are posted by the server under the Agent's display name; an Agent cannot specify its displayed identity.
- Every piece of work given to an Agent is delivered as a durable work item; chat messages are never the only delivery path. Agents explicitly report acceptance, rejection, and results.
- After linking their Mattermost user to an Account through a verified challenge, a Human Member must be able to complete Task creation, delegation, approval, verification, and document viewing using Mattermost alone, without the web console.
- Pending approvals, Verifier assignment, waiting Tasks, and budget overruns are delivered to the responsible people through rule-based notifications.

## 9. Data Model

### 9.1 Core Entities

| Entity | Key fields |
|---|---|
| Workspace | `workspace_id`, name, status, default_policy_version |
| Account | `account_id`, type(human/agent/service), status, auth_subject, profile |
| ExternalIdentityLink | `link_id`, provider_instance_id, external_user_id, account_id, verification_method, status, verified_at |
| Agent | `agent_id`, account_id, adapter_type, endpoint_ref, status, owner_account_id, runtime_metadata |
| Role | `role_id`, name, permissions, constraints, version, status |
| PrincipalRoleAssignment | `account_id`, `role_id`, scope, valid_from/to, assigned_by |
| Capability | `capability_id`, tool/domain/resource/side_effect, schema, limits |
| Channel | `channel_id`, mattermost_channel_id, type, policy, documentation_template, status |
| ChannelMember | channel_id, account_id, permissions, status |
| TelegramBridge | `bridge_id`, channel_id, telegram_chat/thread, direction, filters, secret_ref, status |
| Task | `task_id`, root_task_id, parent_task_id?, channel_id, title, domain, risk, delegated_by?, assignee, delegation_depth, join_policy, policy_snapshot, status, verification_status |
| AcceptanceCriteria | `criteria_id`, task_id, statement, check_type(evidence/test_command/artifact_hash/human_attest), required, revision |
| WorkItem | `work_item_id`, kind, agent_id, task_id?, brainstorm_id?, deadline, status, idempotency_key, secret_handles |
| UsageRecord | agent_id, task_id?, run_id?, brainstorm_id?, document_id?, work_item_id, model, tokens, tool_calls, wall_ms, cost_units, source, pricing_version |
| NotificationRule | `rule_id`, event_type, recipient_selector, channels, dedupe_window, quiet_hours |
| Event | `event_id`, workspace_id, aggregate_type, aggregate_id, aggregate_seq, task_id?, channel_id?, type, actor, caused_by, idempotency_scope/key, payload, sensitive_ciphertext/key_ref, previous_hash, content_hash, policy_version, ts |
| Conversation | `conversation_id`, channel_id, mode, source_thread mapping |
| Message | `message_id`, conversation_id, source, source_message_id, body, visibility, event_id? |
| Brainstorm | `brainstorm_id`, conversation_id, status, limits, summary_document_id? |
| Decision | `decision_id`, brainstorm_id?, statement, rationale, status, decided_by, source_event_id |
| Approval | `approval_id`, subject_type(task/schedule/run/action), subject_id, action, resource_scope, risk, status, valid_from, expires_at, max_uses?, used_count(ledger-derived), requested_by, decided_by |
| Artifact | `artifact_id`, creator, storage_uri, MIME, size, sha256, source_event_id; target links are separated into ArtifactLink |
| ArtifactLink | `artifact_id`, subject_type(task/schedule_run/brainstorm/decision), subject_id, relation, linked_by, linked_at |
| SecretMetadata | `secret_id`, provider, path/reference, classification, current_version, owner; value excluded |
| SecretGrant | `grant_id`, secret_id, grantee_agent_id, task_id, action_scope, expires_at, status |
| SecretAccessAudit | grant_id, agent_id, task_id, version, accessed_at, result; value excluded |
| Document | `document_id`, type, source_scope, version, status, storage_uri, sha256, publisher_refs |
| VerificationRun | `verification_id`, target_type/id, phase/task?, implementer_account/agent/credential_snapshot, verifier_account/agent/credential_snapshot, alias_graph_version, criteria_version, result, evidence |
| Schedule | `schedule_id`, name, status, current_version_id, next_run_at, created_by |
| ScheduleVersion | `schedule_version_id`, schedule_id, version, cron_expression, timezone, action_template, channel_id, execution_principal, agent_selection, concurrency/missed/retry/budget/documentation policy, backfill_limit/window, max_duration, starts_at/ends_at, snapshot_hash |
| ScheduleRun | `run_id`, schedule_id, schedule_version_id, run_kind, occurrence_key?, scheduled_for, local_scheduled_for?, retry_of_run_id?, claimed_by, lease_expires_at, task_id?, status, attempt_count, started/finished, cancel_requested_at?, cancelled_at?, result_event_id, error_code |
| SystemSetting | `setting_key`, encrypted/value_ref, scope, version, source, changed_by |
| AuditEvent | actor, action, target, result, correlation_id, ts, redacted_metadata |

### 9.2 Relationship Rules

- A Channel has zero or more TelegramBridges.
- An Account can hold several Roles, and an Agent obtains permissions through its Account.
- An external provider user executes commands with Account permissions only with an active, verified ExternalIdentityLink. Unlinked or suspended users can only read and receive guidance and cannot create Task/Event side effects.
- A Task can have one or more VerificationRuns and requires a final pass.
- Task parent/root relationships are allowed only within the same Workspace and cannot be cyclic. Parent completion requires the join condition and the latest PASSED Verification and FINALIZED Document of every required child.
- ArtifactLink activates Task in Phase 1, ScheduleRun in Phase 5, and Brainstorm/Decision in Phase 6, and validates existence and Workspace match per subject type.
- A Document has a Task, Brainstorm, Conversation, or period-based operations scope as its source.
- Secret values are never stored in the Event/Message/Document DB.
- A Schedule has several immutable ScheduleVersions and ScheduleRuns. `(schedule_id, occurrence_key)` is unique for scheduled runs; manual/retry Runs use a separate idempotency key.
- Modifying a Schedule creates a new version and never changes the snapshot of already created Runs.

### 9.3 Event Principles

Events are append-only, and every state aggregate is serialized by `(workspace_id, aggregate_type, aggregate_id, aggregate_seq)`. Task, Approval, Schedule, and Agent state are computed as Event projections, but command authorities that need concurrency safety, such as Approval use counts, are enforced by an authoritative ledger inside the append transaction. Stored Event bytes and hashes are never UPDATEd or DELETEd. Canonical JSON and envelope metadata/ciphertext are chained by SHA-256 hashes, and the versioned schemas in the JSON Schema registry are the final authority.

- Agent/Admin: `AGENT_REGISTERED`, `AGENT_UPDATED`, `AGENT_ACTIVATED`, `AGENT_SUSPENDED`, `AGENT_REVOKED`, `AGENT_HEARTBEAT_RECORDED`, `AGENT_MARKED_OFFLINE`, `PRINCIPAL_ROLE_ASSIGNED`, `PRINCIPAL_ROLE_REVOKED`, `ACCOUNT_SUSPENDED`, `SETTING_CHANGED`, `BREAK_GLASS_STARTED`, `BREAK_GLASS_ENDED`, `HARD_DELETE_REQUESTED`, `HARD_DELETE_APPROVED`, `HARD_DELETE_EXECUTED`
- Channel/Bridge: `CHANNEL_CONFIGURED`, `CHANNEL_ARCHIVED`, `TELEGRAM_BRIDGE_ENABLED`, `TELEGRAM_BRIDGE_DISABLED`, `BRIDGE_DELIVERY_FAILED`
- Work: `TASK_CREATED`, `SUBTASK_CREATED`, `TASK_DELEGATED`, `TASK_REASSIGNED`, `TASK_ACCEPTED`, `TASK_STARTED`, `TASK_WAITING`, `TASK_PROGRESS_REPORTED`, `TASK_JOIN_SATISFIED`, `TASK_CANCEL_REQUESTED`, `TASK_CANCELLED`, `IMPLEMENTATION_SUBMITTED`, `TASK_COMPLETED`
- Approval: `APPROVAL_REQUESTED`, `APPROVAL_GRANTED`, `APPROVAL_REJECTED`, `APPROVAL_CANCELLED`, `APPROVAL_EXPIRED`, `APPROVAL_ESCALATED`, `APPROVAL_REVOKED`, `APPROVAL_CONSUMED`
- Brainstorm/Decision: `BRAINSTORM_OPENED`, `IDEA_RECORDED`, `BRAINSTORM_PAUSED`, `BRAINSTORM_RESUMED`, `SUMMARY_RECORDED`, `DECISION_RECORDED`, `BRAINSTORM_CLOSED`
- Work delivery/Budget: `WORK_ITEM_QUEUED`, `WORK_ITEM_DELIVERED`, `WORK_ITEM_ACKED`, `WORK_ITEM_RESULT_RECEIVED`, `WORK_ITEM_EXPIRED`, `BUDGET_RESERVED`, `BUDGET_EXCEEDED`
- Identity/Notification: `IDENTITY_LINK_CHALLENGED`, `IDENTITY_LINK_VERIFIED`, `NOTIFICATION_SENT`
- Artifact: `ARTIFACT_REGISTERED`, `ARTIFACT_VERIFIED`, `ARTIFACT_QUARANTINED`
- Verification: `VERIFIER_ASSIGNED`, `VERIFICATION_PASSED`, `VERIFICATION_FAILED`, `VERIFICATION_BLOCKED`
- Secret: `SECRET_REGISTERED`, `SECRET_GRANT_CREATED`, `SECRET_ACCESSED`, `SECRET_GRANT_REVOKED`
- Documentation: `DOCUMENT_DRAFTED`, `DOCUMENT_FINALIZED`, `DOCUMENT_REVIEWED`, `DOCUMENT_PUBLISHED`
- Schedule: `SCHEDULE_CREATED`, `SCHEDULE_UPDATED`, `SCHEDULE_ENABLED`, `SCHEDULE_PAUSED`, `SCHEDULE_RESUMED`, `SCHEDULE_DISABLED`, `RUN_DUE`, `RUN_CLAIMED`, `RUN_STARTED`, `RUN_CANCEL_REQUESTED`, `RUN_CANCELLED`, `RUN_SUCCEEDED`, `RUN_FAILED`, `RUN_SKIPPED`, `RUN_TIMED_OUT`

## 10. Mattermost–Telegram Bridge

### 10.1 Unit of Configuration

The owning unit of a Telegram connection is the **Mattermost Channel**, not the Workspace. One channel can connect to several Telegram chats/threads; connecting one Telegram target to several Mattermost channels is forbidden by default to prevent context confusion. Exceptions are explicitly approved by an administrator.

### 10.2 Policies

- direction: Mattermost→Telegram, Telegram→Mattermost, bidirectional
- content: text, attachment, system event, approval notice, mention
- identity display: original sender and source shown
- redaction: secret patterns, private messages, restricted Artifacts blocked
- threading: Mattermost root/thread mapped to Telegram topic/reply
- dedupe: `(bridge_id, source_platform, source_message_id)` unique
- loop prevention: immutable origin marker and hop count
- retry: transactional outbox, exponential backoff, dead-letter
- rate limit and message size/attachment policy
- Bridge enable/disable/test and delivery status available in the web console

Telegram is an auxiliary channel; the Colab Server is the authority for Events/Tasks. Whether Task commands are allowed from Telegram is configured per channel; the default is read/reply only. When commands are allowed, the Telegram user must be connected to a pre-verified Account by provider instance and external user ID; unlinked, suspended, or duplicate links obtain no execution permission.

## 11. Web Administration and Operations

### 11.1 Dashboard

- Hub/DB/Mattermost/Telegram/Secret Store/NAS status
- Agent online/offline, heartbeat, version, errors
- active/queued/failed/waiting/verification Tasks
- approval backlog, outbox backlog, disk/DB/backup status
- API latency/error, policy denials, loop detection
- Agent/Channel/Schedule usage and cost_units budget consumption, work item inbox backlog

### 11.2 Management Functions

- Account create/invite/modify/suspend/deletion request/role assignment
- Agent registration, connection test, role/capability/channel settings, rotate, suspend, revoke
- Mattermost channel import/configuration and Telegram Bridge CRUD/test
- Policy, risk, approval, rate/turn limit changes with diff/rollback
- Secret metadata registration, grant/revoke, rotation status (values never re-displayed)
- Setup configuration view/change, dependency health test
- Task/Event/Approval/Artifact/Document/Verification browsing
- backup execution, restore rehearsal, maintenance mode
- audit search/export
- Approvals queue and notification rule management
- Schedule creation, cron/timezone preview, modification, pause/resume/disable, Run now, individual Run cancel, run history, retry

Deletion is soft delete/suspend by default for referential integrity and audit. When legal or retention policy requires hard delete, a dedicated administrator workflow is used: dual approval by different requester and approver, retention-policy and referential-integrity checks, a waiting period (default 72 hours), and after execution the identifying tombstone and AuditEvents are preserved. Event `payload` holds non-sensitive data only; sensitive content is stored as a separate object or envelope-encrypted ciphertext with a key reference. Hard delete destroys the per-target data-encryption key so decryption becomes impossible, and never modifies or removes Event rows, ciphertext, or stored hashes. Redaction applies only at projection, search, and display and never modifies stored Event bytes.

## 12. Initial Server Setup

On first start `/setup` binds to loopback only by default. Remote management access is allowed only when a pre-configured HTTPS/TLS reverse proxy, client mTLS, an IP allowlist, and the setup token are all satisfied. The setup token is generated by a CSPRNG with at least 256 bits, has a 30-minute TTL, is single-use, and after 5 failures within 15 minutes per IP and token fingerprint the source is blocked for 15 minutes. Before the DB is configured, a sealed local bootstrap file with owner-only permissions (default container path `/var/lib/agent-colab/bootstrap/state.json`) records only setup state, token hash, and configuration pointers, never secret values. Secret input before DB connection is kept only in process memory or an OS credential store TTL session and must be re-entered after a restart. After DB connection and migration succeed the state moves to the DB, and the local file keeps only a lock marker and minimal recovery metadata. A legitimate `RECONFIGURING` session is 30 minutes by default and returns to `LOCKED` automatically on expiry.

### 12.1 Setup Wizard Steps

1. Language, timezone, instance name, base URL, and local/remote setup transport confirmation
2. PostgreSQL connection or embedded Compose DB configuration and migration
3. Encryption master key/Secret Store provider configuration and key custody confirmation
4. System Owner account creation, TOTP MFA enrollment (mandatory), recovery code generation shown once, OIDC provider integration (optional)
5. Mattermost URL, bot account, webhook/WebSocket connection test
6. Artifact/Document storage and NAS path/quota test
7. SMTP/Telegram (optional) and reverse proxy/TLS status
8. Default channel templates, roles, approval/risk policy
9. Backup destination/retention, hard-delete tombstone ledger, telemetry/metrics settings
10. Default timezone, Scheduler polling/lease, missed-run default policy
11. Configuration summary, redacted diff, full preflight, atomic bootstrap commit

After setup completes the bootstrap endpoint is locked. Reconfiguration is possible only through this path: the System Owner enables maintenance mode and re-authenticates with the recovery code and MFA, which opens a time-limited reconfiguration session; every reconfiguration action is recorded as an AuditEvent, and the endpoint locks again when the session ends. Changeable settings are managed in the web admin console with validation, diff, secret redaction, and rollback.

## 13. Secret Broker

### 13.1 Goal

Deliver secrets such as tokens, accounts, API keys, and certificates to Agents within Task scope for the minimum time, without exposure in public channels, prompt history, Events, or logs.

### 13.2 Structure

- v8 default: an encrypted local secret store or a verified external provider adapter
- Recommended external providers: HashiCorp Vault, Infisical, or a SOPS-based file provider, chosen to fit the operating environment
- The Colab DB stores only provider references and metadata
- Agents authenticate to the Secret Broker with mTLS or short-lived service tokens
- A Secret Grant is limited to `agent + task + action + resource + expiry`
- Where possible a short-lived derived credential/lease is issued instead of the original secret
- Values are never re-displayed in the web console after registration; clipboard/download is restricted
- Audit records only secret ID/version and result, never values, partial values, or hashes

### 13.3 Delivery Method

1. The Agent requests a secret handle through MCP/API.
2. The Broker checks identity, Task, policy, Approval, and expiry.
3. If allowed, delivery is a one-time encrypted response or injection through the Agent's local sidecar.
4. The Agent Adapter injects the value as a process-scoped environment/file descriptor/in-memory handle and never places it in chat context.
5. After use, the lease is revoked and local cleanup is performed.

Work in which a secret is sent to an LLM provider is forbidden without the separate `llm_exposure_allowed` policy and Human Approval.

## 14. Result Documentation and Knowledge Storage

### 14.1 Canonical Document Structure and Lifecycle

Documents are produced in two stages. `DRAFT_PRE_VERIFICATION` records purpose, process, results, Artifacts, and open items but leaves the final verification result empty. When each VerificationRun becomes terminal, an immutable attempt document containing that attempt's verdict and residual risks is created; only when the latest verification is `PASSED` is the Task result document `FINALIZED` and the Task completed. Failure and blocked reports are preserved but never published as completion documents.

```markdown
# Title
## Purpose and Scope
## Participants and Roles
## Inputs and Resources Used
## Process and Key Events
## Discussion, Alternatives, Decisions and Rationale
## Results and Artifacts
## Verification Method and Results
## Shortcomings, Risks and Open Questions
## Follow-up Work
## Provenance
```

Provenance includes source channel/thread, Conversation/Task/Brainstorm/Decision/Event/Artifact/Verification/Schedule/ScheduleRun IDs, generating and reviewing Agents, template/version, and checksum.

Structured sections are generated deterministically from Events, Artifacts, Verification, and usage records. Narrative sections are optionally written by a Documentation Agent, but every paragraph must include Event/Artifact/Decision/Verification ID citations, and sentences without a citation cannot be published. A structured skeleton document is valid even without narrative.

### 14.2 Storage and Publishing

- The canonical source is stored as Markdown plus a metadata manifest.
- Default storage is Agent-Colab-managed storage/NAS and a versioned document registry.
- A Publisher interface is provided: Git repository/Gitea, BookStack, Wiki.js.
- The mandatory v8 implementation is filesystem/NAS plus a Git-compatible Markdown publisher.
- BookStack/Wiki.js keep a common adapter contract; a reference connector for one of the two is implemented and verified with contract/integration tests.
- A document becomes `PUBLISHED` after secret/PII redaction scanning and Human/Verifier review.
- The relationship between document provenance and retention policy is stated explicitly even if the original conversation is deleted.

## 15. Permissions and Security

1. Deny-by-default RBAC + capability + scope policy.
2. Separate Human/Agent/service account identities and per-Agent credentials.
3. PostgreSQL and admin/metrics endpoints are not exposed externally.
4. Mattermost/Telegram callback signature, timestamp, and replay verification.
5. Prevention of Bridge loops, spoofing, attachment malware, and path traversal.
6. Runtime UPDATE/DELETE permission on Events removed.
7. Detection and blocking of secret values entering messages/Events/logs/documents.
8. High-risk, external-send, and destructive actions require scoped Human Approval.
9. Setup endpoint locked after bootstrap; recovery code protected.
10. Administrator setting changes require re-authentication, audit, and before/after diff.
11. Artifact/Document ACL, checksum, retention, backup encryption.
12. Separation of implementing and verifying Agents and tamper-proof verification results.
13. Separate permissions for Schedule creation, modification, and Run now, with re-authorization at execution time.
14. Secret values are never stored in Task templates; secret references are validated for scope/expiry on every Run.
15. The Scheduler service identity never inherits Task action permissions automatically and uses only the intersection with the designated execution principal.
16. Break-glass enforces re-authentication, time limit, full announcement, and post-hoc independent verification.
17. Agent Limits (concurrent Tasks, rate, turns, cost/time) are server-enforced; excess requests are rejected and audited.
18. Hard delete requires the dual-approval workflow, waiting period, and tombstone preservation.
19. System Owner and Administrator must use TOTP MFA; Human Members per policy. Agent/service accounts use short-lived credentials and rotation instead of MFA.
20. AuditEvents and Verification results are also protected by append-only revisions and hashes.
21. The DLP zero-finding criterion applies to copies and outputs that Agent-Colab creates, stores, or delivers. Raw external inputs used for testing are kept only as isolated evidence and excluded from normal queries and Bridge targets.

## 16. Operations and Backup

- The Colab Server application and PostgreSQL run on local SSD/container volumes.
- NAS/object storage is used for Artifacts, Documents, archives, and encrypted backups.
- DB/Mattermost/config/Artifact/Document/secret metadata are backed up consistently.
- Secret value backups follow provider-specific encryption and a separate key custody policy.
- Hard-delete key tombstones are kept in an append-only ledger/KMS separate from normal backups and reconciled immediately after restore so that destroyed keys are never reactivated.
- Retention defaults to 14 daily, 8 weekly, 12 monthly and is changeable in the web console.
- A quarterly empty-environment restore rehearsal and a monthly backup read/checksum test are performed.
- Service dependency failures are shown on the dashboard and in the ops channel.
- Scheduler lag, due/claimed/running/failed/skipped Runs, lease owner, and next run time are observed.
- After a DB/server restart the missed-run policy is recomputed and the same Run is never created twice.
- The default operational targets are RPO 24 hours and RTO 4 hours; Phase 0 may only tighten them.

## 17. Success Metrics and Release Criteria

| Item | v8 target |
|---|---:|
| Registration and participation of 3+ different Agent Adapter types | success |
| Cycles/duplicates/unverified parent completion in parallel delegation and join with 3+ Agents | 0 |
| Web completion rate for Agent addition/Role modification | 100% |
| Telegram Bridge isolation per Mattermost channel | 0 cross-deliveries |
| Bridge normal delivery p95 | ≤ 5 s |
| Event duplicates/projection mismatches | 0 |
| Final verification approved by the same Agent as the implementer | 0 |
| High-risk execution without approval | 0 |
| Plaintext secret exposure in message/Event/log/document | 0 |
| Document generation rate for closed Task/Brainstorm/Schedule Run | ≥ 95% |
| Document provenance/verification information completeness | 100% |
| Clean install completed through web setup | ≤ 30 min |
| Backup restore rehearsal | success |
| Duplicate Runs for the same Schedule occurrence | 0 |
| Schedule on-time start delay p95 | ≤ 60 s under normal load |
| Missed-run policy consistency after server restart | 100% |
| Scheduled work executed after permission revocation | 0 |
| Human-only path (Task creation→approval→verification→document viewing using Mattermost only), consecutive successes | 10 |
| Executions exceeding Agent Limits/Schedule budget | 0 |

## 18. Key Risks

| Risk | Mitigation |
|---|---|
| Feature differences between Agent products | Adapter contract, capability negotiation, conformance tests |
| Multi-Agent delegation storms, cycles, lost results | depth/fan-out/concurrency limits, acyclic Task graph, join/verification gate |
| Permission confusion during Role changes | policy snapshot + emergency revoke Event |
| Mattermost–Telegram echo/duplicates | origin marker, mapping unique key, hop limit, outbox dedupe |
| Weak context/permissions on Telegram | channel-level scope, commands restricted by default, source display |
| Web console privilege escalation | re-authentication, CSRF/CSP, admin audit, least privilege |
| Initial Setup takeover | local-only bootstrap, one-time token, endpoint lock after completion |
| Secrets leaking into LLM context | sidecar/in-memory delivery, DLP scan, exposure policy |
| Factual errors in automatic documents | source citation, independent verification, human publish review |
| Bias/collusion of the verifying Agent | identity separation, fresh context, evidence-based verdicts, audit |
| Dependence on external document systems | canonical Markdown preservation, publisher adapters |
| Duplicate Scheduler execution | DB lease, `(schedule_id, occurrence_key)` unique, idempotency |
| DST/timezone malfunction | IANA timezone, next-run preview, DST fixture tests |
| Missed-run storms | limited backfill, maximum count/window, administrator alerts |
| Execution with stale permissions/secrets | re-authorization per Run, short-lived Secret Grants/leases |
| Overlapping scheduled work/cost explosion | concurrency, timeout, retry, budget policy |
| Deleted data resurrected from backups | external KMS/key tombstone ledger, forced reconciliation after restore |
| Ad hoc design of the product surface (commands, delivery, approvers) | Phase 0 contracts/spikes; server-enforced Command Router, work items, approver policy |
| Budget rendered ineffective by Adapters not reporting usage | mandatory usage schema, estimated fallback, conformance measurement |

## 19. Phased Roadmap

| Phase | Goal | Implementation result | Independent verification |
|---|---|---|---|
| 0. Baseline & Bootstrap | repo/schema/policy/setup skeleton | ADRs, contracts, Compose, Setup state | separate Architecture Verifier |
| 1. Core Event/Policy | deterministic server authority | Events, Tasks, projections, authz, minimal Approval/Artifact/Document Core | separate Core Verifier |
| 2. Mattermost/Telegram | basic conversation and channel Bridges | Gateway, Renderer, Bridge, outbox | separate Integration Verifier |
| 3. Generic Agent | dynamic Agents/Roles/Adapters and multi-Agent delegation | Registry, adapters, Task graph/join, web management | separate Agent Conformance Verifier |
| 4. Admin/Setup/Secrets | operations, initial setup, secret delivery | Admin Web, Wizard, Secret Broker | separate Security/Ops Verifier |
| 5. Scheduled Work | cron-based recurring work | Schedule, durable Run, history, alerts | separate Scheduler Verifier |
| 6. Collaboration/Docs | collaboration UX and document publishing | Approval UX, Brainstorm, document finalization, Publisher | separate Workflow/Docs Verifier |
| 7. Release Hardening | deployment, recovery, performance, security | CI/CD, alerts, restore, release | Release Verifier |

Every phase completes only with an independent Verifier PASS. In v8 there are no human gates between phases: when a phase passes, the next phase starts automatically. The single human decision in the pipeline is deployment approval, requested after Phase 7 has passed and the final development report has been delivered.

Detailed work and Exit Gates follow [[agent-colab-development-plan_en-v8]]; Test IDs and verdict rules follow [[agent-colab-validation-plan_en-v8]].

## 20. Next 10 Actions to Start Immediately

- [ ] 1. Adopt the three v8 documents as the baseline and record the removal of specific Agent/server roles and the Schedule principles in ADR-0001.
- [ ] 2. Write the inventory and access conditions of the target deployment environment, Mattermost, Telegram, PostgreSQL, and NAS/storage.
- [ ] 3. Fix the generic Agent Adapter/Role/Capability, multi-Agent Task graph/join, and Verification independence schemas.
- [ ] 4. Fix the Channel–TelegramBridge relationship, message mapping, and loop/dedupe contract.
- [ ] 5. Fix the Setup Wizard's pre-DB sealed bootstrap state, one-time token, migration, and reconfiguration rules.
- [ ] 6. Review Secret provider and lease/injection methods against the threat model and select the v8 provider.
- [ ] 7. Fix the Schedule schema, cron/timezone parser, and concurrency/missed-run/idempotency policies.
- [ ] 8. Fix the canonical result document template and the Git-compatible publisher contract.
- [ ] 9. Assign the Phase 0 repo, Compose, schema, policy, and CI skeleton to the implementing Agent and designate a different Agent as Verifier.
- [ ] 10. Start Phase 1 Event Store/Policy implementation only after Phase 0 has PASSED.

## 21. Change Management

The following changes require an ADR, impact analysis, independent verification, and System Owner approval; during an autonomous build run they are not made by the implementing Agent:

- Changes to Agent identity/Role/Capability permission interpretation
- Changes to Event immutability, verification independence, or Secret delivery boundaries
- Changes to the Mattermost-first principle or Channel Bridge isolation
- Expansion of Setup authentication or administrator permissions
- Changes to the canonical Document storage format
- Changes to cron interpretation, missed-run/concurrency policy, or Schedule execution identity

When any of the three documents changes, the Requirement registry in Appendix A and the test mapping are updated together.

## 22. Appendix A — Requirement Registry

Every mandatory requirement has an ID of the form `REQ-<AREA>-<number>`. This registry is the authority for requirement–implementation–verification traceability, and V-P0-10 checks mapping integrity against the three documents. Adding, removing, or weakening a mandatory requirement follows §21. The Development column refers to work packages in [[agent-colab-development-plan_en-v8]]; the Verification column refers to Test IDs in [[agent-colab-validation-plan_en-v8]].

| REQ ID | Requirement | Spec | Development | Verification |
|---|---|---|---|---|
| REQ-AGENT-001 | Generic Agent registration and lifecycle | §4.2 | P3-01 | V-P3-01, V-P3-08 |
| REQ-AGENT-002 | Role/Capability with explicit-deny precedence | §4.3 | P3-02 | V-P3-02, V-P3-09 |
| REQ-AGENT-003 | Conformance of 3 different Adapter types | §17 | P3-03~05 | V-P3-05, V-P3-12 |
| REQ-AGENT-004 | Server-enforced Agent Limits | §4.2, §15 | P3-08 | V-P3-15 |
| REQ-CHAN-001 | Mattermost-first conversation channel | §3.3 | P2-01 | V-P2-01 |
| REQ-CHAN-002 | 4 channel templates + custom | §8.1 | P2-01, P2-02 | V-P2-01, V-P2-19 |
| REQ-CHAN-003 | Soft delete after channel archive | §8.1 | P2-09 | V-P2-18 |
| REQ-BRDG-001 | Per-channel Bridge isolation | §10.1 | P2-05 | V-P2-03, V-P2-13 |
| REQ-BRDG-002 | echo/loop/dedupe blocking | §10.2 | P2-06 | V-P2-04, V-P2-07 |
| REQ-BRDG-003 | Duplicate Telegram target prohibition and exception | §10.1 | P2-05 | V-P2-17 |
| REQ-BRDG-004 | Telegram commands read/reply only by default | §10.2 | P2-08 | V-P2-16 |
| REQ-BRDG-005 | Bridge delivery p95 ≤ 5 s | §17 | P2-03, P2-06 | V-P2-15 |
| REQ-BRDG-006 | External identity verification and Account linking | §10.2, §15 | P1-05, P2-02, P2-08 | V-P1-23, V-P2-20~22 |
| REQ-EVNT-001 | Generic aggregate Events, append-only, idempotency, immutable bytes | §9.3, §11.2 | P0-03, P1-01, P1-02 | V-P0-13, V-P1-01~06, V-P1-20~22, V-P4-25 |
| REQ-EVNT-002 | Projection rebuild equivalence | §3.3 | P1-04 | V-P1-10, V-P7-08 |
| REQ-EVNT-003 | Event/Audit/Verification hash chain and tamper detection | §9.3, §15 | P1-01, P1-02, P1-06 | V-P1-21, V-P1-25, V-P7-16 |
| REQ-VRFY-001 | Effective identity separation and snapshot of implementer/verifier | §3.3, §8.5 | P0-07, P1-06, P3-02 | V-P0-07, V-P1-12, V-P1-24, V-P3-14, V-P3-16 |
| REQ-VRFY-002 | Immutable verification revisions | §8.5 | P1-06 | V-P1-13, V-P1-25 |
| REQ-APRV-001 | Approval subject, scope, expiry, bounded use | §8.4 | P1-08, P6-01 | V-P1-15, V-P1-16, V-P5-30, V-P6-01, V-P6-22 |
| REQ-APRV-002 | Zero unapproved high-risk execution | §15 | P1-08, P5-04, P6-01 | V-P5-18, V-P6-02 |
| REQ-SCRT-001 | No secret value exposure | §13 | P4-05~07 | V-P4-10, V-P4-14 |
| REQ-SCRT-002 | Scoped lease/TTL/revoke | §13.2 | P4-06 | V-P4-11~13 |
| REQ-SCRT-003 | LLM exposure control | §13.3 | P4-06 | V-P4-15 |
| REQ-SETP-001 | Wizard clean install within 30 minutes | §12, §17 | P0-09, P4-03 | V-P0-12, V-P4-01, V-P4-24 |
| REQ-SETP-002 | Bootstrap lock, token, pre-DB store defense | §12 | P0-05, P0-09, P4-03 | V-P0-12, V-P4-02, V-P4-03, V-P4-24 |
| REQ-SETP-003 | Legitimate reconfiguration path | §12 | P4-03 | V-P4-19 |
| REQ-SETP-004 | Owner/Administrator TOTP MFA mandatory, OIDC optional | §12.1, §15 | P4-09 | V-P4-20 |
| REQ-SETP-005 | Setup loopback/HTTPS-mTLS boundary, DB→key→Owner order, integration preflight | §12 | P0-06, P0-09, P4-03 | V-P0-12, V-P4-27, V-P4-28, V-P4-30 |
| REQ-ADMN-001 | 100% web completion of Account/Agent/Role management | §17 | P3-02, P3-07, P4-01 | V-P3-13, V-P3-16, V-P4-07, V-P4-26 |
| REQ-ADMN-002 | UI/API authz parity | §15 | P4-08 | V-P4-08 |
| REQ-ADMN-003 | Break-glass procedure | §4.4 | P4-10 | V-P4-21 |
| REQ-ADMN-004 | Hard-delete workflow | §11.2 | P4-11 | V-P4-22 |
| REQ-ADMN-005 | Audit search/export | §11.2 | P4-02 | V-P4-23 |
| REQ-SCHD-001 | Zero duplicate Runs for the same time | §8.6, §17 | P5-03 | V-P5-06~08 |
| REQ-SCHD-002 | IANA timezone and DST handling | §8.6 | P5-02 | V-P5-02~05 |
| REQ-SCHD-003 | Concurrency and missed-run policies | §8.6 | P5-05 | V-P5-09~14 |
| REQ-SCHD-004 | Re-authorization on every Run | §15 | P5-04 | V-P5-15, V-P5-16, V-P5-18 |
| REQ-SCHD-005 | Shell commands forbidden | §5.6 | P5-01 | V-P5-26 |
| REQ-SCHD-006 | Start delay p95 ≤ 60 s | §17 | P5-10 | V-P5-27 |
| REQ-SCHD-007 | Budget enforcement | §18 | P5-10 | V-P5-28 |
| REQ-SCHD-008 | Normative cron grammar and DOM/DOW OR | §8.6 | P0-08, P5-02 | V-P0-11, V-P5-01, V-P5-29 |
| REQ-SCHD-009 | Individual Run cancel state, Events, immutable terminal | §8.6 | P5-01, P5-06 | V-P5-20, V-P5-31, V-P5-32 |
| REQ-SCHD-010 | Occurrence-key based DST duplicate prevention | §8.6 | P0-08, P5-01~03 | V-P5-05, V-P5-06, V-P5-35 |
| REQ-SCHD-011 | Immutable ScheduleVersion and status lifecycle | §8.6, §9 | P5-01, P5-03, P5-08 | V-P5-22, V-P5-33 |
| REQ-SCHD-012 | Bounded retry, REPLACE, manual retry | §8.6 | P5-05, P5-06 | V-P5-11, V-P5-19, V-P5-20, V-P5-24, V-P5-34 |
| REQ-SCHD-013 | Phase 5 Schedule/Run subject activation | §8.4, §8.6 | P1-08, P1-09, P5-01, P5-04 | V-P1-15, V-P1-17, V-P5-30, V-P5-36 |
| REQ-DOCS-001 | Mandatory structure, provenance, two-stage lifecycle 100% | §14.1, §17 | P1-10, P6-04, P6-05 | V-P1-18, V-P1-19, V-P6-07~14, V-P6-23 |
| REQ-DOCS-002 | Document generation rate ≥ 95% | §17 | P6-04 | V-P6-20 |
| REQ-DOCS-003 | Git-compatible publisher | §14.2 | P6-06 | V-P6-15 |
| REQ-DOCS-004 | One optional connector verified | §14.2 | P6-06 | V-P6-21 |
| REQ-DOCS-005 | Separation of failed/blocked attempt documents and PASS completion documents | §8.2, §14.1 | P1-10, P6-04, P6-07 | V-P1-19, V-P6-19, V-P6-23, V-P6-24 |
| REQ-OPS-001 | Backup/restore consistency | §16 | P7-03 | V-P7-07 |
| REQ-OPS-002 | Retention setting applied | §16 | P7-03 | V-P7-19 |
| REQ-OPS-003 | 20 consecutive E2E, zero loss | §17 | P7-01~04 | V-P7-02~04 |
| REQ-OPS-004 | Zero high/critical security findings | §17 | P7-05 | V-P7-11 |
| REQ-OPS-005 | Zero hard-delete resurrection from backups | §11.2, §16 | P4-11, P7-03 | V-P4-29, V-P7-07, V-P7-20 |
| REQ-OPS-006 | Default RPO 24 h / RTO 4 h | §16 | P7-03 | V-P7-07 |
| REQ-OPS-007 | Defined normal and peak load targets | §17 | P5-10, P7-04 | V-P5-27, V-P7-03, V-P7-04 |
| REQ-BASE-001 | Product name, genericity, adopted baseline | §2, §3 | P0-02 | V-P0-01, V-P0-02 |
| REQ-BUILD-001 | Clean build and Compose reproducibility | §19 | P0-01, P0-04 | V-P0-03, V-P0-04 |
| REQ-CNTR-001 | Determinism of schema, policy, and Task transitions | §3.3, §8 | P0-03, P1-03, P1-04 | V-P0-05, V-P0-06, V-P1-07, V-P1-09, V-P1-14, V-P1-27 |
| REQ-THRT-001 | Full trust-boundary threat model, zero credentials | §15, §18 | P0-06 | V-P0-08, V-P0-09 |
| REQ-TRAC-001 | Semantic traceability, deterministic criteria, Phase DAG | §2, §19, §21 | P0-02 | V-P0-10, V-P0-14, V-P0-15 |
| REQ-IDNT-001 | Credential-based actor, anti-spoofing | §4, §15 | P1-05 | V-P1-08 |
| REQ-API-001 | Common REST/MCP commands, schema, ACL, idempotency, SSE resume | §7, §15 | P1-07 | V-P1-11, V-P1-26 |
| REQ-BRDG-007 | Direction, thread, mapping, transactional outbox, outage recovery | §10 | P2-03~06 | V-P2-02, V-P2-05, V-P2-06, V-P2-08, V-P2-14, V-P2-23 |
| REQ-BRDG-008 | Callback, DLP, attachment, administrative permissions | §10, §15 | P2-04, P2-06, P2-07 | V-P2-09~12 |
| REQ-AGENT-005 | Deterministic routing, delivery, cancel, membership, heartbeat lifecycle | §4.2, §8.5 | P3-03~06 | V-P3-03, V-P3-04, V-P3-06, V-P3-07, V-P3-10, V-P3-11, V-P3-17 |
| REQ-AGENT-006 | Multi-Agent sub-Tasks, fan-out/join, cycle prevention, reassignment | §4.2, §5.2, §9 | P3-09 | V-P3-18~20 |
| REQ-SETT-001 | Settings validation, diff, audit, rollback | §11, §12 | P4-04 | V-P4-04~06 |
| REQ-ADMN-006 | Web security, dashboard truth, accessibility | §11, §15 | P4-02, P4-08 | V-P4-09, V-P4-16, V-P4-18 |
| REQ-OPS-008 | Backup key separation, credential rotation, provider outage recovery | §16, §18 | P7-02, P7-03, P7-05 | V-P4-17, V-P7-05, V-P7-06, V-P7-12~14 |
| REQ-SCHD-014 | Per-Run Secret lease, Run now permission | §8.6, §13 | P4-06, P5-04, P5-08 | V-P5-17, V-P5-21 |
| REQ-SCHD-015 | Channel notification, Run history, metric truth | §5.6, §11 | P5-07~09 | V-P5-23, V-P5-25 |
| REQ-BRST-001 | Brainstorm limits, Decision, Taskify provenance | §8.3, §14 | P6-02 | V-P6-03, V-P6-04 |
| REQ-ARTF-001 | Artifact integrity, malicious input, ACL, generic subject links | §9, §15 | P1-09, P5-01, P6-03 | V-P1-17, V-P5-36, V-P6-05, V-P6-06, V-P6-25 |
| REQ-DOCS-006 | Publisher outage, version, permissions | §14.2 | P6-06, P6-07 | V-P6-16~18 |
| REQ-DOCS-007 | Schedule recurring summary | §5.6, §14 | P6-08 | V-P6-09 |
| REQ-RELS-001 | Clean install, upgrade, forward-fix, signed release | §16, §19 | P7-06, P7-07 | V-P7-01, V-P7-09, V-P7-10, V-P7-15 |
| REQ-RELS-002 | Evidence, residual risk, deployment approval | §17, §19 | P7-07 | V-P7-17, V-P7-18 |
| REQ-MMUX-001 | `/colab` command grammar, Task thread, card rendering | §8.7 | P0-10, P2-10, P2-11 | V-P0-16, V-P2-24, V-P2-25 |
| REQ-MMUX-002 | Interactive action security and Agent identity display | §8.7 | P2-12, P2-14 | V-P2-26, V-P2-28 |
| REQ-MMUX-003 | Mattermost user link challenge | §8.7, §10.2 | P2-13 | V-P2-27 |
| REQ-MMUX-004 | Human-only path acceptance | §3.1, §8.7, §17 | P7-09 | V-P7-22 |
| REQ-AGENT-007 | Work item delivery model, 3 Adapter flows, MCP transport | §4.2, §8.7 | P0-11, P1-12, P3-10~12 | V-P0-17, V-P1-29, V-P3-21~23 |
| REQ-AGENT-008 | Accept timeout and re-routing | §4.2 | P3-14 | V-P3-25 |
| REQ-AGENT-009 | Conformance suite CS-01~12 | §17 | P3-05 | V-P3-05 |
| REQ-COST-001 | Usage reporting, cost_units, pricing, budget enforcement | §4.2, §14.1, §17 | P1-14, P3-08, P3-15, P5-10 | V-P1-30, V-P3-15, V-P3-26, V-P5-28, V-P5-37 |
| REQ-TASK-001 | Mandatory acceptance criteria and evidence linking | §5.2, §8.5 | P1-11 | V-P1-28 |
| REQ-VRFY-003 | Automatic Verifier assignment, delivery, timeout reassignment | §8.5 | P3-13 | V-P3-24 |
| REQ-APRV-003 | Approver eligibility, self-approval ban, quorum, expiry escalation, re-authentication | §8.4 | P1-08, P4-14, P6-01 | V-P1-32, V-P4-33, V-P6-29 |
| REQ-BRST-002 | Brainstorm turn engine, facilitator, limits | §8.3 | P6-02 | V-P6-26 |
| REQ-BRST-003 | summary/decision/taskify | §8.3 | P6-09 | V-P6-27 |
| REQ-DOCS-008 | Narrative layer citations and skeleton determinism | §14.1 | P1-10, P6-10 | V-P6-28 |
| REQ-SCRT-004 | Secret sidecar | §13.3 | P4-12 | V-P4-31 |
| REQ-NOTF-001 | Notification rules, delivery, mute/digest | §8.7, §11 | P1-13, P2-17 | V-P1-31, V-P2-31 |
| REQ-POLC-001 | Permission and risk catalog | §15 | P0-12, P1-03 | V-P0-18 |
| REQ-MSG-001 | Message ingestion, retention, legal hold | §8.1, §9.1 | P2-15 | V-P2-29 |
| REQ-I18N-001 | Language settings and display | §12.1 | P2-16 | V-P2-30 |
| REQ-OPS-009 | Maintenance mode | §11.2 | P4-13 | V-P4-32 |
| REQ-OPS-010 | Runbook completeness | §16 | P7-08 | V-P7-21 |
| REQ-BRDG-009 | Telegram API constraint spike reflected | §10 | P0-13, P2-04 | V-P0-19 |
| REQ-PLAN-001 | Package sizing, prerequisite DAG, risk mapping, dependency owners, package↔Test mapping | §19 | P0-14 | V-P0-20 |
| REQ-QGAT-000 | Phase 0 full work and verification gate | §19 | P0-01~14 | V-P0-01~20 |
| REQ-QGAT-001 | Phase 1 full work and verification gate | §19 | P1-01~14 | V-P1-01~32 |
| REQ-QGAT-002 | Phase 2 full work and verification gate | §19 | P2-01~17 | V-P2-01~31 |
| REQ-QGAT-003 | Phase 3 full work and verification gate | §19 | P3-01~15 | V-P3-01~26 |
| REQ-QGAT-004 | Phase 4 full work and verification gate | §19 | P4-01~14 | V-P4-01~33 |
| REQ-QGAT-005 | Phase 5 full work and verification gate | §19 | P5-01~10 | V-P5-01~37 |
| REQ-QGAT-006 | Phase 6 full work and verification gate | §19 | P6-01~10 | V-P6-01~29 |
| REQ-QGAT-007 | Phase 7 full work and verification gate | §19 | P7-01~09 | V-P7-01~22 |
