# Observability (P7-02)

Three surfaces describe a running instance, and they are three renderings of the same numbers:
the ops dashboard (`GET /api/v1/ops/overview`), the metrics exposition
(`GET /api/v1/ops/metrics`) and the alert stream in the ops channel. A number that appears in two
of them cannot disagree, because both read `server/ops/dashboard.py` and
`server/schedules/metrics.py` rather than counting anything themselves.

## Structured logs

`server/observability/logs.py` writes one JSON object per line on two loggers:

| Logger | One line per | Fields |
|---|---|---|
| `agent_colab.access` | HTTP request | `correlation_id`, `method`, `path`, `status`, `duration_ms`, `outcome` |
| `agent_colab.command` | command execution | `correlation_id`, `command`, `outcome`, `duration_ms`, `principal_kind`, `principal`, `workspace`, `resource_id`, `event_id`, `error_code` |

`RequestLogMiddleware` is the outermost middleware. It accepts an inbound `X-Correlation-ID`, or
mints one, puts it in a `ContextVar`, and echoes it on the response — so a command log written
deep inside a handler carries the same id as the access log line and as any alert raised during
that evaluation. Health and metrics reads log at `DEBUG` so a scrape does not drown the log.

Values are never logged: records carry ids, outcomes and durations only.

## Metrics

`GET /api/v1/ops/metrics` (permission `admin.settings`) returns a Prometheus text exposition.
No client library is used; the format is three lines per family. Families are prefixed
`agent_colab_`:

| Metric | Labels | Meaning |
|---|---|---|
| `dependency_up` | `dependency` | 1 when the probe passed, 0 when it failed |
| `dependency_latency_ms` | `dependency` | latency of the last probe |
| `alerts_active` | `severity` | dependency alerts currently raised |
| `tasks` | `status` | Tasks in the projection |
| `agents` | `status` | registered Agents |
| `agents_online` | — | Agents currently online |
| `outbox_rows` | `kind`, `status` | unsent delivery outbox rows |
| `hard_delete_requests_pending` | — | hard deletes awaiting approval or their waiting period |
| `maintenance_mode` | — | 1 while non-administrator writes are refused |
| `schedule_runs_due` / `schedule_runs_running` | — | scheduler queue depth |
| `schedule_run_failures` / `schedule_stuck_leases` | — | scheduler health |
| `schedule_start_delay_seconds` / `schedule_lag_seconds` | `quantile` | p50, p95 and max |

## Alerts and their runbooks

Rules live in `policy/alert-rules.yaml`; `server/ops/alerts.py` evaluates them against signals
read from the same tables the dashboard reads. Every **critical** rule names a runbook, and the
loader refuses a rule set where a critical rule does not, or where a rule points at a runbook the
file does not declare.

| Alert | Severity | Runbook |
|---|---|---|
| `DEPENDENCY_DOWN_POSTGRES` | critical | `db-restore` |
| `DEPENDENCY_DOWN_STORAGE` | critical | `nas-full` |
| `DEPENDENCY_DOWN_SECRET_PROVIDER` | critical | `credential-rotation` |
| `DEPENDENCY_DOWN_MATTERMOST` | warning | `bridge-loop` |
| `OUTBOX_BACKLOG` | warning | `bridge-loop` |
| `OUTBOX_DEAD_LETTERS` | critical | `bridge-loop` |
| `BRIDGE_DEAD_LETTERS` | critical | `bridge-loop` |
| `SCHEDULER_LAG` | warning | `scheduler-storm` |
| `SCHEDULER_STUCK_LEASES` | critical | `scheduler-storm` |
| `SCHEDULE_BUDGET_EXCEEDED` | warning | `scheduler-storm` |
| `SECRET_CANARY_DETECTED` | critical | `secret-leak` |
| `AUTH_RATE_LIMITED` | warning | `credential-rotation` |
| `HARD_DELETE_BACKLOG` | warning | `hard-delete-restore` |

Signals a deployment cannot compute (an absent table, for instance) are skipped rather than
guessed at, so a partially deployed instance does not alert on nothing.

Emission goes through the notification outbox to the ops channel, deduplicated per Workspace, key
and hour by the `schedule_alert_emissions` ledger — its columns are alert-generic, so ops alerts
and scheduler alerts share one ledger and one hourly window.

### Timing

A failing dependency is visible on the next evaluation with `refresh=1`, and within
`probes.STALE_S` (60 s) without one, which is the §21.1 "status and alert consistent within 60 s
of probe failure" budget. `tests/integration/test_observability_alerts_db.py` measures that
window and checks that the log, the alert and the dashboard row carry one correlation id.
