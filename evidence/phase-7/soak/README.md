# Soak evidence

`rss-only-aborted-2026-09-03.jsonl` is the first 24-hour attempt, stopped after 2.6 hours (156
samples) because it measured the wrong quantity. It is kept because it is the evidence for the
change, not because it is a passing run.

It samples `server_rss_kb` and `worker_rss_kb` only, and RSS was the wrong denominator: summed
`VmRSS` counts a shared page once per process that maps it, so across eight forked workers it
overstates memory by the whole shared set and makes growth look smaller as a ratio than it is.
Fitting the server series from the end of warm-up:

| Model | R² | Extrapolated to 24 h |
|---|---|---|
| linear | 0.9735 | 1.263x |
| √t | 0.9967 | 1.114x |
| log t | 0.9765 | 1.063x |

A leak is linear. The best fit by a wide margin is √t, the shape of a process that is settling
rather than accumulating. Two direct measurements then settled it, neither relying on
extrapolation: `tests/integration/test_write_path_memory_db.py` shows 4,000 real commands retain no
Python objects, and `tests/e2e/test_http_path_memory.py` shows private memory going completely flat
after warm-up under 8,400 real HTTP requests.

The accepted run samples `server_private_kb` and `worker_private_kb` as well, and the leak bound
reads those. See `docs/operations/load-testing.md`.

## `void-jit-14h-2026-09-04.jsonl` — the second aborted attempt

14.80 h, 888 samples, stopped deliberately. Void for a reason independent of its length: from
6.55 h the sampler's database queries began failing, and from 13.04 h they failed continuously.

```
psycopg.errors.UndefinedFile: could not load library ".../lib/llvmjit.so":
libLLVM-17.so.1: cannot open shared object file
```

The sampler's aggregates are unindexed counts over tables that reached 1.16 M rows. Their planned
cost crossed `jit_above_cost` partway through the run, PostgreSQL tried to JIT-compile them, and
this user-space installation ships `llvmjit.so` without an LLVM runtime, so the queries failed
outright. 49 of 888 samples carry no database fields at all — 5.5%, five times the 1% tolerance in
`test_sample_file_covers_the_full_24_hours`. The run could not have passed however long it ran.

Fixed in two places: `jit = off` in the host's `postgresql.conf`, and — so it cannot recur on any
host — `samples.database_sample` now issues `SET jit = off` on its own session. A soak must not
depend on the host's JIT configuration to record its own evidence.

What the 14.8 h did establish, over 1,008,059 writes and 509,268 reads:

| Watched | Result |
|---|---|
| 5xx errors | 0 |
| duplicate occurrence keys, Events, deliveries, relays | 0 throughout |
| dead letters | 0 throughout |
| stuck claimed Runs | 0 throughout |
| open work items | 0 throughout |
| heartbeat age | max 20.5 s, no stale Agents |
| database connections | 12–23, no climb |
| server private memory | 1,014,172 → 1,126,852 kB (**1.111×**, past the 1.10 bound) |
| worker private memory | 151,106 → 155,864 kB (1.032×) |

Every integrity criterion held across a million writes. The memory criterion had already failed at
14.8 h, as the hour-6 projection said it would.
