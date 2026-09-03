# Schedule Run execution (P5-04, P5-05, P5-06, P5-07, P5-10)

Persistence and claiming belong to the schedule core package (P5-01..P5-03). This package turns a
*claimed* Run into a Task and closes it, applying the §10A.2 execution algorithm steps 4-7 and
10-13. All decisions come from the Phase 0 contract library (`server/schedules/contract.py`,
ADR-0006); nothing here re-implements grammar, transitions or policy arithmetic.

## Modules

| Module | Role |
|---|---|
| `server/schedules/execution.py` | `execute(run, ctx)`, cancel/finish, Task-terminal closing, retries |
| `server/schedules/policy_check.py` | per-Run authority re-check (§10A.4) |
| `server/schedules/budget.py` | per-Run and daily cost_units reservation/settlement + alerts (§7C) |
| `server/schedules/notify.py` | channel notices through the Renderer outbox (§10A.2 step 7) |
| `server/schedules/recovery.py` | max-duration timeouts, cancel windows, due retries |
| `server/schedules/run_access.py` | `RunStore` over the core package's tables |
| `server/application/schedule_runs.py` | the scheduler tick and the Task terminal hook |

`RunStore`/`SchedulerPorts` (protocols in `execution.py`) are the only surface between the two
packages: execution contains no SQL for Runs, and the core owns the schema.

## One execution (steps 4-7)

1. **Authority re-check** against the *live* state, with the action template read from the Run's
   pinned ScheduleVersion: Schedule `ENABLED`, execution principal ACTIVE and still permitted,
   channel membership, Agent selection (fixed Agent active+online, else the capability query via
   `server/agents/routing.py`), Approval requirement, Secret grants for every `secret_refs` entry.
   A failure yields `SKIPPED_POLICY` or `SKIPPED_AGENT_UNAVAILABLE` with zero side effects.
2. **Budget** (§7C): the version's `budget_policy` reserves an estimate against the
   `schedule_run` and `schedule_daily` scopes. An open overrun or an exceeded reservation ends the
   Run as `SKIPPED` / `BUDGET_EXCEEDED` with a `budget_alerts` row and an administrator notice.
3. **Concurrency** (§8.6): `FORBID` skips (`SKIPPED_CONCURRENCY`), `ALLOW` proceeds, `REPLACE`
   cancels the active Run first and waits for its cleanup; unconfirmed after 60 s the new Run ends
   `SKIPPED` / `SKIPPED_REPLACE_CANCEL_TIMEOUT` instead of running concurrently.
4. **Task creation** with the Run's deterministic idempotency key; the Approval consumption, the
   delegation and the short single-use Secret leases share that one transaction. The Run becomes
   `TASK_CREATED` and `RUN_STARTED` is appended; a start later than the §21.1 target posts the late
   notice and raises a `start_delay` alert.

The Task's terminal transition closes the Run through the registered hook: `SUCCEEDED` for
COMPLETED/VERIFIED, `FAILED` otherwise, then leases are revoked, budgets settled from
`usage_records`, retries cleared and the result notice posted.

## Retries, timeouts, cancel

* **Transient** failures (`TRANSIENT`, `TIMEOUT_TRANSIENT`, `PROVIDER_UNAVAILABLE`) schedule the
  next attempt in `schedule_run_retries` at 1/5/25 s plus 0-20 % jitter, at most three attempts;
  every attempt is a `schedule_run_attempts` row. Any other error is terminal `FAILED` at once.
* **Max duration** exceeded → cancel request; the Adapter has 10 s to acknowledge and 60 s to clean
  up. Confirmed cleanup ends the Run `CANCELLED`, otherwise `TIMED_OUT`, both with their Events,
  lease revocation and budget settlement, plus `timeout` / `cancel_timeout` alerts.
* **Cancel** of a pending Run is immediate; of a running Run it goes through `CANCEL_REQUESTED`.

## The tick

`server.application.schedule_runs.tick(runtime, workspace_id=...)` runs from the gateway
maintenance loop: expire leases → materialize (planner) → claim (runner) → execute each Run →
recovery pass. Maintenance mode pauses claiming (V-P4-32) while recovery still closes open cancel
windows. Values from `server/domain/defaults.py`: poll 15 s, claim lease 60 s, heartbeat 15 s.

## Tables (migration 0017)

`schedule_run_retries` (one open retry per Run), `schedule_run_budgets` (reservation per Run and
scope with its settlement), `budget_alerts` (budget/latency/timeout/backfill alerts for the metrics
package and the dashboard), `schedule_notices` (one notice per Run and kind). `channel_posts.role`
gains `notice`.

## Codes

Skips: `SKIPPED_CONCURRENCY`, `SKIPPED_REPLACE_CANCEL_TIMEOUT`, `SKIPPED_POLICY`,
`SKIPPED_AGENT_UNAVAILABLE`, `BUDGET_EXCEEDED`. Failures: the bus error code, `RETRY_EXHAUSTED`,
`TASK_<terminal state>`, `CANCEL_CLEANUP_TIMEOUT`, `MAX_DURATION_EXCEEDED`,
`SCHEDULE_ACTION_UNSUPPORTED`. Secret values never appear in templates, notices, alerts or logs;
only `secret://` references and lease ids do.
