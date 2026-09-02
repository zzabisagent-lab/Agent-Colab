# ADR-0005: Repository conventions, evidence layout, and implementer/verifier identities

- Status: Accepted (Phase 0)
- Date: 2026-09-02

## Layout

- `tools/` — repository-level linters and helpers (traceability, criteria, phase DAG, plan
  baseline, policy lint, evidence recorder). Not shipped in images.
- `evidence/phase-<n>/SELF-<Test ID>/attempt-NN/` — implementer self-test evidence written by
  `python -m tools.evidence run <Test ID> -- <command>`; immutable, new attempts are appended.
- `evidence/phase-<n>/manifest.yaml` — Evidence Manifest (validation plan §6.1).
- `verification/phase-<n>/` — Verifier Reports written only by the Verifier process, stored with
  `<report>.sha256`; never edited by the implementer.
- `docs/adr/` — decisions; `docs/protocol/` — spike results and interface notes.

## Identities

| Role | agent_id | account_id | credential fingerprint | Notes |
|---|---|---|---|---|
| Implementer | `agent-claude-code` | `account-implementer-claude` | `sha256(claude-code:zzabisagent-lab)` | Anthropic Claude Code session, GitHub author `zzabisagent-lab` |
| Verifier | `agent-codex` | `account-verifier-codex` | `sha256(codex:chatgpt-login)` | OpenAI Codex CLI 0.152, separate process, fresh context per run |

`identity_graph_version = identity-v8-001`. The two identities share no account, credential,
alias, or session. The fingerprints are computed from identity labels, never from secrets, and
are recorded in every Evidence Manifest and Verifier Report.

## Branching

`phase-<n>` branches; merge to `main` and tag `phase-<n>-passed` only after a `PASSED` report.
Commits reference package IDs (`P0-03: ...`).
