# Schedule contract v1 (P0-08)

Authority: spec §5.6, §8.6, §9.1 (Schedule/ScheduleVersion/ScheduleRun), §15 items 13–15;
development plan §6.6, §10A, §21.1. Machine-readable form: `schemas/api/schedule/*.schema.json`;
code: `server/schedules/{cron,occurrence,contract,validate}.py`; fixtures:
`tests/fixtures/schedule/`.

## cron grammar (normative, spec §8.6)

- Exactly five whitespace-separated numeric fields: `minute hour day-of-month month day-of-week`.
- Ranges: minute 0-59, hour 0-23, day-of-month 1-31, month 1-12, day-of-week 0-6 (Sunday = 0).
- Per field: `*`, comma lists, hyphen ranges `a-b` (a ≤ b), steps `*/n` or `a-b/n` (n ≥ 1).
- Rejected: names (`MON`, `JAN`), a seconds field (6 fields), `? L W #`, `@aliases`,
  day-of-week 7, out-of-range values, empty items, single-value steps (`5/15`), reversed ranges.
- Day matching: when both day-of-month and day-of-week are restricted, Vixie **OR** applies; when
  only one is restricted, only that field applies; when neither is, every day matches.
- Minimum interval: the minimum wall-clock gap between consecutive occurrences (computed exactly
  over a 28-year window) must be ≥ `min_interval_minutes` (default 5; operational floor 1).
  Expressions that never match a date (e.g. `0 0 30 2 *`) are `CRON_UNREACHABLE`.

| Code | Trigger |
|---|---|
| `CRON_FIELD_COUNT` | not 5 fields (and not 6) |
| `CRON_SECONDS_REJECTED` | 6 fields |
| `CRON_ALIAS_REJECTED` | expression starts with `@` |
| `CRON_NAME_REJECTED` | any letter in a field |
| `CRON_EXTENDED_TOKEN_REJECTED` | `?`, `L`, `W`, `#` |
| `CRON_DOW7_REJECTED` | value 7 in day-of-week |
| `CRON_RANGE_INVALID` | value outside the field range or reversed range |
| `CRON_STEP_INVALID` | step not a positive integer, or step on a single value |
| `CRON_TOKEN_INVALID` | empty item, dangling `-`, non-numeric token |
| `CRON_INTERVAL_TOO_SHORT` | minimum gap below the configured minimum interval |
| `CRON_INTERVAL_FLOOR` | configured minimum interval below 1 minute |
| `CRON_UNREACHABLE` | never matches a calendar date |
| `TIMEZONE_INVALID` | not an IANA identifier resolvable by `zoneinfo` |
| `TIMESTAMP_NAIVE` | preview asked with a naive datetime |

## Time, timezone, DST (spec §8.6, development plan §10A.3)

- Storage and comparison in UTC; the schedule's IANA timezone name is preserved. Computation
  never depends on the server's local timezone (tested with `TZ=Pacific/Kiritimati`).
- Occurrences are enumerated on the local wall clock. A local minute that does not exist
  (spring-forward gap) is skipped and reported in preview/history with `reason: DST_GAP` and no
  UTC instant. A duplicated local minute (fall-back fold) runs **once** at its first (earlier) UTC
  instant and is reported with `reason: DST_FOLD`.
- `occurrence_key = SHA-256("<schedule_id>|<timezone>|<YYYY-MM-DDTHH:mm>")` (hex). The fold
  offset is not part of the key, so both UTC instants share the key; `scheduled_for` keeps the
  chosen UTC instant and `local_scheduled_for` the wall-clock minute.
- Preview returns the next 10 occurrences by default (`next_occurrences`), strictly after the
  reference instant, within a 5-year horizon.
- Reference cross-check: `croniter` agrees on every non-DST case; it differs on DST by shifting a
  gap time to the next valid local time and by firing a fold time twice. The v8 rule (skip / once)
  is normative; the fixtures document both.

## Runs, kinds, idempotency (development plan §6.6)

- `run_kind`: `SCHEDULED` requires `occurrence_key` and no `retry_of_run_id`; `MANUAL` and
  `RETRY` require `occurrence_key = NULL`; only `RETRY` carries `retry_of_run_id`
  (`RUN_KIND_INVARIANT`).
- Idempotency keys: `SCHEDULED:<schedule_id>:<occurrence_key>`,
  `MANUAL:<schedule_id>:<requester_account_id>:<client_key>`, `RETRY:<original_run_id>:<n>`.
  `(schedule_id, occurrence_key)` and `(schedule_id, idempotency_key)` are unique.
- A Run pins `schedule_version_id` at creation; the action/budget/documentation snapshot is
  read from that version and never overwritten by later Schedule edits.

## Status tables (spec §8.6)

Schedule: `DRAFT→ENABLED`, `DRAFT→DISABLED`, `ENABLED→PAUSED`, `ENABLED→DISABLED`,
`PAUSED→ENABLED`, `PAUSED→DISABLED`; anything else `SCHEDULE_TRANSITION_INVALID`; `DISABLED` is
terminal.

ScheduleRun: `PENDING→DUE→CLAIMED→TASK_CREATED→RUNNING→VERIFYING→SUCCEEDED`; failures
`FAILED`/`TIMED_OUT` from `TASK_CREATED|RUNNING|VERIFYING` (`FAILED` also from `CLAIMED`);
`SKIPPED` from `PENDING|DUE|CLAIMED`; `CLAIMED→DUE` on lease-expiry recovery; cancel from a
pending state (`PENDING|DUE`) → `CANCELLED` immediately; cancel from a running state
(`CLAIMED|TASK_CREATED|RUNNING|VERIFYING`) → `CANCEL_REQUESTED` → `CANCELLED` (Adapter ack ≤ 10 s,
cleanup ≤ 60 s, else `TIMED_OUT`). Terminal: `SUCCEEDED, FAILED, SKIPPED, TIMED_OUT, CANCELLED`;
any write to a terminal Run is `RUN_TERMINAL_CONFLICT`; a second cancel request is
`RUN_CANCEL_ALREADY_REQUESTED`.

## Policies (spec §8.6, development plan §10A.2, §21.1)

| Policy | Values | Default | Rule |
|---|---|---|---|
| concurrency | `FORBID`, `ALLOW`, `REPLACE` | `FORBID` | FORBID → new Run `SKIPPED/SKIPPED_CONCURRENCY`; ALLOW → independent; REPLACE → cancel existing, start only after cleanup confirmed within 60 s, else `SKIPPED/SKIPPED_REPLACE_CANCEL_TIMEOUT` |
| missed run | `SKIP`, `RUN_ONCE`, `BACKFILL_LIMITED` | `RUN_ONCE` | RUN_ONCE creates only the most recent missed occurrence with its original `scheduled_for`; BACKFILL_LIMITED creates occurrences within `backfill_window_seconds`, oldest first, up to `backfill_limit`, with a `BACKFILL_TRUNCATED` warning when anything was dropped |
| retry | max 3 attempts, backoff 1/5/25 s, jitter 0–20 % | as listed | transient errors only; permanent errors → `FAILED` after 1 attempt; attempts recorded in `schedule_run_attempts` |
| skip codes | `SKIPPED_CONCURRENCY`, `SKIPPED_REPLACE_CANCEL_TIMEOUT`, `SKIPPED_POLICY`, `SKIPPED_AGENT_UNAVAILABLE`, `BUDGET_EXCEEDED` | — | stored in `error_code` of a `SKIPPED` Run |

## Action template and agent selection (spec §5.6, §15, development plan §10A.1, §10A.4)

- `action-template.v1`: `action` ∈ the §7.4 core tool names (default `task_create`), `input`
  object, optional `secret_refs` (`secret://...` references only). Keys `shell|command|script|
  exec|args|cmd|argv|bash|sh` anywhere, strings containing shell metacharacters (`; | & > < $( \``)
  or interpreter prefixes, and any secret-valued key (`*secret`, `*token`, `*password`, `api_key`,
  `private_key`) are rejected: `ACTION_TEMPLATE_FORBIDDEN`, `ACTION_TEMPLATE_SECRET_VALUE`;
  other schema violations `ACTION_TEMPLATE_INVALID`.
- `agent-selection.v1`: `{mode: fixed, agent_id}` or `{mode: capability, required_capabilities,
  domain?, exclude_agent_ids?}`; `product`/`vendor` keys → `AGENT_SELECTION_PRODUCT_FORBIDDEN`.
- `schedule-version.v1` / `schedule-run.v1`: full field sets with enums and CHECK-equivalent
  constraints (`backfill_limit`, `backfill_window_seconds`, `max_duration_seconds` ≥ 0;
  `attempt_count` ≤ 3; `SKIPPED` requires a skip code; `CANCELLED` requires `cancelled_at`).
  Codes: `SCHEDULE_VERSION_INVALID`, `RUN_INVALID`, plus the cron/timezone codes above.
