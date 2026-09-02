# Agent-Colab — Progress

Resume point for any new session. Baseline: `docs/baseline/` (v8). Rules: `AGENTS.md`, ADRs in `docs/adr/`.

## Current phase: 2 (Mattermost/Telegram) — branch `phase-2`

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

Phase 2 progress (size-weighted): 33 / 33 — all packages implemented; full suite + lint + check-docs green; Codex verification pending (see Next step)

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

Phase 2: run Codex verification revision 1 on branch `phase-2` (`tools/run_verification.py --phase 2 --revision 1 --commit <sha> --no-sandbox --secret-env .env --secret-env ~/.local/opt/mattermost/.spike-credentials`); on PASSED merge to `main`, tag `phase-2-passed`, start Phase 3 (P3-01 first).

## Phase history

| Phase | Result | Report | Tag |
|---|---|---|---|
| 0 | PASSED | verification/phase-0/VR-P0-003.yaml | phase-0-passed |
| 1 | PASSED | verification/phase-1/VR-P1-001.yaml | phase-1-passed |
