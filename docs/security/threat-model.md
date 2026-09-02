# Agent-Colab Threat Model (P0-06)

- Status: Accepted baseline for Phase 0 (V-P0-08); revised per phase when a boundary changes
- Sources: spec §4.4, §10, §12, §13, §15, §18; development plan §3.1, §6.3–6.5, §7.5, §7A–§7B,
  §8.1, §9.3–§9.4, §24; validation plan §7.1 severity levels
- Requirement: REQ-THRT-001 (full trust-boundary threat model, zero credentials); REQ-SETP-005

This document never contains real credentials. Test canaries used by DLP fixtures follow the
pattern `CANARY-NOT-A-SECRET-<digits>` (example: `CANARY-NOT-A-SECRET-0001`) and are allow-listed
in `.gitleaks.toml`; every other secret-like string is a scan finding.

## 1. Trust zones

| Zone | Contents | Trust level |
|---|---|---|
| Z0 Public conversation | Mattermost channels/threads, Telegram chats/topics, LLM context | untrusted; readable by every member and by external Agents |
| Z1 Provider callbacks | Mattermost WebSocket/action callbacks, Telegram bot updates, Agent webhooks/MCP sessions | authenticated per message (signature/token), content untrusted |
| Z2 Agent-Colab server | API gateway, application services, Policy Engine, Event Store, Outbox, Scheduler, Secret Broker | trusted computing base; actor derived only from credentials |
| Z3 Persistence | PostgreSQL, Artifact/Document storage, encrypted secret store, backups | trusted for integrity when guarded by DB roles/triggers and hash chains |
| Z4 Operator surfaces | Setup Wizard (loopback), web admin console, metrics/admin endpoints | trusted only with MFA sessions, re-authentication, and network boundaries |
| Z5 Agent hosts | Adapter runtimes, secret sidecar | semi-trusted; hold only leased handles for the minimum time |

Data flows cross zones only through the server (Z2). No component in Z0/Z1 can write authority
state directly (spec §3.3 "Conversation is not state", development plan §3.1 module boundaries).

## 2. Boundary analysis

Each boundary lists assets, entry points, STRIDE threats, server-enforced controls, and the REQ IDs
and Test IDs that judge them. Severity follows validation plan §7.1.

### 2a. External identity — Mattermost/Telegram user → Account link

- Assets: `external_identity_links`, `identity_link_challenges`, Account permissions.
- Entry points: `/colab link start|confirm`, web console code entry, administrator approval,
  Telegram bot commands.
- Threats: **S** spoofed external user ID; **S** replayed or guessed link code; **E** command
  execution by an unlinked/suspended user; **E** one external user bound to several Accounts
  (alias escalation); **R** links without a verification record.
- Controls: link unique on `(provider_instance_id, external_user_id)` pointing to exactly one
  active Account (development plan §6.5); 10-minute single-use code by DM, 15-minute lockout after
  5 failures, `pending_admin` for the command-only path (§7A.5); unlinked users may run only
  `link`/`help`; suspend/revoke blocks commands immediately; `IDENTITY_LINK_CHALLENGED` /
  `IDENTITY_LINK_VERIFIED` Events. Severity of a bypass: High (command execution by unlinked
  identity).
- Judged by: REQ-BRDG-006, REQ-MMUX-003, REQ-IDNT-001 — V-P1-23, V-P2-20, V-P2-21, V-P2-22,
  V-P2-27, V-P1-08.

### 2b. Mattermost gateway — WebSocket, slash command, action callbacks, identity display

- Assets: bot account token, slash command token, action callback endpoint, Task/Brainstorm cards,
  the `/colab` command principal.
- Entry points: WebSocket events (`posted`, `post_edited`, `reaction_added`), `/colab` and `@colab`,
  `/api/v1/providers/mattermost/actions`.
- Threats: **S** forged action callback or slash request; **T** replayed callback (duplicate
  approval/verify click); **S** Agent choosing its own display identity in a payload; **E** free
  text interpreted as a command; **E** button click authorized only client-side; **D** oversized
  post bodies.
- Controls: integration token, 5-minute timestamp tolerance, one-time nonce, provider instance, and
  body hash validated before any command (§7.5, §7A.1); commands only through the
  `/colab <resource> <verb>` grammar validated by JSON Schema, errors ephemeral with zero side
  effects (§7A.2); server re-evaluates permissions at callback time and processes duplicate
  clicks once by idempotency key `(provider_instance + post_id)` (§7A.3); override username/icon
  set only by the server, payload display identity ignored and audited (§7A.4); bodies over 16k
  characters stored as Artifacts. Severity: callback spoof → High; free-text command → High.
- Judged by: REQ-MMUX-001, REQ-MMUX-002, REQ-BRDG-008 — V-P0-16, V-P2-09, V-P2-24, V-P2-26,
  V-P2-28.

### 2c. Telegram Bridge — per-channel isolation, echo/loop, dedupe, command policy, attachments

- Assets: bridge secret references, `message_mappings`, `delivery_outbox`, channel context.
- Entry points: Telegram bot updates, Mattermost→Telegram outbox deliveries, Bridge admin API.
- Threats: **I** cross-channel delivery (message of channel A reaching Telegram B); **T/D** echo
  loops and duplicates; **E** Task commands from Telegram under the default policy; **I** secret
  patterns or restricted Artifacts relayed; **T** malicious attachments (path traversal, MIME
  spoofing, oversize, malware); **E** Bridge changes by unauthorized accounts; **I** one Telegram
  target attached to two channels.
- Controls: the Mattermost Channel is the owning unit; duplicate Telegram targets rejected by
  default with explicit administrator exceptions (spec §10.1, §6.5); immutable origin marker, hop
  count, `UNIQUE(bridge_id, source_platform, source_message_id)` (§10.2, §6.5); transactional
  outbox with backoff and dead-letter; redaction before persistence and relay; read/reply-only
  default with §7A.6 restricted grammar and a verified active link required; attachment policy
  (allow-list MIME, size cap, path normalization, ClamAV quarantine); Bridge CRUD requires
  `bridge.manage`. Severity: cross-channel delivery or echo loop → Critical/High (validation plan
  §10 exit verdict).
- Judged by: REQ-BRDG-001..005, REQ-BRDG-007, REQ-BRDG-008, REQ-BRDG-009 — V-P0-19, V-P2-03,
  V-P2-04, V-P2-05, V-P2-07, V-P2-10, V-P2-11, V-P2-12, V-P2-13, V-P2-16, V-P2-17, V-P2-23.

### 2d. Setup Service — loopback default, remote 4-condition, token, sealed pre-DB store, apply order, reconfiguration

- Assets: setup token, bootstrap state file, DB credentials and master key material during setup,
  System Owner account, recovery code.
- Entry points: `/setup/api/v1/*` (`state`, `preflight`, `bootstrap`), maintenance-mode
  reconfiguration session.
- Threats: **E** initial setup takeover from a remote network; **S** token guessing/brute force;
  **T** re-running bootstrap after completion; **I** secrets written to disk before the DB exists;
  **R/T** partial Owner/TOTP records shown as created before the DB/key provider are ready;
  **E** reconfiguration without recovery code + MFA; **D** stale `RECONFIGURING` session left open.
- Controls: `/setup` binds to loopback by default; remote access requires HTTPS/TLS reverse proxy,
  client mTLS, IP allowlist, and a valid token, all four (spec §12, development plan §8.1);
  CSPRNG ≥ 256-bit token, 30-minute TTL, single-use, 5 failures per 15 minutes per IP and token
  fingerprint → 15-minute block; sealed owner-only `state.json` holding only state, token hash, and
  non-secret pointers; secrets only in process memory / OS credential store with 15-minute TTL;
  apply order DB/migration → master key/provider → Owner/TOTP/recovery code → integrations →
  atomic CONFIGURED/LOCKED; `/setup/bootstrap` answers 404/403 after LOCKED; reconfiguration only
  through maintenance mode + recovery code + MFA, 30-minute session, every action audited.
  Severity: takeover → Critical.
- Judged by: REQ-SETP-001, REQ-SETP-002, REQ-SETP-003, REQ-SETP-005 — V-P0-12, V-P4-01, V-P4-02,
  V-P4-03, V-P4-04, V-P4-19, V-P4-24, V-P4-27, V-P4-28, V-P4-30.

### 2e. Secret Broker and sidecar — grant/lease/handle, TTL/single-use, revoke, DLP, LLM exposure

- Assets: secret values (encrypted local provider), master key, grants, leases, handles, audit.
- Entry points: Secret Broker API (mTLS/service token), MCP `secret` handle delivery in work items,
  sidecar resolve socket, admin secret metadata API.
- Threats: **I** secret value in chat, Event payload, log, Artifact, or Document; **E** lease
  request for another Agent/Task/action; **T** handle replay after expiry or after single use;
  **I** handle resolved from a different host; **I** value passed into LLM context; **I** plaintext
  at rest in DB/backups; **R** audit that records values or hashes; **D** lease not revoked at
  Task end.
- Controls: Grant limited to `agent + task + action + resource + expiry` (spec §13.2); TTL 5
  minutes, single-use by default, revoke at Task end with new resolves rejected immediately and
  existing handles invalidated within 5 s (development plan §9.3); sidecar injects via socket/env/fd
  only, never disk, handle bound to sidecar instance ID (§9.4); values never re-displayed in the
  web console; audit stores only secret ID/version/result; DLP canary scan of messages, Events,
  logs, errors, Documents, and backup plaintext (§21.1 DLP scope); LLM exposure requires the
  `llm_exposure_allowed` policy plus Human Approval (spec §13.3); master key separated from DB and
  backups. Severity: plaintext exposure → Critical.
- Judged by: REQ-SCRT-001..004, REQ-OPS-008 — V-P4-10, V-P4-11, V-P4-12, V-P4-13, V-P4-14,
  V-P4-15, V-P4-17, V-P4-31, V-P2-10, V-P6-13.

### 2f. Admin console/API — RBAC, MFA, re-authentication, CSRF/CSP, UI/API parity, break-glass

- Assets: Accounts, Roles, policies, settings, Approvals queue, audit, break-glass sessions.
- Entry points: `/api/v1/*` admin groups, web admin console, Approvals queue, break-glass
  activation, hard-delete workflow.
- Threats: **E** privilege escalation by calling the API directly around UI hiding; **S** session
  fixation/CSRF; **E** critical action without MFA or re-authentication; **E** self-approval or
  Agent approval of HIGH+ actions; **T** settings changed without diff/audit; **E** break-glass used
  for routine changes or to alter Events/secrets; **I** metrics/admin endpoints exposed publicly.
- Controls: deny-by-default RBAC + capability + scope with `explicit deny > scope restriction >
  allow` (spec §4.3, §15); server-side authorization mandatory, UI hiding never controls access
  (development plan §11.2); TOTP MFA mandatory for Owner/Administrator; re-authentication for HIGH
  and above and for settings apply (§7E, §8.3); CSRF tokens, CSP, secure cookies, session expiry;
  approver eligibility excludes requester/implementer/aliases, `SELF_APPROVAL_FORBIDDEN`, quorum
  per risk (§7E); settings versions with redacted before/after diff and rollback; break-glass
  requires recovery code + MFA, 60-minute limit, immediate ops-channel announcement, automatic
  post-hoc independent verification Task, and never Event mutation or plaintext secret reads (spec
  §4.4); PostgreSQL/admin/metrics endpoints not exposed externally. Severity: escalation →
  Critical/High.
- Judged by: REQ-ADMN-001..006, REQ-SETP-004, REQ-APRV-003, REQ-SETT-001 — V-P4-05, V-P4-06,
  V-P4-07, V-P4-08, V-P4-09, V-P4-16, V-P4-18, V-P4-20, V-P4-21, V-P4-23, V-P4-26, V-P4-33.

### 2g. Event Store and verification immutability — UPDATE/DELETE revocation, hash chain, crypto-shredding

- Assets: `events`, `audit_events`, `verification_runs`/`verification_revisions`,
  `audit_hash_anchors`, sensitive ciphertext and key references.
- Entry points: low-level append API (designated services only), DB connections of runtime/admin
  roles, projection rebuild, integrity job.
- Threats: **T** UPDATE/DELETE of stored Event bytes; **T** payload/ciphertext/previous_hash
  tampering; **R** implementer submitting a PASS on its own work; **T** editing a verification
  result instead of adding a revision; **S** actor taken from the request body; **T** duplicate
  Events on retry; **I** sensitive data in `payload`.
- Controls: runtime and admin roles have no UPDATE/DELETE on Events/audit/verification revisions
  and triggers block them (development plan §6.3, §6.4); SHA-256 over RFC 8785 canonical JSON +
  immutable metadata + ciphertext + `previous_hash`, daily anchors in separate storage; aggregate
  appends serialized by advisory lock/expected sequence; scoped idempotency `(workspace, actor,
  scope, key)` with `IDEMPOTENCY_CONFLICT` on body mismatch; DB CHECKs `implementer_account_id <>
  verifier_account_id` and agent inequality plus credential/alias checks; immutable
  implementer/verifier snapshot per VerificationRun; sensitive content only as envelope-encrypted
  ciphertext with a key reference; redaction applied at projection/display only. Severity:
  verification forgery or authority corruption → Critical.
- Judged by: REQ-EVNT-001..003, REQ-VRFY-001, REQ-VRFY-002, REQ-CNTR-001 — V-P0-07, V-P0-13,
  V-P1-05, V-P1-08, V-P1-12, V-P1-13, V-P1-20, V-P1-21, V-P1-24, V-P1-25.

### 2h. Hard delete and backup resurrection — DEK tombstone ledger, restore reconciliation

- Assets: per-target data-encryption keys, `key_tombstones` ledger, backups, restore procedure.
- Entry points: hard-delete administrator workflow, backup/restore jobs, KMS/ledger.
- Threats: **T** hard delete executed with a single approver or without the waiting period; **T**
  direct DELETE of Event rows under the pretext of hard delete; **I** destroyed content resurrected
  by restoring a pre-deletion backup with key material; **R** missing tombstone/AuditEvent after
  execution.
- Controls: dual approval by different requester and approver, retention/referential checks,
  72-hour waiting period, tombstone + AuditEvents preserved (spec §11.2); hard delete destroys only
  the DEK, Event rows/ciphertext/hashes unchanged (development plan §6.3); tombstones in an
  append-only ledger/KMS separated from normal backups; restore performs tombstone reconciliation
  before the service opens and never re-registers destroyed DEKs (§9.3, §23.3). Severity:
  resurrection → High (unrecoverable state / data-protection breach).
- Judged by: REQ-ADMN-004, REQ-OPS-005, REQ-OPS-001 — V-P4-22, V-P4-25, V-P4-29, V-P7-07,
  V-P7-20.

### 2i. Agent adapters and work delivery — HMAC webhook, MCP auth, handle non-exposure, limits/budget

- Assets: work items and payloads, service tokens, webhook signing keys (Secret Broker refs), usage
  and budget ledgers, routing decisions.
- Entry points: `/mcp` Streamable HTTP, webhook POSTs to Agent endpoints and result intake, bot
  replies in Task threads, heartbeat/probe.
- Threats: **S** forged or replayed webhook delivery/result; **S** MCP call with invalid token or
  another Agent's identity; **T** duplicate results or side effects on redelivery; **I** secret
  handle printed in logs/messages; **E** Agent invoking an unadvertised tool; **D** delegation
  storms, unbounded fan-out/depth, budget exhaustion; **E** permission granted by product name.
- Controls: HMAC-SHA256 + timestamp (5-minute window) + nonce (24 h) with signing key as a Secret
  Broker reference (development plan §7B.2); Bearer service token bound to the Agent Account or
  mTLS; results accepted exactly once per `work_item_id`, duplicates ignored and audited (§7B.1);
  conformance CS-07 (zero handle values in logs) and CS-10 (`CAPABILITY_UNSUPPORTED`); acyclic
  Task graph with depth/fan-out/concurrency limits; server-enforced Agent Limits with
  `budget_reservations` and `BUDGET_EXCEEDED`; eligibility from Registry/Role/Capability only, no
  product-name privileges (spec §2, §15). Severity: forged delivery/results → High.
- Judged by: REQ-AGENT-001..009, REQ-COST-001 — V-P0-17, V-P1-29, V-P3-05, V-P3-15, V-P3-19,
  V-P3-21, V-P3-22, V-P3-23.

### 2j. Scheduler — re-authorization per Run, no shell

- Assets: Schedules, immutable ScheduleVersions, ScheduleRuns, execution principal, Run leases.
- Entry points: schedule API/MCP tools, planner/runner processes, `Run now`, Run cancel/retry.
- Threats: **E** action template smuggling a shell command; **E** Run executing with stale
  permissions after principal revocation or Agent suspension; **E** Scheduler service identity
  inheriting Task permissions; **T** duplicate Runs for one occurrence under dual planners or
  restart; **T** live Schedule edits overwriting an existing Run snapshot; **E** high-risk recurring
  action without a per-Run or bounded Approval; **I** secret values embedded in templates.
- Controls: `action_template` restricted to versioned Agent-Colab action schemas, shell strings
  rejected (development plan §6.6, §10A.4); policy/Role/Agent/Approval/Secret re-checked at every
  Run start with `SKIPPED_POLICY` / `SKIPPED_AGENT_UNAVAILABLE`; Scheduler identity uses only the
  intersection with the execution principal (spec §15.15); `UNIQUE(schedule_id, occurrence_key)`,
  `FOR UPDATE SKIP LOCKED`, DB leases; immutable `schedule_versions` pinned per Run; approval
  consumption atomic with Run claim/Task creation; only secret references in templates with a
  short lease per Run. Severity: unauthorized or duplicate execution → High/Critical.
- Judged by: REQ-SCHD-001, REQ-SCHD-004, REQ-SCHD-005, REQ-SCHD-011, REQ-SCHD-014 — V-P0-11,
  V-P5-06, V-P5-07, V-P5-15, V-P5-16, V-P5-17, V-P5-18, V-P5-26, V-P5-30, V-P5-33.

## 3. Cross-cutting controls

- Actor identity comes only from the credential; body/header actor claims are ignored (V-P1-08).
- All writes carry `Idempotency-Key` and a correlation ID; not-found and forbidden are the same
  404 per the information-disclosure policy (development plan §7.5).
- DLP zero-finding criterion applies to every copy Agent-Colab creates, stores, or delivers; raw
  external fixtures containing canaries are isolated evidence (spec §15.21).
- Every phase report and evidence file is scanned for secrets before commit (§4 below).

## 4. Secret hygiene in this repository

- `.env` is git-ignored from the first commit and holds the deployment target credentials; no value
  from it may appear in tracked files, evidence, or reports (`docs/security/secret-scan.md`).
- `gitleaks` scans history and the working tree in CI (`make secret-scan`) with the canary
  allow-list in `.gitleaks.toml`.
- Test fixtures use obviously fake values (`CANARY-NOT-A-SECRET-0001`, `not-a-real-token`).

## 5. Boundary checklist (V-P0-08)

`tools/threat_model_lint.py` parses this table: the five mandatory boundaries must be present
with `yes`, non-empty controls, and Test IDs that exist in `docs/traceability.json`.

| Boundary | Included | Controls | Tests |
|---|---|---|---|
| Mattermost | yes | callback signature/timestamp/nonce/body hash; `/colab` grammar only; server-side authz at callback; server-only display override | V-P0-16, V-P2-09, V-P2-24, V-P2-26, V-P2-28 |
| Telegram | yes | per-channel Bridge ownership; origin marker + hop count + mapping unique key; read/reply-only default; attachment policy; redaction | V-P0-19, V-P2-03, V-P2-04, V-P2-07, V-P2-10, V-P2-11, V-P2-16, V-P2-17 |
| Setup | yes | loopback default; remote HTTPS/TLS + client mTLS + allowlist + token; single-use token with lockout; sealed pre-DB store; DB→key→Owner/TOTP order; endpoint lock; reconfiguration via maintenance mode + recovery code + MFA | V-P0-12, V-P4-02, V-P4-03, V-P4-04, V-P4-19, V-P4-24, V-P4-27, V-P4-28 |
| Secret | yes | scoped grant/lease; TTL 5 min single-use; revoke ≤ 5 s; sidecar socket/env/fd injection, host-bound handles; DLP canaries; LLM exposure needs policy + Human Approval; master key separation | V-P4-10, V-P4-11, V-P4-12, V-P4-13, V-P4-14, V-P4-15, V-P4-17, V-P4-31 |
| Admin | yes | deny-by-default RBAC with explicit-deny precedence; TOTP MFA; re-authentication; CSRF/CSP; UI/API parity; approver eligibility and quorum; break-glass time limit + announcement + post-hoc verification | V-P4-08, V-P4-09, V-P4-20, V-P4-21, V-P4-26, V-P4-33 |
| External identity | yes | unique verified link per provider instance; challenge TTL/single-use/lockout; unlinked users read-only | V-P1-23, V-P2-20, V-P2-21, V-P2-22, V-P2-27 |
| Event Store / verification immutability | yes | UPDATE/DELETE revoked + triggers; RFC 8785 + SHA-256 chain + anchors; implementer≠verifier DB/API checks; immutable revisions; crypto-shredding | V-P0-07, V-P0-13, V-P1-05, V-P1-12, V-P1-13, V-P1-21, V-P1-24, V-P1-25 |
| Hard delete / backup resurrection | yes | dual approval + 72 h wait; DEK destruction only; separate tombstone ledger; restore reconciliation before open | V-P4-22, V-P4-25, V-P4-29, V-P7-20 |
| Agent adapters / work delivery | yes | HMAC + timestamp + nonce; Bearer/mTLS for MCP; exactly-once results; CS-07/CS-10; acyclic graph limits; budget reservations | V-P0-17, V-P1-29, V-P3-05, V-P3-15, V-P3-19, V-P3-21, V-P3-22 |
| Scheduler | yes | action schema without shell; per-Run re-authorization; occurrence-key uniqueness + leases; immutable versions; atomic approval consumption | V-P0-11, V-P5-06, V-P5-15, V-P5-18, V-P5-26, V-P5-30, V-P5-33 |
