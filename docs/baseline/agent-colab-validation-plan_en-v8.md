# Agent-Colab Validation Plan v8 (EN)

> Document version: 8.0  
> Product baseline: [[agent-colab-project-spec_en-v8]]  
> Implementation baseline: [[agent-colab-development-plan_en-v8]]  
> Document role: authority for independent verification, phase passage, and final acceptance  
> Supersedes: this document replaces validation plan versions v1–v7. It is the English canonical text; the Korean v7 is the last Korean edition.

## 1. Purpose

This document defines the targets, methods, independence rules, evidence formats, and verdict criteria for verifying Agent-Colab phase by phase. The implementing Agent's self-report may be consulted as evidence but can never be the final PASS verdict. In v8 all phase verification is executed automatically by the verifying Agent; no human sign-off exists between phases, and the only human decision is deployment approval after the final development report.

## 2. Verification Principles

1. **Identity separation**: Implementer and Verifier differ in Account, `agent_id`, service credential, and alias.
2. **Fresh review**: the Verifier independently reads the product baseline, development plan, change diff, execution environment, and raw evidence.
3. **Evidence over assertion**: reproducible test output, DB queries, API responses, hashes, screenshots, and logs outrank explanations.
4. **Negative first**: rejection, forgery, duplication, failure, and recovery paths are verified, not only allowed paths.
5. **No silent fix**: the Verifier does not, as a rule, modify product code; Findings and reproduction steps are returned to the implementer.
6. **Immutable result**: verification results are never edited; a new revision is added.
7. **No skipped gate**: the next Phase cannot be declared complete unless the previous Phase PASSED.
8. **Secret-safe evidence**: evidence never contains real secret values or recoverable fragments.
9. **Traceability**: every Test links to requirement, commit/image, environment, and evidence.
10. **Human authority (v8 scope)**: inside the product, production-impacting, break-glass, and high-risk/secret-boundary actions still require Human approval as specified. Inside the development pipeline, no human approval is required for any Phase; the single human decision is whether to deploy, taken after the final development report.

## 3. Verification Roles

| Role | Responsibility | Forbidden |
|---|---|---|
| Implementer Agent | feature implementation, self-tests, evidence manifest submission | final PASS of its own result |
| Phase Verifier Agent | independent execution of the Phase criteria, Findings and verdict | unauthorized product code changes during verification |
| Security Verifier | authz, Setup, Secret, Bridge attack-surface verification | reading real operational secrets |
| Operations Verifier | deployment, monitoring, backup/restore, incident response | PASS with unverified recovery |
| Documentation Verifier | source, facts, limitations, and redaction of result documents | approving publication without provenance |
| Deployment Approver (Human) | decides whether to deploy after the final development report | approving exceptions without evidence |

One Agent may hold several specialist Verifier roles but cannot be both Implementer and Verifier for the same scope. Phases 4 and 7 require a Security or Operations specialist verdict in addition to the general Phase Verifier. In v8 the Implementer role is held by Claude Code and every Verifier role by Codex, invoked as a separate process with a fresh context.

## 4. Independence Rules

### 4.1 Mandatory Checks

When a VerificationRun is created the server confirms:

- `implementer_agent_id != verifier_agent_id`
- the two identities share no credential/fingerprint
- they do not resolve to the same effective principal in the Human/Agent/service Account alias graph
- the verifier is not the author of the target commit nor the assignee of the implementation Task
- the verifier role has the `verification.submit` permission
- the target commit/image digest is pinned
- the criteria version is pinned

### 4.2 Context Separation

Provided to the Verifier:

- the three baseline documents and applicable ADRs
- the target commit SHA/image digest
- the change manifest and self-test evidence
- clean-environment reproduction steps
- known limitations

Not provided by default:

- prompts that present the implementing Agent's conclusions as the answer
- unnecessary internal reasoning that could bias verification
- real secret values

### 4.3 Exceptions

If no different Agent can be obtained, the result is `BLOCKED_INDEPENDENCE`. Human code review alone never substitutes automatically. An emergency patch may be deployed, but a post-hoc independent verification Task with an expiry must be created. In an autonomous run, `BLOCKED_INDEPENDENCE` stops the pipeline and is reported to the user; the implementer never self-verifies to proceed.

## 5. VerificationRun States

```text
PLANNED → ASSIGNED → RUNNING →
  PASSED | FAILED | BLOCKED | CANCELLED

FAILED → FIX_SUBMITTED → RECHECK_ASSIGNED → RUNNING → ...
```

- `PASSED`: every mandatory Test passes and there are no open blocker/critical/high Findings.
- `FAILED`: reproducible criteria violation or mandatory Test failure.
- `BLOCKED`: verdict impossible because of environment, permissions, or external dependencies. Never counted as success.
- `CANCELLED`: verification lost its value, e.g., the target commit was superseded.

## 6. Submissions and Evidence

### 6.1 Implementer Evidence Manifest

```yaml
implementation_id: P3-IMPLEMENT-001
phase: 3
implementer_agent_id: agent-...
implementer_account_id: account-...
implementer_credential_fingerprint: sha256:...
identity_graph_version: identity-...
commit_sha: ...
image_digests: [...]
requirements: [REQ-AGENT-001, REQ-AGENT-002]
changed_files: [...]
migrations: [...]
tests_run:
  - id: SELF-P3-001
    command: ...
    result: pass
known_gaps: [...]
rollback: ...
evidence_refs: [...]
```

### 6.2 Verifier Report

```yaml
verification_id: VR-P3-001
criteria_version: v8.0
implementer_agent_id: agent-...
verifier_agent_id: agent-...
implementer_account_id: account-...
verifier_account_id: account-...
implementer_credential_fingerprint: sha256:...
verifier_credential_fingerprint: sha256:...
identity_graph_version: identity-...
effective_policy_hash: sha256:...
target_commit: ...
environment_fingerprint: ...
started_at: ...
completed_at: ...
tests:
  - id: V-P3-01
    result: PASS
    evidence_ref: ...
findings: [...]
result: PASSED
residual_risks: [...]
```

### 6.3 Acceptable Evidence

- JUnit/coverage/contract reports
- raw test logs including commands and exit codes
- redacted API requests/responses and DB query results
- file/image/backup hashes and manifests
- UI screenshots/videos or Playwright traces
- network/permission/scan reports
- Mattermost/Telegram message ID mapping exports
- restore rehearsal logs with timing

Evidence consisting only of narrative, only of editable screenshots, or containing secrets is not accepted on its own.

## 7. Common Verdict Rules

### 7.1 Severity

| Level | Definition | Gate effect |
|---|---|---|
| Critical | secret/administrator takeover, authority corruption, unapproved high-risk execution | immediate FAIL |
| High | permission bypass, cross-channel leakage, unrecoverable state, verification forgery | FAIL |
| Medium | major functional error/missing observability/limited disclosure | FAIL in principle; explicit risk acceptance required |
| Low | minor non-core UX/docs/performance issues | conditionally allowed with owner and deadline |

### 7.2 Test Results

- PASS: expected result and evidence both present.
- FAIL: expected result mismatch or insufficient evidence.
- NOT_RUN: not executed. If mandatory, the Phase is FAIL/BLOCKED.
- NOT_APPLICABLE: requires a reason pre-approved by the criteria owner.

### 7.3 Recheck

- After a fix, the full Phase smoke suite and the failed Tests are re-run.
- Changes to Event/Policy/Secret/Bridge core require full regression of the related Phases.
- A changed target commit creates a new VerificationRun revision.

### 7.4 Package-Level Progress Verdicts

- Every work package carries the Test IDs in the `Tests` column of the development plan table as its minimum self-test set. The implementer must submit `SELF-<ID>` evidence for those IDs to mark the package `IMPLEMENTED`.
- The Phase Verifier re-runs everything at the end of the Phase, but also spot-rechecks at least one Test per `IMPLEMENTED` package during the Phase to confirm the reliability of progress reports. A failed spot recheck returns the package to `IN_PROGRESS` and records a Finding.
- Phase progress = size-weighted sum of `IMPLEMENTED` packages / total size-weighted sum (S=1, M=2.5, L=5). The dashboard shows per-package SELF evidence presence, spot recheck results, and starts that violated prerequisites.
- A package started in violation of its prerequisites is a Medium Finding and cannot be marked complete before its prerequisites are `IMPLEMENTED`.

### 7.5 Autonomous Verification Execution (v8)

- Every VerificationRun is executed by Codex, started by the implementer (Claude Code) as a separate non-interactive process with a fresh context, using the inputs of §4.2 and nothing that suggests a conclusion.
- The Verifier Report (§6.2) is stored unmodified under `verification/phase-<n>/` in the repository with its SHA-256 and committed. The implementer never edits, reorders, or summarizes it into the authoritative record.
- A Phase transitions on `result: PASSED` only. `FAILED` reopens the Phase implementation; `BLOCKED` or `BLOCKED_INDEPENDENCE` stops the pipeline and is reported to the user with the blocker.
- Deployment approval is the only human decision: after Phase 7 PASSES and the final development report is delivered, the user answers whether to deploy. No other question about phase passage is asked.

## 8. Phase 0 Verification — Baseline and Bootstrap

### Assignee

Architecture Verifier Agent, with an identity different from the Implementer.

| Test ID | Subject | Method | PASS criterion |
|---|---|---|---|
| V-P0-01 | product name consistency | search repo/docs/UI metadata | user-facing name is Agent-Colab |
| V-P0-02 | fixed Agent roles removed | search schema/policy/code | no specific Agent product/machine hard-coded as a core role |
| V-P0-03 | clean bootstrap | lock install/build/test from a new checkout | succeeds by the documented procedure |
| V-P0-04 | Compose health | start the stack with empty volumes | DB/server/web health pass |
| V-P0-05 | schema fixtures | run valid/invalid fixtures | valid accepted, invalid stable error |
| V-P0-06 | policy fixtures | deny/allow/conflict matrix | explicit deny wins, deterministic result |
| V-P0-07 | verification independence | attempt same implementer/verifier creation | rejected at DB and API |
| V-P0-08 | threat boundary | threat model checklist review | Mattermost, Telegram, Setup, Secret, Admin included |
| V-P0-09 | secret scan | repo/history/config scan | zero real credentials |
| V-P0-10 | requirement trace | check spec Appendix A registry against the three documents | every mandatory REQ linked to development and verification; every mandatory P/V ID back-linked to at least one REQ |
| V-P0-11 | Schedule contract | cron/timezone/occurrence/version/status/action/concurrency/missed/retry/cancel fixtures | schema matches v8 policy |
| V-P0-12 | pre-DB bootstrap contract | sealed state schema/permission/migration/reconciliation fixtures | starts without a DB, no secret values stored, rollback never regresses the setup stage |
| V-P0-13 | aggregate/Event contract | Task/Approval/Schedule/Agent fixture schema review | every state aggregate has type/id/seq/hash/idempotency scope; no projection-only permission decision |
| V-P0-14 | deterministic criteria | mandatory Test criteria schema lint | every Test has at least one applicable numeric/state/error-code/hash-equality/invariant criterion; zero criteria judged only by words such as "appropriately", "as far as possible", "per policy" |
| V-P0-15 | Phase dependency DAG | check the introducing Phase of every entity referenced by work/tests | only entities of an earlier or the same Phase referenced; zero forward dependencies outside explicit contract stubs |
| V-P0-16 | Mattermost interaction contract | command grammar valid/invalid fixtures, Task card/thread rule schema, action callback contract, override/slash-command spike evidence | schema exists for every resource/verb in development plan §7A.2, invalid 100% stable error, spike result recorded as possible/not possible with fallback |
| V-P0-17 | work-item/usage contract | work item state machine fixtures, HMAC webhook fixtures, usage schema, pricing.yaml schema, Streamable HTTP long-poll/subscribe spike evidence | all fixtures pass; long-poll response within 30 s and redelivery of un-acked items after reconnect proven by spike logs |
| V-P0-18 | permission/risk catalog | lint permissions.yaml/risk-rules.yaml, compare against the full action list | zero out-of-vocabulary permissions in Role fixtures, zero unclassified actions, quorum defaults match §7E |
| V-P0-19 | Telegram API spike | forum topic/reply/edit/rate-limit evidence | zero contradictions between Bridge thread mapping rules and spike results |
| V-P0-20 | plan operating baseline | lint development plan package tables, §25, §25A | every P-ID has a size and ≥ 1 Test ID, prerequisite DAG acyclic, every V-ID back-linked to ≥ 1 P-ID, every §25A row has ≥ 1 package, every §25 row has owner and deadline |

### Exit verdict

V-P0-01~20 all PASS. Only Low documentation Findings may remain, with owner and deadline. No human approval is required; Phase 1 starts automatically.

## 9. Phase 1 Verification — Event, Policy, Verification Core

### Assignee

Core Verifier Agent.

| Test ID | Subject | Method | PASS criterion |
|---|---|---|---|
| V-P1-01 | aggregate Event append | normal requests on Task/Approval/Agent aggregates | aggregate seq/Event/Projection/response consistent |
| V-P1-02 | scoped idempotent retry | repeated and concurrent requests with the same workspace/actor/operation/key/body | 1 Event, same result; different scopes processed independently |
| V-P1-03 | key conflict | same key, different body | `IDEMPOTENCY_CONFLICT` |
| V-P1-04 | aggregate sequence concurrency | concurrent appends on the same Task, Approval, and Agent | unique monotonic seq per aggregate, zero loss |
| V-P1-05 | Event immutability | UPDATE/DELETE with runtime/admin app roles | rejected at DB trigger/permission level |
| V-P1-06 | causality | non-existent parent/cycle attempts | stable error, no domain Event |
| V-P1-07 | policy deny | action without scope/capability | deny + redacted audit |
| V-P1-08 | actor spoof | tampered body/header actor | credential identity preserved |
| V-P1-09 | state transition | re-run/complete a terminal Task | rejected, state unchanged |
| V-P1-10 | projection rebuild | delete projection then replay | identical canonical snapshot hash |
| V-P1-11 | SSE resume | disconnect + Last-Event-ID | resume without gaps or duplicates |
| V-P1-12 | self-verification | implementer submits pass | API/DB reject, audit created |
| V-P1-13 | verifier revision | fail→fix→recheck | previous result immutable, new revision linked |
| V-P1-14 | complete gate | completion attempt before verification | `VERIFICATION_REQUIRED` |
| V-P1-15 | Approval subject Phase 1 | create/query per task/action subject, request schedule/run | task/action fixed successfully; not-yet-active schedule/run handlers return stable `SUBJECT_TYPE_NOT_ACTIVE` |
| V-P1-16 | Approval bounded consume | expired/revoked/concurrent max-use consumption | only the valid count succeeds atomically, zero over-execution |
| V-P1-17 | Artifact Core | Task Artifact metadata/hash/ACL and premature Run link | Task link/checksum consistent, unauthorized read rejected, Run link returns `SUBJECT_TYPE_NOT_ACTIVE` |
| V-P1-18 | pre-verification draft | Document generated after implementation submit | `DRAFT_PRE_VERIFICATION`, no final verdict, provenance linked |
| V-P1-19 | document finalization | FAILED/BLOCKED/PASSED each executed | the first two produce ATTEMPT_FINALIZED with Task not completed; only PASSED allows a new FINALIZED and completion |
| V-P1-20 | Event crypto-shredding | compare bytes/hash before/after DEK destruction of a sensitive Event | undecryptable; Event row/ciphertext/content_hash byte-for-byte unchanged |
| V-P1-21 | hash chain/tamper | recompute canonical JSON; tamper payload/ciphertext/previous_hash | normal chain matches; every tamper detected 100% |
| V-P1-22 | projection authority forbidden | concurrent consume from a stale/deleted Approval projection | only the ledger-correct count succeeds; identical after rebuild |
| V-P1-23 | external identity core | provider/user link creation, duplicates, suspend/revoke | exactly one active Account per link, duplicates rejected, suspension blocks commands immediately |
| V-P1-24 | verifier identity snapshot | query VerificationRun before/after Account/Agent/credential/alias changes | creation-time snapshot/hash immutable; independence at that time reproducible |
| V-P1-25 | audit/verification immutability | UPDATE/DELETE with runtime/admin roles and chain tampering | rejected at DB or detected by anchor mismatch; existing revisions unchanged |
| V-P1-26 | REST/MCP contract parity | run the same create/delegate/approval commands via REST and MCP and compare schema/error/idempotency/expected-seq fixtures | same application handler and Policy result, identical stable error/Event shapes; zero bypass side effects |
| V-P1-27 | Task transition table | table-driven execution of the normal flow, FAILED→RUNNING, BLOCKED→WAITING, PASSED→VERIFIED, and every invalid/terminal write | only defined transitions succeed; invalid ones give stable errors with zero Event side effects; COMPLETED/CANCELLED immutable |
| V-P1-28 | acceptance criteria | delegate without criteria, submit without evidence for required criteria, change criteria | the first two rejected with `ACCEPTANCE_CRITERIA_REQUIRED`/`EVIDENCE_REQUIRED` and zero side effects; changes only via new revision + Event |
| V-P1-29 | work item inbox | deliver→no ack, 3 redeliveries, duplicate results, deadline exceeded | exactly 3 redeliveries after 60-second ack timeout then EXPIRED; results exactly once; duplicate results ignored + audited |
| V-P1-30 | usage/pricing | known model, unknown model, result without usage | cost_units match the pricing-version computation; unknown uses the default rate + `estimated`; missing usage without a reason is rejected |
| V-P1-31 | notification rules core | inject APPROVAL_REQUESTED/VERIFIER_ASSIGNED/TASK_WAITING Events | exact recipient set per rule, zero duplicates within the dedupe window, notification loss has zero effect on Task state |
| V-P1-32 | approver eligibility | approval attempts by requester, implementing Agent, alias, unauthorized account, Role below risk; quorum not met | all rejected with stable codes such as `SELF_APPROVAL_FORBIDDEN`; APPROVED only when quorum is met; zero Agent approvals for HIGH and above |

### Exit verdict

V-P1-01~32 all PASS. Event/Policy/Verification/Approval Core Findings are fixed and rechecked regardless of severity.

## 10. Phase 2 Verification — Mattermost/Telegram

### Assignee

Integration Verifier Agent, with an identity different from the Implementer.

### Environment

At least 2 channels in the Mattermost test team, at least 2 Telegram chats/topics, and separate bot credentials.

| Test ID | Subject | Method | PASS criterion |
|---|---|---|---|
| V-P2-01 | Mattermost first input | Task command in a channel | Event created and thread reply |
| V-P2-02 | Renderer latency | 100 Events under the development plan §21.1 normal profile | p95 ≤ 5 s; > 3 s is a warning metric, single maximum ≤ 15 s |
| V-P2-03 | per-channel Bridge | connect channels A/B to Telegram A/B | only exact pairs delivered |
| V-P2-04 | bidirectional | 100 messages from both sides | zero loss/duplicates/echo |
| V-P2-05 | direction policy | attempt the reverse of a one-way Bridge | zero delivery, audit/metrics correct |
| V-P2-06 | thread mapping | root/reply/topic round trips | parent mapping preserved |
| V-P2-07 | loop prevention | altered/re-injected forwarded markers | blocked by hop limit/dedupe |
| V-P2-08 | outage recovery | 10-minute outage of each provider | core continues, exactly-once delivery after recovery |
| V-P2-09 | callback spoof/replay | tampered signature/timestamp/nonce | 401/403, no domain Event |
| V-P2-10 | secret redaction | canary injected via isolated ingress fixture | zero canaries in normalized messages/Events/logs/destinations excluding the raw fixture |
| V-P2-11 | attachment policy | allowed/oversize/MIME/path malicious | only allowed delivered, rest blocked |
| V-P2-12 | admin isolation | Bridge change by unauthorized account | rejected in UI and API |
| V-P2-13 | disable behavior | disable only channel A's Bridge | only A stops, B unaffected |
| V-P2-14 | mapping integrity | message mapping query | source/destination/origin unique and complete |
| V-P2-15 | Bridge delivery latency | 100 deliveries timed at the development plan §21.1 rate of 10 messages/s | end-to-end p95 ≤ 5 s, zero loss/duplicates |
| V-P2-16 | Telegram command policy | Task command from Telegram under the default policy | zero execution (read/reply only); only when allowed per channel, executed per permission mapping |
| V-P2-17 | duplicate Telegram target | connect the same chat/topic to two channels | rejected by default; allowed and recorded only with administrator exception |
| V-P2-18 | channel soft delete | delete after archive, then query references | soft delete only; mapping/Artifact/Document references intact |
| V-P2-19 | custom channel/template | custom channel and user template CRUD/application | 4 default templates preserved; custom/template settings apply independently per channel |
| V-P2-20 | external identity command | the same command by verified-active/unlinked/suspended Telegram users | only the active link executes with Account permissions; others produce zero Task/Event side effects |
| V-P2-21 | external identity collision | link the same provider/user to two Accounts | second link rejected and audited, existing link unchanged |
| V-P2-22 | provider-instance isolation | same external user ID on different bot/provider instances | links apply per instance only, zero cross-Account permission use |
| V-P2-23 | transactional outbox | inject failures between Event insert and outbox insert, and right after provider send | the former rolls back both; the latter yields exactly one destination side effect after replay; Event/Mapping/Outbox states consistent |
| V-P2-24 | command grammar | valid/invalid/help for every resource/verb, prefix-less free text, `<task_id>` omitted inside a thread, commands by unlinked users | valid creates Events; invalid gives ephemeral error with zero side effects; zero free-text command interpretation; omission targets the thread's Task; unlinked users get link/help only |
| V-P2-25 | Task card/thread | Task creation → 10 transitions, 1 sub-Task | one root post edited in place; one thread reply per transition; progress coalesced at 10 s; Artifact link above 16k; sub-Task link card |
| V-P2-26 | interactive action | clicks by authorized/unauthorized users, signature tampering, duplicate clicks | only authorized executes, exactly once; unauthorized rejected + audited; tampered signature 401/403 with zero domain Events |
| V-P2-27 | link challenge | valid/wrong/expired/reused code, 6 failures, `confirm` command path | only valid becomes active; wrong/expired/reused rejected; 15-minute lockout from the 6th; command path becomes pending_admin |
| V-P2-28 | Agent identity display | MCP Agent utterance, override disallowed configuration, display identity injected in Agent payload | override or `[agent-name]` fallback exact; payload injection ignored and audited |
| V-P2-29 | message retention | change retention 365→1 day, legal hold, advance virtual clock | expired Messages DEK-destroyed with tombstones; zero deletions in held channels; provenance shows `REDACTED_BY_RETENTION` |
| V-P2-30 | i18n | instance ko, channel en; cards/errors/Document headings | displayed in the configured language; Event types/error codes/IDs untranslated |
| V-P2-31 | notification delivery | approval/verifier/waiting notifications, mute/digest, Bridge policy | mention/DM/approval channel reached; digest batched hourly; zero when muted; Telegram relay matches Bridge policy |

### Exit verdict

V-P2-01~31 all PASS. Cross-channel delivery, secret exposure, echo loops, and command execution by unlinked external identities are Critical/High and FAIL immediately.

## 11. Phase 3 Verification — Generic Agents

### Assignee

Agent Conformance Verifier Agent, with an identity different from the Implementer.

### Environment

At least the MCP, REST/Webhook, and Mattermost bot adapter types, prepared by adapter type rather than product name.

### 11.1 Adapter Conformance Suite (CS)

V-P3-05 executes CS-01~12 on each of the 3 Adapters; all are mandatory.

| CS ID | Check | PASS criterion |
|---|---|---|
| CS-01 | probe identity stability | identity/capabilities/delivery_modes identical across 3 probes |
| CS-02 | deliver idempotency | same work_item delivered twice yields the same receipt and 1 side effect |
| CS-03 | ack/accept timing | ack within 60 s, accept within 120 s |
| CS-04 | invoke result schema | conforms to expected_result_schema, usage included or `usage_unavailable` reason |
| CS-05 | cancel | ack within 10 s, cleanup within 60 s |
| CS-06 | heartbeat | 30-second interval, capacity reported |
| CS-07 | secret handle non-exposure | zero handle values in logs/messages/results (unsupported Adapters advertise unsupported) |
| CS-08 | correlation preserved | correlation/task/event IDs echoed 100% |
| CS-09 | retry duplicate prevention | zero duplicate side effects on redelivery of the same work_item |
| CS-10 | unsupported declared | calling an unadvertised tool returns `CAPABILITY_UNSUPPORTED` |
| CS-11 | error normalization | stable error codes for 5 injected failure kinds |
| CS-12 | reconnect | un-acked items re-received after disconnect, zero duplicate results |

| Test ID | Subject | Method | PASS criterion |
|---|---|---|---|
| V-P3-01 | Agent register | register 3 types via Web/API | all with unique identity/status |
| V-P3-02 | role create/change | create/modify a custom Role, then run the first authorization request | allow/deny and explicit-deny precedence match the latest committed RoleVersion, zero stale allows |
| V-P3-03 | capability routing | combinations of active/online/membership/capability/capacity/policy and score ties | only the eligible intersection is selected; ties reproduced by ascending agent_id |
| V-P3-04 | unsupported | delegate an unsupported tool | `CAPABILITY_UNSUPPORTED`, zero side effects; one reselection if an eligible fallback exists, else Task WAITING |
| V-P3-05 | adapter conformance | execute §11.1 CS-01~12 | all 3 types pass every CS-01~12 |
| V-P3-06 | idempotent delivery | retry/timeout simulation | zero duplicate side effects |
| V-P3-07 | cancel | cancel a running Task | ack within 10 s and cleanup within 60 s; Event/Task/Adapter states consistent |
| V-P3-08 | suspend/revoke | revoke an active Agent | new requests blocked immediately |
| V-P3-09 | role conflict | allow + deny Roles granted together | explicit deny wins |
| V-P3-10 | channel membership | read/write by a non-member Agent | access denied |
| V-P3-11 | heartbeat/offline | stop 30-second heartbeats, then return | offline within 90 s; active/online within 30 s after the returning heartbeat |
| V-P3-12 | product neutrality | add a new mock adapter | works via plugin/registration only, without core changes |
| V-P3-13 | Web management | full add/edit/suspend paths | same results/audit as the API |
| V-P3-14 | verifier selection | implementer/ineligible Agent as candidate | automatically excluded |
| V-P3-15 | limits enforcement | requests beyond concurrent Task/rate/turn limits | server rejection with audit; requests within limits processed normally |
| V-P3-16 | principal role/alias independence | grant Roles to Human/Agent/service, then pick an alias credential as verifier | same Account-based policy applied; alias/shared-credential verifiers all excluded |
| V-P3-17 | lifecycle Event rebuild | rebuild projection after register/update/activate/suspend/revoke/heartbeat/offline | identical state and lifecycle history hash |
| V-P3-18 | multi-Agent fan-out/join | delegate 3 sub-Tasks in parallel to 3 different Agents and run ALL/ANY/QUORUM each | root/parent/provenance preserved; parent proceeds only when the defined count for each join is met; ALL cannot complete with an unverified required sub-Task |
| V-P3-19 | delegation graph limits | attempt self/ancestor cycles, parent in another Workspace, depth/fan-out/concurrency limit + 1 | all stable errors with zero Task/Event side effects; within limits succeeds |
| V-P3-20 | in-flight reassignment | inject assignee offline/revoke before and during execution | reassigned to an eligible Agent with history preserved where policy allows; zero duplicates of already-started side effects; Task `WAITING` if no candidate |
| V-P3-21 | MCP transport | Streamable HTTP auth (valid/invalid token, mTLS), `work_poll` 30-second long-poll, subscribe, disconnect/reconnect | invalid auth 401 with zero side effects; poll answered within 30 s; un-acked redelivered after reconnect with zero duplicate results |
| V-P3-22 | webhook push | tampered/reused HMAC/timestamp/nonce, retries after endpoint 5xx | tampered/reused rejected; retries per backoff; 1 side effect per receipt |
| V-P3-23 | Mattermost bot adapter | structured work message delivery, valid/malformed replies, assignment of a secret-requiring Task | valid reply becomes work_result; malformed yields ephemeral error with zero side effects; secret-requiring Task excludes the bot from eligibility |
| V-P3-24 | verifier assignment | 3 candidates (2 eligible, 1 ineligible), first candidate silent for 10 minutes, no candidate | best-scored eligible chosen with criteria/evidence delivered; reassigned on non-acceptance; WAITING + Administrator notification when none |
| V-P3-25 | accept timeout re-routing | 120-second non-acceptance, rejection codes, with/without alternative candidates | one reassignment with history revision; WAITING when none; zero duplicate side effects via resume_context |
| V-P3-26 | usage reporting conformance | result/heartbeat usage of the 3 Adapters | cost_units computations match, estimated marked, `usage_unavailable` ratio reported |

### Exit verdict

V-P3-01~26 all PASS. Three different Adapter types must complete the same lifecycle, and sub-Tasks of 3+ Agents must join per policy.

## 12. Phase 4 Verification — Admin, Setup, Secrets

### Assignee

The Security Verifier and Operations Verifier must each be an eligible identity different from the Implementer of their scope. The two specialist roles may be held by the same Verifier when policy allows and capability is proven, with per-area verdicts separated in the final report.

| Test ID | Subject | Method | PASS criterion |
|---|---|---|---|
| V-P4-01 | clean Web setup | Wizard on empty DB/storage | configured/locked within 30 minutes |
| V-P4-02 | setup token | invalid/expired/reused tokens and 6 failures within 15 minutes per IP/token fingerprint | the first three cases 403 with zero bootstrap side effects; 429 for 15 minutes from the 6th request; one redacted AuditEvent per rejection |
| V-P4-03 | setup lock | bootstrap call after completion | 404/403, configuration unchanged |
| V-P4-04 | preflight rollback | process kill right after each dependency step | zero partial CONFIGURED/Owner records, zero disk secrets, one successful bootstrap after re-entry |
| V-P4-05 | settings validation | wrong type/endpoint/permission | rejected before apply |
| V-P4-06 | setting diff/audit | change/rollback | redacted old/new versions linked |
| V-P4-07 | account lifecycle | create/edit/suspend/delete request | permissions/references/audit consistent |
| V-P4-08 | UI/API authz parity | compare direct API and UI actions | no privilege escalation via UI bypass |
| V-P4-09 | admin web security | CSRF/session/CSP/re-auth | critical actions protected |
| V-P4-10 | secret at rest | inspect DB/backups/files | zero plaintext values, keys separated |
| V-P4-11 | scoped lease | requests with wrong Agent/Task/action | all rejected |
| V-P4-12 | TTL/single-use | second resolve of an expired handle and of a single-use handle | each rejected with zero secret bytes; exactly one redacted denial AuditEvent per request |
| V-P4-13 | revoke | Task/Agent/grant revocation | new resolves rejected immediately; existing leases/handles invalidated and cleaned up within 5 s |
| V-P4-14 | secret canary DLP | scan chat/Events/logs/errors/Documents | zero canaries |
| V-P4-15 | LLM exposure | exposure request without approval | rejected, Human approval required |
| V-P4-16 | dashboard truth | inject dependency failures | accurate status/alert within 60 s of probe failure |
| V-P4-17 | backup key separation | credential/key inventory review | backups cannot be decrypted with runtime tokens |
| V-P4-18 | accessibility | axe plus manual keyboard/label/contrast/critical-flow checks | WCAG 2.1 AA automated violations 0, critical keyboard flows 100%, zero blockers |
| V-P4-19 | setup reconfiguration path | maintenance mode + recovery code + MFA re-auth, then attempts at the 29/30-minute boundary and with an ordinary session | only the 29-minute session reconfigures; the 30-minute expiry and ordinary sessions get 403 with configuration unchanged; state LOCKED |
| V-P4-20 | MFA enforcement | TOTP enrollment/login/bypass attempts | System Owner/Administrator cannot perform critical actions without MFA |
| V-P4-21 | break-glass | activation/actions/expiry/termination scenarios | re-auth, time limit, immediate announcement, automatic post-hoc verification Task; Event immutability preserved |
| V-P4-22 | hard delete workflow | single approval, skipped waiting period, direct DELETE attempts | dual approval and waiting period enforced; tombstones/AuditEvents preserved |
| V-P4-23 | audit search/export | search/export by period/actor/action | accurate results, secret redaction preserved |
| V-P4-24 | bootstrap local store | inspect file/permissions/DB migration from no-DB to configured | owner-only; only the token hash exists; zero secrets; only the LOCKED marker remains after migration |
| V-P4-25 | hard delete Event immutability | compare Event dump/hash and key state before/after an approved hard delete | only DEK destruction and display redaction occur; Event bytes/hash unchanged |
| V-P4-26 | principal Role CRUD | create/modify/revoke Roles for Human/Agent/service | reflected in the common Account assignment; UI/API parity and audit consistent |
| V-P4-27 | Setup network boundary | full combination of default bind and HTTPS/TLS, client mTLS, allowlist, valid token | only loopback open by default; remote allowed only when all four conditions hold; every other combination yields zero domain side effects |
| V-P4-28 | Setup persistence order | inject DB/key failures and browser/process restarts at each step | DB→key→Owner/TOTP order; zero pre-DB secrets on disk; zero false "owner created" displays on failure |
| V-P4-29 | hard-delete restore resurrection | backup before deletion → hard delete → restore into an empty environment | zero decryption/reactivation of destroyed DEKs after tombstone reconciliation; Event hashes preserved |
| V-P4-30 | integration preflight persistence | repeat success/wrong endpoint/permission denied for Mattermost/storage/secret provider in Setup and Settings | failures blocked before CONFIGURED; successful settings identical after restart as redacted values/secret references; zero real secret re-display |
| V-P4-31 | secret sidecar | injection/resolve/revoke/host binding/disk and log inspection | env/fd injection succeeds; revoke invalidates and cleans up within 5 s; other-host handles rejected; zero values on disk/logs |
| V-P4-32 | maintenance mode | non-admin writes, scheduler, outbox, exit after entering | 503 + Retry-After; zero due Run claims; outbox drain continues; enter/exit audit and announcement |
| V-P4-33 | Web Approvals queue | HIGH approval without/with re-auth, CRITICAL quorum 2 | rejected without re-auth; APPROVED after re-auth; quorum shortfall shown accurately, same Human twice rejected, zero Agent approvals |

### Exit verdict

V-P4-01~33 all PASS. Secret/Admin/Setup security Findings other than Low cannot pass without risk acceptance, and no risk acceptance is granted during an autonomous run. No human approval is required at this gate; Phase 5 starts automatically.

## 13. Phase 5 Verification — Scheduled Work

### Assignee

Scheduler Verifier Agent, with an identity different from the implementing Agent. The Verifier's own identity is read/test-only without product write permissions; scenario operations such as pause/resume/update/Run now are performed with separate test administrator and ordinary accounts issued for the verification environment.

| Test ID | Subject | Method | PASS criterion |
|---|---|---|---|
| V-P5-01 | cron validation | `*`/list/range/step and field boundary fixtures; names, seconds, `? L W # @`, DOW 7 inputs | only the normative grammar stored; invalid gives stable errors |
| V-P5-02 | next-run preview | 50 standard cron expressions × next 10 | matches the reference computation in UTC/local |
| V-P5-03 | timezone | different IANA timezones | correct regardless of server timezone |
| V-P5-04 | DST spring forward | non-existent local time | zero Runs/Tasks, `DST_GAP` reason in preview/history |
| V-P5-05 | DST fall back | the two UTC instants of a duplicated local time | same occurrence_key, 1 Run at the first UTC instant, matches preview |
| V-P5-06 | unique Run | dual planners materialize the same occurrence concurrently | 1 row per `(schedule_id, occurrence_key)` |
| V-P5-07 | dual runner | two runners claim concurrently | only one runner claims/creates the Task |
| V-P5-08 | idempotent Task | retry/crash after Task creation | zero duplicate Tasks/side effects |
| V-P5-09 | FORBID concurrency | previous Run still running | new Run `status=SKIPPED`, `error_code=SKIPPED_CONCURRENCY` |
| V-P5-10 | ALLOW concurrency | previous Run still running | two Runs execute/track independently |
| V-P5-11 | REPLACE concurrency | previous Run cancelled normally and unresponsive to cancel, each | normal case: new Run after cleanup; unconfirmed after 60 s: new Run `status=SKIPPED`, `error_code=SKIPPED_REPLACE_CANCEL_TIMEOUT` |
| V-P5-12 | missed SKIP | restart after server stop | missed occurrences not executed |
| V-P5-13 | missed RUN_ONCE | several missed occurrences | only the most recent occurrence, exactly once, with its original scheduled_for |
| V-P5-14 | limited backfill | more misses than window/limit | only occurrences within the window executed oldest first up to the limit, with a warning |
| V-P5-15 | permission recheck | due after principal permission revocation | `status=SKIPPED`, `error_code=SKIPPED_POLICY`, zero Tasks |
| V-P5-16 | suspended Agent | due after fixed Agent suspension | without fallback policy `status=SKIPPED`, `error_code=SKIPPED_AGENT_UNAVAILABLE`; capability query selects only eligible substitutes |
| V-P5-17 | Secret lease | per-Run secret reference use | short lease, revoked after end, zero leakage |
| V-P5-18 | Approval | high-risk recurring action | only per-Run or validity/count-limited Schedule approvals allowed; zero execution without or after exhaustion |
| V-P5-19 | retry/backoff | 3 transient failures and a permanent failure | at most 3 attempts at 1/5/25 s with allowed jitter; permanent FAILED after 1 attempt |
| V-P5-20 | timeout/cancel | max duration exceeded | cancel ack within 10 s and cleanup within 60 s, or TIMED_OUT; defined Events and lease/secret cleanup |
| V-P5-21 | Run now authz | execution by unauthorized/authorized accounts | the former rejected, the latter creates a separate manual Run |
| V-P5-22 | lifecycle/update | DRAFT→ENABLED↔PAUSED→DISABLED and version changes | invalid transitions rejected; existing Run version/hash unchanged; only new occurrences use the current version |
| V-P5-23 | channel notice | success/fail/skip/late | matches the designated Mattermost channel and Bridge policy |
| V-P5-24 | restart recovery | process kill right after claim | exactly one runner recovers within configured lease expiry + 2× poll interval, zero duplicate Tasks |
| V-P5-25 | metrics/history | compare state with the dashboard | due/run/lag/error values match |
| V-P5-26 | shell rejection | command/script template attempts | rejected by schema/policy |
| V-P5-27 | start delay p95 | measure actual start vs scheduled_for for many Runs under normal load | p95 ≤ 60 s; alert fires above |
| V-P5-28 | budget enforcement | set limit 100, then requests in units of 99/100/101 | 99/100 allowed; 101 rejected before the next side effect with `BUDGET_EXCEEDED` and alert |
| V-P5-29 | DOM/DOW OR | reference fixtures restricting both fields | preview/Runs match Vixie OR semantics |
| V-P5-30 | Schedule Approval race | two runners consume a max-use=1 approval concurrently | 1 consumption, 1 execution, used_count consistent |
| V-P5-31 | pending/running Run cancel | cancel 1 pending and 1 safely cancellable running Run | the former CANCELLED immediately, the latter REQUESTED→CANCELLED with defined Events and cleanup |
| V-P5-32 | terminal Run cancel | cancel succeeded/failed/timed-out Runs | conflict error; existing state/Events/history unchanged |
| V-P5-33 | version snapshot pin | modify Schedule action/budget/channel after Run materialization | the Run executes the original version with only current authz/secret rechecked; new Runs use the new version |
| V-P5-34 | manual retry | repeatedly retry a terminal failed Run with the same idempotency key | exactly one new Run with `retry_of_run_id`; original/attempts/history unchanged |
| V-P5-35 | manual Run isolation | scheduled Run and Run now in the same minute concurrently | 1 scheduled occurrence and 1 manual Run; zero duplicates on idempotent retries of each |
| V-P5-36 | Schedule subject activation | schedule/run Approval and Artifact links after the Phase 5 migration | FKs/handlers active; wrong workspace/target rejected; bounded consume atomicity preserved |
| V-P5-37 | Run budget settlement | per-Run/daily cost_units limits with usage reports | usage_records Run aggregation matches; daily overrun makes the new Run `SKIPPED` with `BUDGET_EXCEEDED` and an alert |

### Exit verdict

V-P5-01~37 all PASS. Duplicate execution, snapshot changes, execution after permission revocation, secret exposure, and arbitrary shell execution are High/Critical and FAIL immediately. The normal load for V-P5-27 is fixed at Human 50, Agents 20, Channels 100, 100 active Schedules, due ≤ 20/min, 2 runners, DB CPU < 70%.

## 14. Phase 6 Verification — Collaboration/Documentation

### Assignee

Workflow/Documentation Verifier Agent.

| Test ID | Subject | Method | PASS criterion |
|---|---|---|---|
| V-P6-01 | Approval scope | reuse for another Task/action/resource | rejected |
| V-P6-02 | high-risk gate | side effect without approval | zero execution |
| V-P6-03 | Brainstorm limits | exceed turn/depth/same-Agent limits | pause/guidance requested |
| V-P6-04 | Decision/Taskify | create Tasks from a Decision | bidirectional provenance |
| V-P6-05 | Artifact integrity | upload/readback/hash | hash matches, ACL preserved |
| V-P6-06 | malicious Artifact | path/MIME/size/malware fixtures | blocked with redacted audit |
| V-P6-07 | Task document | automatic draft for a completed Task | 100% of mandatory sections present |
| V-P6-08 | Brainstorm document | automatic draft for a closed session | includes arguments/alternatives/decisions/limitations |
| V-P6-09 | Schedule document | automatic draft of Run/period results | includes Run status/Tasks/Artifacts/limitations |
| V-P6-10 | process accuracy | compare Event and document sequences | zero omissions/distortions of important Events |
| V-P6-11 | resources | combinations of Agent/model/tool/time/token/cost/artifact sources | every field has a value or a standard `UNAVAILABLE_<REASON>`; zero omissions |
| V-P6-12 | verification section | compare report and document | result/findings/residual risk consistent |
| V-P6-13 | redaction | isolated raw secret/PII canary source | zero canaries in canonical/published documents and logs excluding the raw fixture |
| V-P6-14 | provenance | check every source link/hash | zero broken/missing |
| V-P6-15 | Git publisher | publish/update/verify/archive | version/checksum consistent |
| V-P6-16 | publisher outage | destination down | canonical preserved, exactly once after recovery |
| V-P6-17 | manual correction | factual correction | new version, original and reason kept |
| V-P6-18 | publish authority | publish by an unauthorized Agent | rejected |
| V-P6-19 | closure gate | close a Task without the latest PASSED Verification or without a FINALIZED Document, each | `COMPLETION_PREREQUISITE_MISSING` if either is missing, state unchanged |
| V-P6-20 | document generation rate | 20 closed Tasks, 20 Brainstorms, 20 Runs (60 total) | ≥ 19/20 automatic drafts per type, a stable reason code for every failure |
| V-P6-21 | optional connector | BookStack or Wiki.js reference connector publish/update/verify | contract tests pass, content matches canonical |
| V-P6-22 | Approval expiry | execution attempt with an expired Approval | zero execution, `EXPIRED` state and non-reusability confirmed |
| V-P6-23 | document two-stage lifecycle | compare draft/finalized content and versions before/after verification | no final verdict in the draft; the finalized new version after PASSED contains results and residual risks; draft immutable |
| V-P6-24 | failed/blocked attempt document | FAILED and BLOCKED VerificationRuns | ATTEMPT_FINALIZED preserved; Task completion/publish gate closed; FINALIZED only after PASSED on recheck |
| V-P6-25 | generic ArtifactLink | link Artifacts to Task/ScheduleRun/Brainstorm/Decision and attempt wrong type/id/workspace | only valid subjects whose Phase has arrived link with ACL; wrong subjects give stable errors with zero side effects |
| V-P6-26 | Brainstorm turn engine | 3 Agents round-robin, consecutive same-Agent attempt, turn/budget/time exceeded, facilitator resume | order reproducible; consecutive utterance rejected; PAUSED + guidance request on overrun; resumes after the resume Event |
| V-P6-27 | summary/decision/taskify | run summarize/decide/taskify | summarizer prefers a non-participant Agent; zero posting before facilitator approval; Decision→Task bidirectional provenance with mandatory criteria |
| V-P6-28 | narrative citation linter | sentences without citations, non-existent IDs, figures contradicting the skeleton | all rejected; skeleton-only draft valid; zero overwrites of skeleton facts by narrative |
| V-P6-29 | Mattermost approval buttons | LOW/MEDIUM button approval, HIGH button, self-approval button | LOW/MEDIUM APPROVED; HIGH shows web re-auth guidance and does not approve; self rejected + audited |

### Exit verdict

V-P6-01~29 all PASS. Whether sources and facts are reconstructable takes priority over how polished the documents look.

## 15. Phase 7 Verification — Release

### Assignee

Release Verifier, Security Verifier, and Operations Verifier (all Codex). The Deployment Approver (Human) decides only on deployment after the final report (V-P7-18).

| Test ID | Subject | Method | PASS criterion |
|---|---|---|---|
| V-P7-01 | clean production-like install | empty host/volumes | succeeds per runbook |
| V-P7-02 | full E2E | Mattermost→Schedule/Agent→Approval→Artifact→Document→Verification | 20 consecutive successes |
| V-P7-03 | load | 3× the development plan §21.1 normal profile for 30 minutes | zero Event/Run loss/duplicates, 5xx < 1%, read/write p95 ≤ 300/500 ms |
| V-P7-04 | 24 h soak | Bridge/heartbeat/scheduler/lease | zero leaks/duplicates/stuck |
| V-P7-05 | dependency outage | 10-minute failure and recovery of each provider except the DB | core writes continue, related outbox preserved, DEGRADED within 60 s, exactly-once drain within 5 minutes of recovery |
| V-P7-06 | DB outage | 10-minute outage/recovery | write API 503 and readiness fail within 30 s, zero successful responses, zero Event/hash corruption after recovery |
| V-P7-07 | backup restore | restore into an empty environment | within RPO 24 h/RTO 4 h; ScheduleVersion/Run/Event/Approval/ExternalIdentity hash/state equal |
| V-P7-08 | projection rebuild | full Event replay | identical snapshot |
| V-P7-09 | upgrade | previous version → v8 target | data/settings/secret refs/Schedules preserved |
| V-P7-10 | rollback/forward fix | one app failure and one irreversible migration scenario | app rollback or forward-fix completes within RTO 4 h; schema/data hashes and migration ledger consistent |
| V-P7-11 | security scan | SAST/dependency/container/dynamic | zero high/critical |
| V-P7-12 | credential rotation | sequential rotation of Agent/Mattermost/Telegram/admin credentials | old credentials rejected within 60 s of confirming the new ones, zero message/Task loss or duplicates |
| V-P7-13 | incident tabletop | leak/NAS full/Bridge loop/Scheduler storm/DB restore | 100% of detection/isolation/recovery/post-verification checkpoints per runbook, zero real secrets used |
| V-P7-14 | observability | synthetic failure set | defined alerts fire within 60 s; dashboard state/cause/correlation ID 100% consistent |
| V-P7-15 | SBOM/image | inspect release artifacts | immutable digest/SBOM/signature |
| V-P7-16 | evidence archive | retrieve every Phase report | zero missing, zero secrets |
| V-P7-17 | residual risk | review open Findings | owner/deadline/acceptor clear |
| V-P7-18 | deployment approval | final development report (development plan §27A) delivered and the user's explicit deployment decision recorded | explicit approval recorded before any deployment action; zero deployment without it |
| V-P7-19 | backup retention | move daily/weekly/monthly boundaries with an injectable virtual clock and accelerated scheduler, observe the backup catalog | retention/expiry per settings without real long waits; identical to the production path after clock reset |
| V-P7-20 | deletion resurrection | restore/resolve attempts with a pre-hard-delete backup and key material | service closed until tombstone reconciliation; zero decryption/reactivation of destroyed keys afterwards |
| V-P7-21 | runbook completeness | inspect the 7 runbooks and critical alert links | each runbook has detection/isolation/recovery/post-verification procedures; every critical alert links to a runbook |
| V-P7-22 | Human-path acceptance | Task creation→delegation→approval→verification→document viewing using Mattermost only, 10 times | 10 consecutive successes; cards/threads/notifications correct at every step |

## 16. Final Acceptance Criteria

Agent-Colab v8 is accepted only when all of the following hold.

- [ ] Phases 0–7 PASSED by eligible Verifiers different from the Implementer of each scope. The same Verifier may cover several Phases as long as no credential/alias is shared.
- [ ] Every mandatory Test is linked to commit/image/environment/evidence.
- [ ] No core role is fixed to a specific Agent product or machine.
- [ ] At least 3 Agent Adapter types passed the conformance suite.
- [ ] Parallel sub-Tasks of 3+ Agents behaved per ALL/ANY/QUORUM join, delegation limits, cycle prevention, and failure reassignment rules.
- [ ] Mattermost works end to end as the default conversation channel.
- [ ] Telegram Bridges of at least 2 Mattermost channels operate independently.
- [ ] Zero echo/duplicates/cross-delivery in the 100-message Bridge test.
- [ ] Destination side effects occurred exactly once despite transaction failures and replays of Events and the Bridge outbox.
- [ ] Accounts, Agents, Roles, Channel Bridges, and key settings are managed in the web console.
- [ ] A clean environment was initialized through the Setup Wizard and re-run attacks were blocked.
- [ ] Pre-DB bootstrap state stored only the token hash and non-secret pointers in an owner-only sealed file and locked after DB migration.
- [ ] Setup is loopback by default; remote access satisfies prior HTTPS/TLS, client mTLS, allowlist, and a valid token, and commits in DB→key→Owner/TOTP order.
- [ ] Roles of Humans, Agents, and services are created, changed, and revoked through the common Account principal model.
- [ ] Mattermost/Telegram commands execute only for Accounts with a verified active ExternalIdentityLink.
- [ ] No secret canary was found in message/Event/log/Document/backup plaintext.
- [ ] Task, Brainstorm, and Schedule Run closing documents have the mandatory structure and provenance.
- [ ] Pre-verification drafts and post-verification finalized versions are separated and past versions are immutable.
- [ ] cron previews match reference results across IANA timezones and DST fixtures.
- [ ] No duplicate Runs/Tasks for the same scheduled time under dual schedulers, crash/retry, and server restarts.
- [ ] DST-fold occurrence keys, immutable ScheduleVersions, lifecycle status, and manual/retry Run linkage were verified.
- [ ] Concurrency and missed-run policies work exactly for FORBID/ALLOW/REPLACE and SKIP/RUN_ONCE/BACKFILL_LIMITED.
- [ ] Pending/running Run cancel states and Events are recorded as defined, and terminal Runs are never changed by cancel.
- [ ] Scheduled work of revoked execution principals and unapproved high-risk scheduled work did not run.
- [ ] Arbitrary shell commands cannot be registered through Schedule templates.
- [ ] Self-verification PASS by the implementing Agent is rejected systemically.
- [ ] REST and MCP use the same command handlers, Policy, schema, and idempotency results with no bypass path.
- [ ] Projection rebuild and backup restore produce identical state/hashes.
- [ ] Approval/Schedule/Agent aggregates, not only Tasks, are rebuilt from sequence/hash chains.
- [ ] After an approved hard delete, sensitive content is undecryptable and Event bytes and hashes are unchanged.
- [ ] Restoring a pre-hard-delete backup does not resurrect deleted content after key tombstone reconciliation.
- [ ] 20 consecutive full E2E successes with zero Event/Run loss.
- [ ] Default RPO 24 h/RTO 4 h and the defined normal/peak profiles are met.
- [ ] Zero high/critical security Findings and zero unapproved high-risk executions.
- [ ] Break-glass and hard delete were performed only through the defined workflows with post-hoc verification.
- [ ] Zero executions exceeding Agent Limits and Schedule budgets.
- [ ] Bridge delivery p95 5 s, Schedule start delay p95 60 s, and document generation rate 95% metrics are met with measurement evidence.
- [ ] Mattermost commands, cards, buttons, and identity display behave per development plan §7A with zero free-text command interpretations.
- [ ] The 3 Adapters performed exactly-once delivery and results through the work item protocol (inbox/push) and passed CS-01~12.
- [ ] Every Task has acceptance criteria, and automatic Verifier assignment/reassignment was verified.
- [ ] Approver eligibility, self-approval ban, quorum, and expiry escalation were verified.
- [ ] Usage reporting and cost_units budgets are enforced for Agents, Channels, and Schedules.
- [ ] The Brainstorm turn engine, summary, decision, taskify, and document narrative citations were verified.
- [ ] The Human-only path acceptance succeeded 10 times consecutively.
- [ ] The final development report was delivered and the user explicitly approved deployment.

## 17. Failure Handling

A Finding must include:

- Finding ID, severity, affected requirement/Test
- target commit/environment
- minimal reproduction steps with actual/expected results
- evidence references
- security/data/user impact
- fix completion conditions

After the implementing Agent fixes, the same Verifier or a new eligible Verifier rechecks. Scopes that had a Critical Finding receive a cross-review by a new Security Verifier where possible.

## 18. Verification Automation

- CI uploads test results as VerificationRun drafts.
- The server automatically checks implementer/verifier identity, target commit, and criteria version.
- Verifier Agents use read-only checkouts/worktrees and separate DBs/schemas.
- UI/provider tests use test tenants/bots/chats and never production secrets.
- The PASS API rejects missing mandatory Test evidence.
- The Phase dashboard clearly shows NOT_RUN/BLOCKED/expired evidence.
- Scheduler, DST, missed-run, and retention tests use an injectable `Clock` and fixed tzdb fixtures and never depend on real long waits.
- In v8 the pipeline driver (Claude Code) invokes Codex for every Phase without waiting for a human, stores the report unmodified, and proceeds only on PASSED.

## 19. Requirements for Verification Agents

A Verifier Agent must prove the following Capabilities at registration.

- repository read and test execution
- structured report and evidence upload
- a conformance profile proving technical understanding of the Phase
- no product write permission, or its temporary removal during verification
- no secret value read permission
- preservation of deterministic commands/outputs

Security Verifiers additionally need threat model, authz negative test, and secret/DLP test Capabilities. Operations Verifiers need deploy, backup/restore, and monitoring test Capabilities.

## 20. Ten Verification Preparations to Start Immediately

- [ ] 1. Build the requirement ↔ V-P0~V-P7 Test ID traceability matrix as a CI artifact.
- [ ] 2. Implement implementer/verifier identity separation and alias/credential detection rules.
- [ ] 3. Create JSON Schemas for the Evidence Manifest and Verifier Report.
- [ ] 4. Create the PASS/FAIL/BLOCKED API and the immutable revision schema.
- [ ] 5. Create the clean-environment Phase 0 validation runner.
- [ ] 6. Create Event/Policy/Projection negative test fixtures.
- [ ] 7. Prepare the Mattermost–Telegram 2-channel/2-chat sandbox and loop canaries.
- [ ] 8. Prepare conformance fixtures for 3 different Agent Adapters.
- [ ] 9. Create Schedule fixtures for cron/timezone/DST/dual-runner/restart and connect the secret redaction scanner.
- [ ] 10. Fix the storage location of final E2E, backup restore, and release evidence including Schedules.
- [ ] 11. Prepare command grammar, work item, usage, and permission catalog fixtures, the CS-01~12 runner, and the Human-path scenario script.

## 21. Change Management

Deleting Tests, loosening PASS criteria, independence exceptions, and severity downgrades are not ordinary document edits. They require:

1. an ADR with reasons and risks
2. a diff of old/new criteria
3. review by another Verifier Agent
4. System Owner approval
5. regression of the affected Phases

None of these changes is made during an autonomous run; if a criterion cannot be met, the Phase is FAILED or BLOCKED and reported. When the validation document version changes, in-progress VerificationRuns keep the criteria in force at their start, while security-critical tightening rules have their scope decided by the System Owner.
