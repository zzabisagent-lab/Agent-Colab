# Load and soak testing (P7-04)

`tests/load/` drives the development plan §21.1 profiles against a **real** API process and
**real** scheduler worker processes. Nothing about the measurement is simulated: latency is timed
around HTTP calls, Event and Run counts come from the database, and PostgreSQL CPU is read from
`/proc` so the "DB CPU < 70 %" context is measured rather than assumed.

## Profiles

`tests/load/profile.py` holds the profiles as data.

| Profile | API writes/s | Messages/s | Population | Due/min |
|---|---|---|---|---|
| `normal` | 20 | 10 | 50 Humans, 20 Agents, 100 Channels, 20 Bridges, 100 Schedules | 20 |
| `peak` | 60 | 30 | same population | 60 |
| `smoke` | 2 | 1 | same population | 2 |

`peak` is `normal` scaled by three, which is what V-P7-03 sustains for 30 minutes. The population
stays as seeded when a profile is scaled; only the rates and due volume change.

## Running it

```
uv run python -m tests.load.run --profile peak --minutes 30
uv run python -m tests.load.run --profile smoke --minutes 1 --json report.json
```

The CLI creates a disposable migrated database, seeds the population, runs the traffic, prints the
report as JSON and exits non-zero when a §21.1 criterion is missed. `AGENT_COLAB_TEST_DATABASE_URL`
supplies the maintenance URL unless `--database-url` is given.

The same harness backs two pytest evidence runs, so continuous integration can run a short version
of exactly the code the evidence used:

```
AGENT_COLAB_LOAD_MINUTES=2  uv run pytest tests/e2e/test_load_peak.py -q -s
AGENT_COLAB_SOAK_MINUTES=5  uv run pytest tests/e2e/test_soak.py -q -s
```

## Server processes

One Python interpreter serves roughly 25 writes per second on a 24-core host and then saturates a
single core, because the request path is CPU-bound rather than database-bound. That ceiling is
below the peak profile, so the server must be run with more than one process:

| API processes | Throughput | p50 | p95 |
|---|---|---|---|
| 1 | 25 req/s | 1583 ms | 1912 ms |
| 4 | 72 req/s | 274 ms | 2822 ms |
| 8 | 181 req/s | 108 ms | 955 ms |

Measured at 40 concurrent writers, which is why the p95 column is far above the p95 seen at the
peak profile's offered rate. `agent-colab --workers N` (or `AGENT_COLAB_WORKERS`) forks N worker
processes; the harness defaults to eight and takes `--api-workers`.

Peak offers 90 requests per second, so capacity has to sit above that with room to spare. Four
processes are not enough: a full 30-minute run held every latency and integrity criterion but
settled at 47 writes/s against the 60 it was offered, because ~72 req/s is the whole four-process
budget. Size a deployment from the offered rate, not from the p95 of a short run.

The notification rule set is parsed once per file version rather than once per command. Re-reading
and re-validating `policy/notification-rules.yaml` on every dispatch cost 30 ms of a 70 ms command,
which is the single largest saving the load run found.

`AGENT_COLAB_DB_POOL_SIZE` and `AGENT_COLAB_DB_MAX_OVERFLOW` size the SQLAlchemy pool per process;
the defaults (20 + 20) leave room for the sync-endpoint threadpool.

Every child process writes its output to `AGENT_COLAB_LOAD_LOG_DIR` (a temporary directory by
default) rather than to a pipe. This is not a convenience: an undrained pipe fills at 64 KiB and
then blocks the child mid-request, which is indistinguishable from a server that has stopped
responding.

## What the traffic is

Writes are `POST /api/v1/tasks` — a real command through the bus, with acceptance criteria, that
appends an Event. Reads are `GET /api/v1/tasks`. Both authenticate with seeded service tokens and
are spread across the profile's channels. Two scheduler workers claim and execute the Runs the
100 Schedules produce on their five-minute cycle.

## Pass criteria (V-P7-03)

* write p95 ≤ 500 ms, read p95 ≤ 300 ms
* 5xx below 1 % of requests (a transport failure counts as a server error)
* zero duplicate occurrences — no `(schedule_id, occurrence_key)` with two Runs
* zero duplicate Event identities — no `(aggregate_type, aggregate_id, aggregate_seq)` twice
* every accepted write recorded an Event, so nothing was lost

## Soak (V-P7-04)

The criterion names 24 hours. `tests/e2e/test_soak.py` runs a bounded window instead
(`AGENT_COLAB_SOAK_MINUTES`, default 30) and looks for the *growth* signals a 24-hour soak exists
to catch, comparing the start of the window with its end:

| Watched | Failure it catches |
|---|---|
| worker resident memory | a leaking scheduler worker |
| database connections | connections not returned to the pool |
| open work items | a queue that only grows |
| stuck claimed Runs | a lease never recovered |
| dead-lettered deliveries | a relay that gave up |
| duplicate dedupe keys | a delivery queued twice |

A leak of any size shows as a trend rather than a single bad sample, which is what makes a bounded
window informative. The duration actually executed is printed with the evidence, so the difference
from 24 hours is visible rather than implied; a release rehearsal should run the full day with
`AGENT_COLAB_SOAK_MINUTES=1440`.
