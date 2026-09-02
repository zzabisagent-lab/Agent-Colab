# Agent-Colab — Progress

Resume point for any new session. Baseline: `docs/baseline/` (v8). Rules: `AGENTS.md`, ADRs in `docs/adr/`.

## Current phase: 4 (Admin, Setup, Secrets) — branch `phase-4`

### Phase 4 package status

| ID | Work | Size | Prereq | Status | SELF evidence |
|---|---|---|---|---|---|
| P4-01 | Account Admin | M | P1-05 | NOT_STARTED | — |
| P4-02 | Operations/Audit dashboard | M | P1-07 | NOT_STARTED | — |
| P4-03 | Setup Wizard | L | P0-09, P4-05 | NOT_STARTED | — |
| P4-04 | Settings | M | P4-02 | NOT_STARTED | — |
| P4-05 | local Secret provider | M | P1-01 | NOT_STARTED | — |
| P4-06 | Grant/Lease/Broker | M | P4-05 | NOT_STARTED | — |
| P4-07 | Adapter injection | M | P4-06, P3-03 | NOT_STARTED | — |
| P4-08 | admin security | M | P4-02 | NOT_STARTED | — |
| P4-09 | MFA/OIDC | M | P1-05 | NOT_STARTED | — |
| P4-10 | break-glass | M | P4-09 | NOT_STARTED | — |
| P4-11 | hard delete workflow | L | P4-05, P1-02 | NOT_STARTED | — |
| P4-12 | Secret sidecar | L | P4-06, P4-07 | NOT_STARTED | — |
| P4-13 | maintenance mode | S | P4-02 | NOT_STARTED | — |
| P4-14 | Web Approvals queue and re-authentication | M | P1-08, P4-09 | NOT_STARTED | — |

Phase 4 progress (size-weighted): 0 / 33

## Phase 3 (PASSED) — branch `phase-3`, tag `phase-3-passed`

### Phase 3 package status

| ID | Work | Size | Prereq | Status | SELF evidence |
|---|---|---|---|---|---|
| P3-01 | Agent Registry | M | P1-05 | IMPLEMENTED | SELF-V-P3-01/08/11/17 |
| P3-02 | Role/Capability | M | P1-03 | IMPLEMENTED | SELF-V-P3-02/09/16 |
| P3-03 | Adapter SDK/contract | M | P1-12 | IMPLEMENTED | SELF-V-P3-05/06/07 |
| P3-04 | default Adapters (MCP, REST/Webhook, Mattermost bot) | M | P3-03, P3-10, P3-11, P3-12 | IMPLEMENTED | SELF-V-P3-05/12 |
| P3-05 | conformance suite CS-01~12 | M | P3-03 | IMPLEMENTED | SELF-V-P3-05 |
| P3-06 | routing | M | P3-01, P3-02 | IMPLEMENTED | SELF-V-P3-03/04/10 |
| P3-07 | Agent Admin UI | M | P3-01 | IMPLEMENTED | SELF-V-P3-13 |
| P3-08 | Limits enforcement | M | P1-14, P3-01 | IMPLEMENTED | SELF-V-P3-15 |
| P3-09 | multi-Agent orchestration | L | P3-06 | IMPLEMENTED | SELF-V-P3-18/19/20 |
| P3-10 | MCP server transport | M | P1-07, P1-12, P0-11 | IMPLEMENTED | SELF-V-P3-21 |
| P3-11 | Webhook push delivery | M | P1-12, P0-11 | IMPLEMENTED | SELF-V-P3-22 |
| P3-12 | Mattermost bot adapter delivery | M | P1-12, P2-11 | IMPLEMENTED | SELF-V-P3-23 |
| P3-13 | Verifier assignment engine | M | P1-06, P3-06 | IMPLEMENTED | SELF-V-P3-14/24 |
| P3-14 | accept timeout/re-routing | S | P3-06, P1-12 | IMPLEMENTED | SELF-V-P3-25 |
| P3-15 | usage reporting conformance | S | P1-14, P3-03 | IMPLEMENTED | SELF-V-P3-26 |

Phase 3 progress (size-weighted): 32 / 32 — Codex VR-P3-001 PASSED (26/26) on revision 1

## Phase 2 (PASSED) — branch `phase-2`, tag `phase-2-passed`

### Phase 2 package status

| ID | Work | Size | Prereq | Status | SELF evidence |
|---|---|---|---|---|---|
| P2-01 | Mattermost provider | M | P1-07, P0-10 | IMPLEMENTED | SELF-V-P2-01, SELF-V-P2-19 |
| P2-02 | Channel/external identity config | M | P2-01, P1-05 | IMPLEMENTED | SELF-V-P2-19, SELF-V-P2-21, SELF-V-P2-22 |
| P2-03 | Renderer/outbox | M | P2-01 | IMPLEMENTED | SELF-V-P2-02, SELF-V-P2-23 |
| P2-04 | Telegram provider | M | P1-07, P0-13 | IMPLEMENTED | SELF-V-P2-09, SELF-V-P2-11 |
| P2-05 | per-channel Bridge | M | P2-03, P2-04 | IMPLEMENTED | SELF-V-P2-03/05/06/13/14/17 |
| P2-06 | dedupe/loop/retry | M | P2-05 | IMPLEMENTED | SELF-V-P2-04/07/08/10/15 |
| P2-07 | Bridge Admin UI | S | P2-05 | IMPLEMENTED | SELF-V-P2-12 |
| P2-08 | Telegram command policy | S | P2-05, P2-10 | IMPLEMENTED | SELF-V-P2-16, SELF-V-P2-20 |
| P2-09 | Channel lifecycle | S | P2-02 | IMPLEMENTED | SELF-V-P2-18 |
| P2-10 | Command Router | M | P2-01, P1-07 | IMPLEMENTED | SELF-V-P2-24 |
| P2-11 | Task card/thread Renderer | M | P2-03, P2-10 | IMPLEMENTED | SELF-V-P2-25 |
| P2-12 | Interactive actions | S | P2-11 | IMPLEMENTED | SELF-V-P2-26 |
| P2-13 | Mattermost link challenge | S | P2-02, P2-10 | IMPLEMENTED | SELF-V-P2-27 |
| P2-14 | Agent identity display | S | P2-11 | IMPLEMENTED | SELF-V-P2-28 |
| P2-15 | Message ingestion/retention | M | P2-03 | IMPLEMENTED | SELF-V-P2-29 |
| P2-16 | i18n | S | P2-10 | IMPLEMENTED | SELF-V-P2-30 |
| P2-17 | Notification providers | S | P1-13, P2-01 | IMPLEMENTED | SELF-V-P2-31 |

Phase 2 progress (size-weighted): 33 / 33 — Codex VR-P2-002 PASSED (32/32) after revision 1 FAILED on F-P2-001/002/003 (fixed in revision 2)

## Phase 1 (PASSED) — branch `phase-1`, tag `phase-1-passed`

### Phase 1 package status

| ID | Work | Size | Prereq | Status | SELF evidence |
|---|---|---|---|---|---|
| P1-01 | DB migration/roles | M | P0-03 | IMPLEMENTED | SELF-V-P1-05, SELF-V-P1-25 |
| P1-02 | aggregate Event append | L | P1-01 | IMPLEMENTED | SELF-V-P1-01/02/03/04/06/21 |
| P1-03 | Policy Engine | M | P1-01, P0-12 | IMPLEMENTED | SELF-V-P1-07 |
| P1-04 | Task state/projection | M | P1-02 | IMPLEMENTED | SELF-V-P1-09/10/27 |
| P1-05 | identity/service token/external link core | M | P1-01 | IMPLEMENTED | SELF-V-P1-08, SELF-V-P1-23 |
| P1-06 | VerificationRun core | M | P1-02, P1-05 | IMPLEMENTED | SELF-V-P1-12/13/14/24 |
| P1-07 | REST/MCP/SSE | L | P1-02, P1-03 | IMPLEMENTED | SELF-V-P1-11, SELF-V-P1-26 |
| P1-08 | Approval Core | L | P1-02, P1-03 | IMPLEMENTED | SELF-V-P1-15/16/22/32 |
| P1-09 | Artifact Core | M | P1-02 | IMPLEMENTED | SELF-V-P1-17 |
| P1-10 | Document lifecycle Core | M | P1-04, P1-06, P1-09 | IMPLEMENTED | SELF-V-P1-18/19/20 |
| P1-11 | Task acceptance criteria | S | P1-04 | IMPLEMENTED | SELF-V-P1-28 |
| P1-12 | Work item inbox core | M | P1-02, P1-04 | IMPLEMENTED | SELF-V-P1-29 |
| P1-13 | Notification core | S | P1-02 | IMPLEMENTED | SELF-V-P1-31 |
| P1-14 | Usage/Budget core | M | P1-02 | IMPLEMENTED | SELF-V-P1-30 |

Phase 1 progress (size-weighted): 33.5 / 33.5

Latest verification: **VR-P1-001: PASSED** (Codex, 2026-09-02, `verification/phase-1/VR-P1-001.yaml`, target 1244dda). 32/32 Tests PASS.

## Phase 0 (PASSED) — branch `phase-0`, tag `phase-0-passed`

### Package status

| ID | Work | Size | Prereq | Status | SELF evidence |
|---|---|---|---|---|---|
| P0-01 | repo/branch/CI skeleton | S | — | IMPLEMENTED | SELF-V-P0-03 (pending clean-clone run) |
| P0-02 | ADRs and requirement IDs | S | — | IMPLEMENTED | SELF-V-P0-01/02/10/14/15/20 |
| P0-03 | schema/policy/Event contract | M | P0-01 | IMPLEMENTED | SELF-V-P0-05/06/13 |
| P0-04 | Compose dev stack | S | P0-01 | IMPLEMENTED | SELF-V-P0-04 attempt 3: all 4 services healthy from empty volumes |
| P0-05 | Setup state skeleton | S | P0-03 | IMPLEMENTED | SELF-V-P0-12 |
| P0-06 | threat model | S | P0-02 | IMPLEMENTED | SELF-V-P0-08/09 |
| P0-07 | verification harness | S | P0-03 | IMPLEMENTED | SELF-V-P0-07 |
| P0-08 | Schedule contract | M | P0-03 | IMPLEMENTED | SELF-V-P0-11 |
| P0-09 | pre-DB bootstrap store contract | M | P0-05 | IMPLEMENTED | SELF-V-P0-12 |
| P0-10 | Mattermost interaction contract and spike | M | P0-03 | IMPLEMENTED | SELF-V-P0-16; spike: docs/protocol/mattermost-spike.md |
| P0-11 | Agent work-item/usage contract and MCP spike | M | P0-03 | IMPLEMENTED | SELF-V-P0-17; spike: docs/protocol/mcp-spike.md |
| P0-12 | permission/risk catalog | S | P0-03 | IMPLEMENTED | SELF-V-P0-18 |
| P0-13 | Telegram API spike | S | — | IMPLEMENTED | SELF-V-P0-19; docs/protocol/telegram-spike.md |
| P0-14 | plan operating baseline | S | P0-02 | IMPLEMENTED | SELF-V-P0-20 (attempt 2), docs/plan-baseline.md |

Phase progress (size-weighted, S=1 M=2.5 L=5): 20.5 / 20.5

### Latest verification result

**VR-P0-003: PASSED** (Codex, 2026-09-02, `verification/phase-0/VR-P0-003.yaml`, target f11e376). 20/20 Tests PASS, zero findings. Earlier: r001 aborted (sandbox), r002 FAILED (3 findings, all fixed).

### Open findings / blockers

- F-P0-002-01/02/03 fixed and rechecked PASS in r003.

- B-001 resolved 2026-09-02: Docker 29.1 + Compose 2.40 installed by the user (this shell uses `sg docker -c`).
- B-002 resolved 2026-09-02: TELEGRAM_BOT_TOKEN/TEST_CHAT_A/B provided in .env; P0-13 spike completed.
- B-003 (root-only, optional) AppArmor blocks unprivileged user namespaces, so Codex's process sandbox cannot run; verification runs unsandboxed in an isolated worktree (ADR-0005 addendum).

### Next step

Phase 4 on branch `phase-4` (foundation: secret provider interface, re-auth seam, migration slots 0012-0015; forks: secrets/broker/injection, setup/settings/maintenance, MFA/admin security/break-glass/approvals queue, account admin/dashboard/hard delete, sidecar; console screens + a11y by the parent).

## Phase history

| Phase | Result | Report | Tag |
|---|---|---|---|
| 3 | PASSED (revision 1) | verification/phase-3/VR-P3-001.yaml | phase-3-passed |
| 2 | PASSED (revision 2; revision 1 FAILED F-P2-001/002/003) | verification/phase-2/VR-P2-002.yaml | phase-2-passed |
| 0 | PASSED | verification/phase-0/VR-P0-003.yaml | phase-0-passed |
| 1 | PASSED | verification/phase-1/VR-P1-001.yaml | phase-1-passed |
