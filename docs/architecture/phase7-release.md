# Phase 7 — Release Hardening: module ownership

No new migrations: Phase 7 hardens, measures and packages what Phases 0–6 built.

| Package(s) | Deliverables | Tests |
|---|---|---|
| P7-01 CI/CD + P7-05 security hardening | `.github/workflows/*` (lint, type, unit, integration, e2e, scans, SBOM, image build/publish with immutable digests), `deploy/production/*` images, SAST/dependency/container/dynamic scan wiring, `docs/security/hardening.md` | V-P7-11, V-P7-15, V-P7-05, V-P7-06, V-P7-12 |
| P7-03 backup/restore + P7-06 upgrade/rollback | full-scope consistent backup with key tombstone reconciliation, retention with an injectable clock, restore rehearsal, projection rebuild parity, upgrade and forward-fix rehearsal | V-P7-07, V-P7-08, V-P7-19, V-P7-20, V-P7-09, V-P7-10 |
| P7-02 observability + P7-04 load/soak | metrics, structured logs, alert rules each linking a runbook, synthetic failure set; 3x normal load for 30 minutes and a bounded soak with zero Event/Run loss | V-P7-14, V-P7-03, V-P7-04 |
| P7-08 runbooks + P7-09 Human-path acceptance | the seven runbooks (secret leak, NAS full, Bridge loop, Scheduler storm, DB restore, credential rotation, hard-delete restore) with every critical alert linking one; Mattermost-only acceptance automation | V-P7-13, V-P7-21, V-P7-02, V-P7-22 |
| P7-07 release package (parent) | immutable digests and an Ed25519-signed manifest, changelog, operations docs, evidence archive, residual-risk register with owner/deadline/acceptor, `REPORT.md` per development plan §27A, and `docs/operations/deployment-decision.md` with its checker `tools/deployment_decision.py` | V-P7-01, V-P7-16, V-P7-17, V-P7-18 |

Rules: the §21.1 profile is the measurement contract (normal load 50 Humans, 20 Agents, 100
Channels, 20 Bridges, 20 API writes/s, 10 messages/s, 100 active Schedules, ≤20 due/min; peak is
3x for 30 minutes; write/read p95 ≤ 500/300 ms; 5xx < 1%). Default RPO 24 h and RTO 4 h. No
runtime dependency may be added. Every scan finding that is not Low needs an owner, a deadline and an acceptor.

The deployment decision is a record, not a message: `docs/operations/deployment-decision.md`
carries the state, and `tools/deployment_decision.py --check` proves the report exists with the
sections §27A requires and that the deployment ledger under `release/deployments/` is empty while
no decision has been recorded. That empty ledger is the evidence that no deployment preceded the
user's approval (V-P7-18). Only a human decision changes the state to `APPROVED`.
