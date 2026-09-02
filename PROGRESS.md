# Agent-Colab — Progress

Resume point for any new session. Baseline: `docs/baseline/` (v8). Rules: `AGENTS.md`, ADRs in `docs/adr/`.

## Current phase: 0 (Baseline and Bootstrap) — branch `phase-0`

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

Phase 1 on branch `phase-1` (P1-01 first; then P1-02/03/05 in parallel).

## Phase history

| Phase | Result | Report | Tag |
|---|---|---|---|
| 0 | PASSED | verification/phase-0/VR-P0-003.yaml | phase-0-passed |
