# Scheduler worker process (P5-03)

`server/schedules/worker.py` runs the scheduler duty of one Workspace as a standalone OS process:

```
uv run python -m server.schedules.worker \
    --workspace <workspace uuid> --runner-id scheduler-1 \
    [--database-url postgresql://…] [--once | --max-ticks N] [--start-delay-s 0]
```

The database URL falls back to `AGENT_COLAB_DATABASE_URL`. Several workers may run against one
database; the claim lease decides who owns a Run, so no other coordination is needed.

## Why the worker is phased

The in-process maintenance tick (`server/agents/maintenance.py`) claims and executes inside one
transaction. The worker instead commits **three phases**:

| Phase | Transaction | Effect |
|---|---|---|
| claim | own | expire stale leases, materialize due occurrences, claim Runs with a DB lease |
| execute | one per Run | policy re-check, budget, Task creation and the Run's status change |
| recover | own | timeouts, cancel windows, due retries |

Committing the claim separately makes the lease visible to peer workers immediately, and a worker
that dies mid-execution leaves a claimed Run whose lease expires, so exactly one peer recovers it
(`CLAIMED → DUE` is the only backward transition). The Task and the Run's `TASK_CREATED` status
still commit together, so a crash can never leave a Task without its Run pointer.

## Settings

| Variable | Meaning | Default |
|---|---|---|
| `AGENT_COLAB_SCHEDULER_POLL_S` | poll interval, 5–60 s (§10A.1) | 15 |
| `AGENT_COLAB_SCHEDULER_LEASE_S` | claim lease, at least 3× the poll interval | 60 |
| `AGENT_COLAB_SCHEDULER_LIMIT` | Runs claimed per tick | 10 |

`server.schedules.runner.validate_scheduler_settings` enforces the bounds at start-up, so an
out-of-range pair fails fast instead of degrading recovery.

Each tick prints one JSON line, for example:

```json
{"claimed": ["run-9c8abfaa66fc4bea"], "executed": [{"run_id": "run-9c8ab…", "status": "TASK_CREATED", "task_id": "task-…", "error_code": null}], "paused": false, "tick": 1}
```

`paused` is true while maintenance mode blocks claiming (V-P4-32); recovery still runs.
`SIGTERM`/`SIGINT` stop the loop after the current tick.

## Test seam: `AGENT_COLAB_SCHEDULE_KILL_AFTER`

Crash recovery cannot be proven with a virtual clock, so the worker carries one failpoint, read in
exactly one place (`_maybe_kill`) and inert unless the variable is set:

| Value | Boundary |
|---|---|
| `claimed` | immediately after the claim transaction commits (lease held, no Task yet) |
| `task_created` | immediately after the transaction that created the Task commits |

At the named boundary the worker calls `os._exit(137)`: no cleanup hooks, no rollback of committed
state, indistinguishable from `SIGKILL` as far as the database is concerned.

`tests/e2e/test_schedule_worker_processes.py` uses it for the two criteria that require real
processes:

* **V-P5-08** kills at `task_created`, then runs a second worker: exactly one Task, one
  `TASK_CREATED` Event, one `RUN_STARTED` Event, one attempt row and one start notice remain.
* **V-P5-24** runs two workers, kills the claimant at `claimed` and measures the wall-clock time
  until the peer finishes the Run — 18.6 s against the criterion's budget of lease + 2 × poll
  (25 s at the §10A.1 minimum settings of 5 s poll and 15 s lease).
