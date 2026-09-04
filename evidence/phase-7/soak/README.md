# Soak evidence

`rss-only-aborted-2026-09-03.jsonl` is the first 24-hour attempt, stopped after 2.6 hours (156
samples) because it measured the wrong quantity. It is kept because it is the evidence for the
change, not because it is a passing run.

It samples `server_rss_kb` and `worker_rss_kb` only. Summed `VmRSS` counts a shared page once per
process that maps it, and the eight API workers are forks of one parent, so the sum rises as
inherited copy-on-write pages are written to and stop being shared — allocating nothing. Fitting
the server series from the end of warm-up:

| Model | R² | Extrapolated to 24 h |
|---|---|---|
| linear | 0.9735 | 1.263x |
| √t | 0.9967 | 1.114x |
| log t | 0.9765 | 1.063x |

A leak is linear. The best fit by a wide margin is √t, the shape of a self-limiting process.
`tests/integration/test_write_path_memory_db.py` then settled it from inside the process: 4,000
real commands retain no Python objects.

The accepted run samples `server_private_kb` and `worker_private_kb` as well, and the leak bound
reads those. See `docs/operations/load-testing.md`.
