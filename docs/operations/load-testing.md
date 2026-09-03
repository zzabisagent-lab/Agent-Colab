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
uv run pytest tests/e2e/test_soak.py -q -s
```

The soak test is different: it does not drive traffic at all. It reads the sample file a recorded
24-hour run left behind and asserts the criterion against it. See [Soak (V-P7-04)](#soak-v-p7-04).

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

The criterion is 24 hours of sustained normal load with no leaks, no duplicates and nothing stuck.
A day cannot be spent inside a test, so the run and the assertions are separate programs:

* `tests/load/soak.py` **runs** the day. It creates a disposable database, seeds the normal
  population, drives real API processes, real scheduler workers, real load generators and real
  Agent heartbeats, and appends one JSON sample per minute.
* `tests/e2e/test_soak.py` **asserts** the recorded run. It reads the finished sample file and
  checks the whole series, and it fails — it does not skip, and does not fall back to a shorter
  window — when the file covers less than 24 hours.

That separation is what makes the evidence honest. A soak fails through *growth* and through
*state that stops being cleaned up*, and both are invisible in a first/last pair: a leak that only
bites after eight hours, or leases that stop being reclaimed at hour twenty, look exactly like a
healthy run when only the endpoints are compared. Reading every minute makes them visible.

### Recording a run

```
AGENT_COLAB_TEST_DATABASE_URL=postgresql://colab@127.0.0.1:5432/postgres \
  setsid nohup uv run python -m tests.load.soak \
  --profile normal --minutes 1440 \
  --samples evidence/phase-7/soak-24h.jsonl \
  --json soak-24h-summary.json > soak-24h.log 2>&1 &
```

Detach it: 24 hours outlives any shell. The runner tolerates transient failure by design — a
sample that cannot be taken is recorded with its `sample_error` and the loop continues, and a
generator that returns 5xx is counted rather than fatal. Only the clock ends the run.

`--minutes` is there for smoke runs of the machinery (`--minutes 2` exercises every code path in
about three minutes). A smoke file cannot satisfy the criterion: the coverage gate rejects it.

### Asserting it

```
uv run python -m tools.evidence run V-P7-04 -- \
  uv run pytest tests/e2e/test_soak.py -q -p no:cacheprovider
```

`AGENT_COLAB_SOAK_SAMPLES` points the test at a different sample file; it defaults to
`evidence/phase-7/soak-24h.jsonl`.

### What each sample carries

Elapsed seconds, cumulative writes, reads and 5xx errors, Events, scheduled Runs, duplicate
occurrence keys, duplicate Event identities, duplicate deliveries, duplicate bridge relays, open
work items, stuck claimed Runs, dead letters (delivery outbox plus bridge), bridge relay counts,
Agent heartbeat count and the age of the oldest heartbeat, stale Agents, database connections, and
the resident memory of the whole API and worker process trees.

### What it asserts, and why those bounds

| Watched | Bound | Failure it catches |
|---|---|---|
| coverage | ≥ 24 h, final sample present | a short window substituted for the day |
| worker and server memory | ≤ 1.10x, median of first vs last 10 samples | a leaking process |
| memory peak | ≤ 1.5x the opening level | a spike that is not reclaimed |
| database connections | last hour ≤ warmed baseline + 5 | connections never returned to the pool |
| open work items | zero at the end | a queue that only grows |
| stuck claimed Runs | zero at the end, never 3 minutes running | a lease that stopped being reclaimed |
| dead letters | zero throughout | a relay that gave up |
| duplicate occurrence keys, Events, deliveries, relays | zero throughout | anything delivered twice |
| oldest heartbeat | ≤ 90 s | heartbeats that stopped being recorded |
| 5xx rate | ≤ 1 % | the §21.1 error budget |

The memory bound is the one that needs justifying. A leak grows without bound, so the question is
only where to draw the line above allocator noise. The 30-minute window already measured 1.2 %
growth; a process leaking at that rate would be near 1 %/hour and far past 10 % in a day. Ten per
cent across 24 hours therefore sits about an order of magnitude below a real leak and still well
above arena fragmentation and per-tick buffers. Medians of ten samples at each end are compared
rather than single samples, because one minute of resident memory is noisy and a leak that matters
is visible either way.

Connections are compared with a *warmed* baseline (the first hour) rather than with the first
sample, because eight API worker processes and two scheduler workers each fill their own pool
during startup. What a soak watches for is a climb that never comes back down.

Agents are registered and activated through the command bus rather than inserted as rows, because
registry state is derived from the Agent's event stream and written back on every command: an
inserted Agent is recomputed to `pending` the first time it is touched, and every later heartbeat
is rejected with `AGENT_STATUS_INVALID`. Heartbeats carry a §7C usage block, so the seed also
activates `policy/pricing.yaml` the way a deployment does.
