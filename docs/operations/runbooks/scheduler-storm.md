# RB-SCHEDULER-STORM — due Runs pile up faster than the runners drain them

- **Id:** `RB-SCHEDULER-STORM`
- **Trigger:** alert `START_DELAY_P95_ABOVE_60S` or `STUCK_LEASES`, or a visible backlog in
  `GET /api/v1/schedules/metrics`.
- **Severity:** critical. Start delay breaches the 60-second target and Tasks arrive late.

## Detection

1. `GET /api/v1/schedules/metrics` — read `due`, `running`, `lag_s` and `start_delay_s`
   percentiles, `stuck_leases` and `failure_rate`. The same snapshot feeds
   `GET /api/v1/ops/overview`, so the dashboard and history must agree.
2. Identify the offenders: `GET /api/v1/schedules/{schedule_id}/metrics` per schedule, or
   `SELECT schedule_id, count(*) FROM schedule_runs WHERE status IN ('PENDING','DUE') GROUP BY 1 ORDER BY 2 DESC;`
3. Check whether the runners are alive: each worker logs one JSON line per tick
   (`python -m server.schedules.worker --workspace <ws> --runner-id <id>`).

## Isolation

1. Pause the storming schedules first, newest offender first:
   `/colab schedule pause sch-…` or `POST /api/v1/schedules/{id}/pause`. Pending Runs are
   cancelled; running Runs finish.
2. If the whole instance is saturated, enter maintenance mode
   (`POST /api/v1/maintenance/enter`): claiming stops while the outbox drain continues.

## Recovery

1. Release stuck claims so another runner can take them:
   `server.schedules.runner.expire_leases(session, now, workspace_id=…)` — a claim whose lease
   expired returns to `DUE` and exactly one runner recovers it.
2. Start an additional worker process if the backlog is genuine load, or lengthen the cron
   interval if a schedule is simply too frequent (the minimum interval is enforced per version).
3. Resume: `/colab schedule resume sch-…`, then exit maintenance mode if it was entered.

## Post-verification

1. `GET /api/v1/schedules/metrics` shows `start_delay_s.p95` at or below 60 seconds,
   `stuck_leases` zero, and the backlog falling.
2. `duplicates_prevented` confirms no occurrence produced two Runs during the incident.
3. The pause and resume actions are audited, and the alert clears.

## Evidence to capture

The metrics snapshot before and after, the per-schedule backlog counts, the lease-expiry result,
and the audit rows for pause, resume and any maintenance transition.
