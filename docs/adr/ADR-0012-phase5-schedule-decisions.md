# ADR-0012: Phase 5 scheduled-work decisions

- Status: Accepted (Phase 5)
- Date: 2026-09-03

## Decisions

1. **The Phase 0 schedule contract is the single source of truth.** Cron grammar, DST handling,
   occurrence keys, status transitions, concurrency/missed-run/retry decisions live in
   `server/schedules/{cron,contract,occurrence,validate}.py` (ADR-0006); Phase 5 persistence and
   runners call those pure functions and never re-implement them.
2. **Exactly-once materialization by unique occurrence key.** `schedule_runs` is unique on
   `(schedule_id, occurrence_key)`; planners insert with `ON CONFLICT DO NOTHING`, so dual
   planners cannot create two Runs (V-P5-06). Manual and retry Runs carry deterministic
   `MANUAL:`/`RETRY:` idempotency keys linked to their originals.
3. **Runners claim with DB leases.** `FOR UPDATE SKIP LOCKED` claim + lease expiry
   (default 60 s, heartbeat 15 s, poll 15 s); a lease that expires returns the Run to `DUE`
   (`CLAIMED→DUE` is the only backward transition) so exactly one runner recovers (V-P5-07/24).
4. **Every Run pins its ScheduleVersion and re-checks authority.** The action template comes from
   the pinned version; principal status, Roles/Capabilities, Channel membership, Approval
   consumption and Secret references are re-evaluated at claim time (`SKIPPED_POLICY`,
   `SKIPPED_AGENT_UNAVAILABLE`, `BUDGET_EXCEEDED`).
5. **Task creation and Approval consumption share one transaction** with a deterministic
   idempotency key, so a crash after Task creation never duplicates side effects (V-P5-08/30).
6. **Budgets are enforced through the §7C reservation path**: per-Run and daily cost_units are
   reserved before delivery and settled from `usage_records`; overruns skip the next Run with an
   alert (V-P5-28/37).
7. **Package-owned migration slots 0016–0018** so parallel packages never conflict on revision ids.
8. **Metrics are computed from Run history, not counters kept elsewhere**: due/run/lag/error and
   stuck-lease alerts derive from `schedule_runs`/`schedule_run_attempts`, so dashboard and
   history cannot disagree (V-P5-25).

## Consequences

- A second scheduler process needs no coordination beyond the database.
- Changing scheduler defaults (poll/lease/heartbeat/retry) is a settings change validated within the
  §10A.1 bounds (poll 5–60 s, lease ≥ 3× poll), audited like every other setting.
