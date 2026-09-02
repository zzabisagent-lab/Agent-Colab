# Plan operating baseline (P0-14, V-P0-20)

Generated facts come from `python -m tools.plan_baseline_lint` (sizes, Test mapping, prerequisite
DAG, §25A coverage, §25 owners/deadlines) and `docs/traceability.md`. This document records the
confirmations that the development plan §12.1 requires before packages start.

## Sizes confirmed

All 103 packages keep the initial sizes of the plan tables (S=1, M=2.5, L=5; total weight 247).
Per phase: P0 20.5 · P1 33.5 · P2 33 · P3 35 · P4 37 · P5 26.5 · P6 23.5 · P7 20 (weights, see
`docs/traceability.json`). No size was changed in Phase 0.

## L packages broken into sub-items (§12.1)

| Package | Sub-items |
|---|---|
| P1-02 aggregate Event append | (a) per-aggregate append with advisory lock + expected-seq CAS; (b) scoped idempotency with body comparison; (c) causality/workspace checks; (d) canonical hash chain + schema validation at append |
| P1-07 REST/MCP/SSE | (a) common command bus + handler registry; (b) REST routes with Idempotency-Key/If-Match; (c) MCP tool surface on the same handlers; (d) SSE with Last-Event-ID resume + ACL |
| P1-08 Approval Core | (a) grants/ledger schema + explicit states; (b) bounded atomic consume; (c) §7E eligibility/self-approval/quorum; (d) expiry/escalation with Clock |
| P3-09 multi-Agent orchestration | (a) task_edges + cycle trigger; (b) depth/fan-out/concurrency limits; (c) ALL/ANY/QUORUM join evaluation; (d) reassignment history + parent completion gate |
| P3-10 MCP server transport | (a) Streamable HTTP + Bearer/mTLS auth; (b) work_poll/ack/result; (c) inbox resource + subscribe; (d) redelivery on reconnect |
| P4-03 Setup Wizard | (a) transport/bind/token guard; (b) DB→key→Owner/TOTP order + preflights; (c) sealed state/reconciliation; (d) lock + reconfiguration session; (e) wizard UI |
| P4-11 hard delete workflow | (a) dual approval + waiting period; (b) DEK destruction + immutable hash; (c) tombstone ledger; (d) restore reconciliation |
| P4-12 Secret sidecar | (a) package + auth; (b) socket/env/fd injection; (c) revoke push/poll ≤ 5 s; (d) host-bound handles + OCI image |
| P5-01 Schedule schema/API | (a) migration + enums + immutable version FK; (b) CRUD/preview/lifecycle API; (c) Run cancel/history; (d) Run ArtifactLink handler + Approval subject activation |
| P5-03 durable Run/lease | (a) occurrence materialization; (b) claim with SKIP LOCKED + leases; (c) attempts; (d) restart recovery |
| P6-02 Brainstorm turn engine | (a) start command + card; (b) round-robin turn work items; (c) limits + PAUSED/resume; (d) Human free-text IDEA ingestion |
| P7-03 backup/restore | (a) consistent full-scope backup; (b) restore + tombstone reconciliation; (c) retention with virtual clock; (d) RPO/RTO rehearsal |

## Prerequisite DAG

Acyclic (lint). Every prerequisite is in the same or an earlier phase. Phase 0 order used:
P0-01, P0-02, P0-13 → P0-03, P0-04, P0-06, P0-14 → P0-05, P0-07, P0-08, P0-10, P0-11, P0-12 → P0-09.

## Risk → package mapping (§25A) and dependencies (§25)

All 19 spec §18 risks are mapped to ≥ 1 package and ≥ 1 Test; all 22 §25 rows have an owner and
a deadline. The implementer's decision per §25 row is in ADR-0003, including the two blockers
(container runtime, Telegram bot) raised to the user.

## Package ↔ Test mapping

Every package has ≥ 1 Test ID and every Test ID maps back to ≥ 1 package and ≥ 1 REQ
(`docs/traceability.md`).
