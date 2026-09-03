# Phase 5 — Scheduled Work: module ownership

Foundation: the Phase 0 schedule library (`server/schedules/{cron,contract,occurrence,validate}.py`,
ADR-0006) is the normative core (grammar, transitions, occurrence keys, concurrency/missed-run/retry
decisions). Phase 5 adds persistence, planners/runners, policy re-checks, notifications, UI and
metrics on top of it. Placeholder migrations `0016`–`0018` are owned by the packages below.

| Package(s) | Modules | Migration | Tests |
|---|---|---|---|
| P5-01/02/03 schedule core: schema/API, planner, durable Runs/leases | `server/schedules/{store,planner,runs,leases,api_models}.py`, `server/application/schedules.py`, `server/api/v1/schedules.py`, `server/schedules/runner.py` | `0016` | V-P5-01..08, V-P5-22, V-P5-24, V-P5-26, V-P5-29, V-P5-31..36 |
| P5-04/05/06/07/10 execution: policy re-check, concurrency/missed runs, retry/timeout/cancel, channel notices, budget/latency | `server/schedules/{execution,policy_check,recovery,notify,budget}.py`, `server/application/schedule_runs.py` | `0017` | V-P5-09..21, V-P5-23, V-P5-27, V-P5-28, V-P5-30, V-P5-37 |
| P5-09 metrics/alerts | `server/schedules/metrics.py`, `server/api/v1/schedule_metrics.py` | `0018` | V-P5-25, V-P5-27 |
| P5-08 Schedule Admin UI (parent) | `web-admin/src/features/schedules/*`, `tests/e2e/test_admin_schedules_ui.py` | — | V-P5-21, V-P5-22 (UI half) |

Rules: the command bus stays the only write path; every Run pins its ScheduleVersion; occurrence
keys are unique per `(schedule_id, occurrence_key)`; policy, principal, Channel, Approval and
Secret references are re-checked on every Run; secrets in templates are rejected by the Phase 0
validator; runner claims use DB leases (poll 15 s, claim lease 60 s, heartbeat 15 s by default).
