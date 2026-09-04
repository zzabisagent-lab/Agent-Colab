# V-P7-04 — the 24-hour soak: procedure and run record

The criterion (validation plan §16) is a day of sustained normal load with, in its own words,
"zero leaks, duplicates, stuck". This document is the whole procedure and the record of every
attempt, passing or not, so that a reader can reproduce the run and judge the evidence rather than
take a summary's word for it.

## 1. What the run is

`tests/load/soak.py` drives the §21.1 normal-load profile for 1,440 minutes against a real
PostgreSQL, and nothing in it is simulated:

| Component | What actually runs |
|---|---|
| API | the real `server.main` entry point, 8 uvicorn worker processes on a loopback port |
| Scheduler | 2 real `server.schedules.worker` processes, leasing and executing Runs |
| Load | 6 real `tests.load.generator` processes issuing HTTP writes and reads |
| Heartbeats | a real `tests.load.heartbeat` driver beating every Agent every 20 s with a §7C usage block |
| Sampling | one JSON line per minute, appended and flushed, so a killed run keeps its history |

The run and the assertions are deliberately separate. A day cannot be spent inside a test, so
`tests/load/soak.py` produces `soak-24h.jsonl` and `tests/e2e/test_soak.py` reads the finished
file. That separation is also what makes the check honest: a soak fails through *growth* and
through *state that stops being cleaned up*, and both are invisible in a first/last pair. Reading
every minute makes them visible.

## 2. Running it

```bash
export PATH=$HOME/.local/bin:$PATH
export AGENT_COLAB_TEST_DATABASE_URL=postgresql://colab@127.0.0.1:54329/colab_test
setsid nohup uv run python -m tests.load.soak \
  --profile normal --minutes 1440 \
  --samples evidence/phase-7/soak-24h.jsonl \
  --json evidence/phase-7/soak-24h-summary.json > soak.log 2>&1 < /dev/null &
```

Then assert it, and record the evidence:

```bash
uv run pytest tests/e2e/test_soak.py -q
uv run python -m tools.evidence run V-P7-04 -- uv run pytest tests/e2e/test_soak.py -q
```

`--minutes` exists for smoke runs of the machinery. A short file cannot satisfy the criterion:
`test_short_sample_file_is_rejected` pins that a run of less than 24 hours **fails** here rather
than being skipped or quietly accepted.

### Host prerequisite

PostgreSQL JIT must not be enabled with a broken LLVM runtime. The sampler's aggregates are
unindexed counts over tables that reach millions of rows, so their planned cost crosses
`jit_above_cost` partway through a run. `samples.database_sample` now issues `SET jit = off` on its
own session so the run does not depend on host configuration; `jit = off` is also set in this
host's `postgresql.conf`. See attempt 2 below for what happens otherwise.

## 3. What is asserted, and why

| Watched | Bound | Failure it catches |
|---|---|---|
| coverage | ≥ 24 h, final sample present, < 1% sampler errors | a short or blind window substituted for the day |
| worker and server private memory | ≤ 1.10x, median of first vs last 10 samples | a leaking process |
| resident memory ceiling | ≤ 1.35x the opening level | growth beyond what warm-up explains |
| resident memory shape | second half grows no more than the first | growth that is not decelerating |
| memory peak | ≤ 1.5x the opening level | a spike that is not reclaimed |
| database connections | last hour ≤ warmed baseline + 5 | connections never returned to the pool |
| open work items | zero at the end | a queue that only grows |
| stuck claimed Runs | zero at the end, never 3 minutes running | a lease that stopped being reclaimed |
| dead letters | zero throughout | a relay that gave up |
| duplicate occurrence keys, Events, deliveries, relays | zero throughout | anything delivered twice |
| oldest heartbeat | ≤ 90 s | heartbeats that stopped being recorded |
| 5xx rate | ≤ 1% | the §21.1 error budget |

The memory reasoning, and why the leak bound reads private memory rather than summed RSS, is in
`docs/operations/load-testing.md`.

## 4. Run record

Every attempt, including the ones that produced no usable evidence.

### Attempt 1 — 2026-09-03, 30 minutes — superseded

A bounded window offered as a substitute for the day, with growth trends in place of duration.
Verifier report `VR-P7-001.yaml` rejected it: "the supplied implementation and evidence use a
shorter substitute". Correctly so. The criterion is a duration, not a shape.

### Attempt 2 — 2026-09-03 22:04 UTC, 2.6 hours — aborted, wrong metric

Stopped deliberately. It measured memory as summed `VmRSS`, which counts a shared page once per
process that maps it and so overstates a pre-forked pool by its whole shared set — about 270 MB of
a 1,260 MB reading. Samples: `evidence/phase-7/soak/rss-only-aborted-2026-09-03.jsonl`.

I first attributed the growth to copy-on-write un-sharing. That was **wrong**, and the next run's
own data disproved it: `Shared_Dirty` is zero in both process trees, so un-sharing completes within
seconds, and private and resident memory then grow by identical amounts. What survives is only the
denominator argument above. The leak bound was moved to private memory, and RSS kept as a ceiling
and a shape check.

### Attempt 3 — 2026-09-04 00:45 UTC, 14.8 hours — void, host defect

Stopped on instruction, but already void. From 6.55 h the sampler's database queries began failing
and from 13.04 h failed continuously:

```
psycopg.errors.UndefinedFile: could not load library ".../lib/llvmjit.so":
libLLVM-17.so.1: cannot open shared object file
```

49 of 888 samples carry no database or memory fields — 5.5% against a 1% tolerance — so the run
could not have passed however long it continued. Samples:
`evidence/phase-7/soak/void-jit-14h-2026-09-04.jsonl`. Fixed on the host and in the sampler.

What its 14.8 hours did establish, over 1,008,059 writes and 509,268 reads:

| Watched | Result |
|---|---|
| 5xx errors | 0 |
| duplicate occurrence keys, Events, deliveries, relays | 0 throughout |
| dead letters | 0 throughout |
| stuck claimed Runs | 0 throughout |
| open work items | 0 throughout |
| heartbeat age | max 20.5 s, no stale Agents |
| database connections | 12–23, no climb |
| server private memory | 1,014,172 → 1,126,852 kB (**1.111x** — over bound) |
| worker private memory | 151,106 → 155,864 kB (1.032x) |

Every integrity criterion held across a million writes. The memory criterion failed.

### Attempt 4 — 2026-09-04 16:46 UTC — in progress, and final

First clean run: JIT disabled on the host and in the sampler, private memory recorded alongside
RSS, and the first samples verified to carry every database and memory field.

**This is the last attempt.** The System Owner directed a single trial and acceptance of whatever
it reports, pass or fail. So its result stands as the V-P7-04 evidence and no further soak is run:
if the memory bound is exceeded, V-P7-04 is recorded as FAILED with this procedure and the
investigation attached, and Phase 7 carries that failure rather than iterating on it. The
threshold is not adjusted to change the outcome — that was true before the decision and is
unaffected by it.

## 5. The open question

Server private memory grew past the 1.10x bound in attempt 3 and the cause is **not isolated**.
It is not an object leak — `evidence/phase-7/soak/memory-investigation.md` excludes object
retention in the command path, the HTTP path and the full traffic mix, plus logging,
copy-on-write un-sharing, allocator arena proliferation and reclaimable free memory, each by
measurement. It does not reproduce in any short run: a single real worker plateaus completely over
42,000 requests, and the same traffic in-process grows 0.4 MB over 6,000 requests.

`PRIVATE_GROWTH_LIMIT` has not been changed and will not be changed to accommodate a result.
If attempt 4 exceeds it, that is reported as a failure of the criterion as written.
