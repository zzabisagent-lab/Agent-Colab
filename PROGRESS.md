# Agent-Colab — Progress

Resume point for any new session. Baseline: `docs/baseline/` (v8). Rules: `AGENTS.md`, ADRs in `docs/adr/`.

## Current phase: 0 (Baseline and Bootstrap) — branch `phase-0`

### Package status

| ID | Work | Size | Prereq | Status | SELF evidence |
|---|---|---|---|---|---|
| P0-01 | repo/branch/CI skeleton | S | — | IMPLEMENTED | SELF-V-P0-03 (pending clean-clone run) |
| P0-02 | ADRs and requirement IDs | S | — | IMPLEMENTED | SELF-V-P0-01/02/10/14/15/20 |
| P0-03 | schema/policy/Event contract | M | P0-01 | IMPLEMENTED | SELF-V-P0-05/06/13 |
| P0-04 | Compose dev stack | S | P0-01 | IMPLEMENTED (static) | SELF-V-P0-04 static checks only; runtime health NOT_RUN (B-001) |
| P0-05 | Setup state skeleton | S | P0-03 | IMPLEMENTED | SELF-V-P0-12 |
| P0-06 | threat model | S | P0-02 | IMPLEMENTED | SELF-V-P0-08/09 |
| P0-07 | verification harness | S | P0-03 | IMPLEMENTED | SELF-V-P0-07 |
| P0-08 | Schedule contract | M | P0-03 | IMPLEMENTED | SELF-V-P0-11 |
| P0-09 | pre-DB bootstrap store contract | M | P0-05 | IMPLEMENTED | SELF-V-P0-12 |
| P0-10 | Mattermost interaction contract and spike | M | P0-03 | IMPLEMENTED | SELF-V-P0-16; spike: docs/protocol/mattermost-spike.md |
| P0-11 | Agent work-item/usage contract and MCP spike | M | P0-03 | IMPLEMENTED | SELF-V-P0-17; spike: docs/protocol/mcp-spike.md |
| P0-12 | permission/risk catalog | S | P0-03 | IMPLEMENTED | SELF-V-P0-18 |
| P0-13 | Telegram API spike | S | — | BLOCKED | needs Telegram bot token + test chats (user) |
| P0-14 | plan operating baseline | S | P0-02 | IMPLEMENTED | SELF-V-P0-20 (attempt 2), docs/plan-baseline.md |

Phase progress (size-weighted, S=1 M=2.5 L=5): 19.5 / 20.5 (P0-13 blocked)

### Latest verification result

r001 aborted (Codex sandbox cannot execute on this host; see verification/phase-0/run-r001/ABORTED.md). VR-P0-002 requested (Codex, revision 2, unsandboxed in an isolated worktree). Known NOT_RUN: V-P0-04 (no container runtime), V-P0-19 (no Telegram bot).

### Open findings / blockers

- B-001 Container runtime (Docker + Compose) absent on the build host; root required. Affects V-P0-04, ClamAV, Compose-based tests.
- B-002 Telegram bot token and two test chats/topics not available. Affects P0-13/V-P0-19 and Phase 2.
- B-003 (root-only, optional) AppArmor blocks unprivileged user namespaces, so Codex's process sandbox cannot run; verification runs unsandboxed in an isolated worktree (ADR-0005 addendum).

### Next step

P0-04/05/07/08/09/10/11/12/14 in parallel (prerequisites P0-01/02/03 are IMPLEMENTED).

## Phase history

| Phase | Result | Report | Tag |
|---|---|---|---|
| 0 | in progress | — | — |
