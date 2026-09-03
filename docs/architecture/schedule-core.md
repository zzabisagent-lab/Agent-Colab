# Schedule core: schema, planner, runner (P5-01/P5-02/P5-03)

The Phase 0 library is normative and is never re-implemented: `server/schedules/cron.py`
(grammar, timezones, DST), `contract.py` (status tables, concurrency, missed runs, retry),
`occurrence.py` (occurrence and idempotency keys), `validate.py` (action template, agent
selection, version and run schemas). ADR-0006 fixes those decisions; this package adds
persistence, the planner and the runner on top of them.

## Tables (migration 0016)

| Table | Purpose |
|---|---|
| `schedules` | one row per Schedule: status, `current_version_id` (deferred FK, same-schedule trigger), `next_run_at`, `last_planned_until` |
| `schedule_versions` | immutable snapshots; an UPDATE/DELETE trigger rejects any change; unique `(schedule_id, version)`; `snapshot_hash` = SHA-256 over the RFC 8785 canonical content |
| `schedule_runs` | unique `(schedule_id, occurrence_key)` and `(schedule_id, idempotency_key)`; CHECKs enforce the run-kind invariants; a trigger freezes `schedule_version_id`, `version_hash`, `run_kind`, `occurrence_key`, `idempotency_key` after creation |
| `schedule_run_attempts` | unique `(run_id, attempt_no)`; `schedule_runs.attempt_count` is kept equal to the row count |
| `schedule_planner_notes` | occurrences that never become Runs: `DST_GAP`, `MISSED_SKIPPED_*`, `BACKFILL_TRUNCATED` |

## Commands, reads and endpoints

`server/application/schedules.py` holds the bus commands `CreateSchedule`,
`CommitScheduleVersion` (PATCH = a new immutable version), `EnableSchedule`, `PauseSchedule`,
`ResumeSchedule`, `DisableSchedule`, `RunScheduleNow`, `CancelScheduleRun`, `RetryScheduleRun`
and the reads `get_schedule`, `list_schedules`, `preview`, `run_view`, `list_runs`,
`run_history`. `server/api/v1/schedules.py` exposes them:

```
POST   /api/v1/schedules                 GET  /api/v1/schedules
GET    /api/v1/schedules/{id}            PATCH /api/v1/schedules/{id}
POST   /api/v1/schedules/{id}/enable|pause|resume|disable|run-now
POST   /api/v1/schedules/preview         GET  /api/v1/schedules/{id}/runs|history
GET    /api/v1/schedules/runs/{run_id}   POST /api/v1/schedules/runs/{run_id}/cancel|retry
```

MCP tools (`server/agents/schedule_tools.py`, §7.4) mirror them and are **hidden by default**:
`list_tools` only shows `schedule_create|preview|get|pause|resume|disable|run_now|run_cancel` to a
caller whose Roles carry `schedule.manage` / `schedule.run` / `schedule.read`; calling an
unadvertised tool answers `CAPABILITY_UNSUPPORTED`.

Stable error codes: `SCHEDULE_NOT_FOUND`, `SCHEDULE_ALREADY_EXISTS`, `SCHEDULE_ID_INVALID`,
`SCHEDULE_FIELD_UNKNOWN`, `SCHEDULE_NO_CHANGES`, `SCHEDULE_STATUS_INVALID`,
`SCHEDULE_TRANSITION_INVALID`, `SCHEDULE_VERSION_MISSING`, `SCHEDULE_PREVIEW_INPUT`,
`RUN_NOT_FOUND`, `RUN_DUPLICATE`, `RUN_RETRY_NOT_TERMINAL`, `RUN_TERMINAL_CONFLICT`,
`RUN_CANCEL_ALREADY_REQUESTED`, plus the contract's cron/template codes (`CRON_*`,
`ACTION_TEMPLATE_*`, `TIMEZONE_INVALID`) which are raised before schema validation so the caller
always sees the precise grammar reason.

Events: `SCHEDULE_CREATED|UPDATED|ENABLED|PAUSED|RESUMED|DISABLED` on the `schedule` aggregate and
`RUN_DUE|CLAIMED|CANCEL_REQUESTED|CANCELLED` on `schedule_run` (the execution package appends
`RUN_STARTED|SUCCEEDED|FAILED|SKIPPED|TIMED_OUT`). No new Event types were needed.

## Planner (`server/schedules/planner.py`)

`plan_schedule(session, store, clock, schedule=…, version=…, horizon_s=…)` and
`plan_workspace(...)`, with `materialize(session, store, clock, workspace_id, horizon_s) -> int`
as the `SchedulerPorts` entry point:

1. occurrences of the **current** version between `last_planned_until` (or now) and `now + horizon`;
2. DST gaps are recorded as planner notes and never become Runs; a folded wall-clock minute keeps
   one occurrence key and runs at its first UTC instant;
3. instants already in the past go through `plan_missed_runs` (`SKIP | RUN_ONCE |
   BACKFILL_LIMITED`) and keep their original `scheduled_for`; truncation is a note;
4. every insert is `ON CONFLICT DO NOTHING` on `(schedule_id, occurrence_key)`, so two planners
   racing on the same occurrence still produce exactly one Run;
5. `last_planned_until` and `next_run_at` are advanced.

## Runner (`server/schedules/runner.py`)

* `mark_due(session, workspace_id=…, now=…, store=…, actor_account_id=…)` — `PENDING → DUE` plus one
  `RUN_DUE` Event each.
* `claim_due(session, runner_id, now, workspace_id=…, lease_s=60, limit=1, …)` — `FOR UPDATE SKIP
  LOCKED`, `DUE → CLAIMED`, lease and `RUN_CLAIMED`.
* `heartbeat(session, run_id, runner_id, now, lease_s)` — extends the lease only for the owner.
* `expire_leases(session, now, workspace_id=…, …)` — `CLAIMED → DUE` (the only backward
  transition), so exactly one runner recovers a crashed claim within lease + 2 poll intervals.
* `tick(session, store=…, clock=…, workspace_id=…, runner_id=…, actor_account_id=…)` — one
  scheduler pass: expire, materialize, claim; the caller hands each claimed Run to
  `execute_claimed(session, run, ctx)`, which delegates to `server.schedules.execution`
  (`EXECUTOR_MISSING` when that package is absent).
* `validate_scheduler_settings(poll_s, lease_s)` — §10A.1 bounds (poll 5–60 s, lease ≥ 3× poll).

Defaults come from `server/domain/defaults.py`: poll 15 s, claim lease 60 s, heartbeat 15 s.

## Subject activation (§6.7, §6.8)

Phase 5 activates the reserved subjects: `server/schedules/links.py` implements the
`schedule_run` ArtifactLink handler (existence, Workspace, readers = execution principal and
requester) and `server/approvals/model.py` now validates `schedule` and `run` Approval subjects
against `schedules` / `schedule_runs` in the caller's Workspace.
