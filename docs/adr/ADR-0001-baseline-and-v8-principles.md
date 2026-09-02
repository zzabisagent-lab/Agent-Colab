# ADR-0001: Adopt the v8 documents as the protected baseline

- Status: Accepted (Phase 0)
- Date: 2026-09-02

## Context

The product specification, development plan, and validation plan (v8, English canonical text)
define Agent-Colab. Earlier versions (v1–v7) and Korean editions are superseded.

## Decision

1. `docs/baseline/` holds the three v8 documents byte-for-byte with `SHA256SUMS`; they are never
   edited in this repository. Any change to a baseline document follows spec §21 / validation
   plan §21 and is not made during the autonomous run.
2. The product name is **Agent-Colab** everywhere user-facing (package metadata, UI titles, API
   titles, documents). Code identifiers use `agent_colab` / `agent-colab`.
3. No specific Agent product or machine is a core role. Agents exist only through the Agent
   Registry + Adapter + Role + Capability model (spec §4.2); adapter types are `mcp`, `webhook`,
   and `mattermost_bot`. Fixed Coordinator/Worker/development-server roles are absent.
4. Schedules are durable Runs with a normative cron grammar (spec §8.6); no OS cron and no shell
   command templates.
5. Every state change is an append-only aggregate Event (spec §9.3); projections are never the
   authority for permissions, approval consumption, or duplicate prevention.
6. Implementation and verification are performed by different identities (ADR-0005); a phase is
   complete only on a Verifier `PASSED`. The pipeline runs Phase 0 → 7 without human gates; the
   only human decision is deployment approval after the final report.
7. Traceability is generated, not hand-written: `python -m tools.trace_matrix --write` produces
   `docs/traceability.md`/`.json` from spec Appendix A and the two plans; CI runs `--check`.

## Consequences

- Requirement IDs (`REQ-*`), package IDs (`P<n>-<nn>`) and Test IDs (`V-P<n>-<nn>`) are the only
  cross-reference vocabulary; new work must be mapped to them.
- Open items that the baseline assigns to human owners (development plan §25) are recorded in
  ADR-0003 with the decision taken or the question raised.
