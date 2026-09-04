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
