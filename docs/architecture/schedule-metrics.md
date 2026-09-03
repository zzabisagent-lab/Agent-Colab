# Scheduler metrics and alerts (P5-09)

Everything the dashboard shows about scheduled work is computed from Run history at read time
(`schedule_runs`, `schedule_run_attempts`, `schedule_planner_notes`, `budget_alerts`), so the
dashboard and the history cannot disagree (validation plan V-P5-25; ADR-0012 decision 8). Two
things history cannot express are stored by this package (migration 0018):

| Table | Purpose |
|---|---|
| `schedule_metrics_counters` | planner occurrence-key conflicts that never became rows (`duplicates_prevented`), per Schedule |
| `schedule_alert_emissions` | one row per (workspace, alert key, hour) — the deduplication ledger for alert notifications |

## Snapshot (`colab.schedule-metrics.v1`)

`server/schedules/metrics.snapshot(session, workspace_id, now, window_s=86400, poll_s=15)`
returns a document validated by `schemas/documents/schedule-metrics.v1.schema.json`:

- `due`, `running` (CLAIMED/TASK_CREATED/RUNNING/VERIFYING/CANCEL_REQUESTED), `runs_in_window`,
  `by_status`
- `lag_s` — seconds DUE/CLAIMED Runs have waited past `scheduled_for` (count/p50/p95/max,
  nearest-rank percentiles)
- `start_delay_s` — `started_at − scheduled_for` of SCHEDULED Runs (the §21.1 p95 ≤ 60 s target)
- `failures`, `timed_out`, `succeeded`, `failure_rate`
- `skips_by_code`, `policy_denials` (`SKIPPED_POLICY`, `SKIPPED_AGENT_UNAVAILABLE`,
  `BUDGET_EXCEEDED`), `backfill_warnings` (Run planner notes and planner-note rows starting with
  `BACKFILL`), `duplicates_prevented`, `stuck_leases` (CLAIMED Runs whose lease expired more than
  one poll interval ago), `budget_alerts` (`budget_alerts.kind = 'budget_exceeded'` in the window)
- `schedules[]` — per Schedule: `next_run_at`, `runs_in_window`, `failures`, `last_status`,
  `last_error_code`, `duplicates_prevented`

The window covers Runs scheduled within `window_s` plus every non-terminal Run regardless of age.

## Alerts

`alerts(snapshot, thresholds)` (defaults in `Thresholds`):

| Key | Fires when | Severity |
|---|---|---|
| `START_DELAY_P95_ABOVE_60S` | `start_delay_s.p95 > 60` (exactly 60 s does not alert) | warning |
| `STUCK_LEASES` | `stuck_leases ≥ 1` | critical |
| `FAILURE_RATE` | `failure_rate ≥ 0.5` with at least 5 finished Runs | warning |
| `BUDGET_EXCEEDED` | any budget overrun alert in the window | warning |

`emit_alerts` posts each alert to the ops channel through the notification outbox
(`mattermost:<ops.channel_id>`, payload `event_type: SCHEDULE_ALERT`) at most once per hour per
key; `evaluate_and_emit` chains snapshot → alerts → emission for a maintenance tick.

## API and dashboard

- `GET /api/v1/schedules/metrics[?window_s=]` and `GET /api/v1/schedules/{id}/metrics`
  (`schedule.manage`, or `admin.settings` through `api:ops_read`); the metrics router is mounted
  before the Schedule CRUD router so `/metrics` is never captured by `/{schedule_id}`.
- `GET /api/v1/ops/overview` carries a `schedules` block (due, running, lag/start-delay p95,
  failures, policy denials, stuck leases, duplicates prevented, active alert keys).

## Hook for the planner

`record_duplicate_prevented(session, workspace_id, schedule_id, now)` — call it whenever an
`ON CONFLICT (schedule_id, occurrence_key)` insert prevented a second Run (V-P5-06).
