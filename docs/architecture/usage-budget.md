# Usage metering and budget (P1-14, development plan §7C)

## Pricing versions

`policy/pricing.yaml` is activated into `pricing_versions` (`server/usage/versions.py`). A
version id is immutable: re-activating identical content is a no-op, different content is
`PRICING_VERSION_IMMUTABLE`; edits create a new version. `current_pricing()` returns the latest
activation and every usage record stores the `pricing_version` it was computed with.

## Usage records

`record_usage()` (`server/usage/records.py`) validates the §7C report against
`schemas/adapters/usage.v1.schema.json`, computes `cost_units` with
`server/usage/pricing.compute_cost_units` (integer ceil per component), and appends one row to
the append-only `usage_records` table with `source`:

| source | when |
|---|---|
| `reported` | the Adapter supplied `cost_units` |
| `computed` | model known in the pricing table |
| `estimated` | model unknown → default rate |
| `unavailable` | `usage_unavailable.reason` given → cost 0 |

Neither usage nor reason → `USAGE_REQUIRED`, nothing is written. The bus command `ReportUsage`
(`server/application/usage.py`) lets an Agent report its own usage; reporting for another
agent needs `work.poll`.

Scopes for aggregation (`usage_for`): `agent_daily` (agent id), `agent_task`
(`<agent>|<task>`), `channel_daily` (channel uuid via `tasks_projection`), `schedule_run`
(run id), `schedule_daily` (run ids supplied by the Schedule service, Phase 5). Daily scopes use
UTC days from the injected `Clock`. `estimate_for()` returns the recent average for the same
Agent and work-item kind, else the caller's default (`DEFAULT_ESTIMATES` in `budget.py`).

## Reservation and settlement

`try_reserve()` takes a per-scope advisory lock and refuses when
`used_today + reserved + estimate > limit` (V-P5-28: 99 and 100 fit a limit of 100, 101 does
not). A refusal appends `BUDGET_EXCEEDED` (payload: scope, limit, requested, used, reserved,
work item) exactly once per rejection (idempotent on work item + estimate) and returns an
outcome; `reserve()` raises `BudgetExceededError` (`CommandError` 409) — the caller must still
commit so the Event persists and put the Task into `WAITING`. Acceptance inserts a
`budget_reservations` row and appends `BUDGET_RESERVED`.

`settle(reservation, actual, limit)` records the actual cost; if `actual > limit - used_others -
reserved_others` the reservation becomes `exceeded`, and `assert_not_overrun(scope)` raises
`BUDGET_EXCEEDED` for the rest of the day so the next side effect is blocked. `release()` frees
an unused reservation. All values are integer `cost_units` (1 credit = 1,000,000).

## Tests

`tests/unit/test_usage_budget.py` (fixtures `tests/fixtures/usage/budget-cases.yaml`) and
`tests/integration/test_usage_db.py` (activation, records through the real store, aggregation,
bus command, allow/allow/reject with Events, settlement overrun, 10-way concurrent reservations).
