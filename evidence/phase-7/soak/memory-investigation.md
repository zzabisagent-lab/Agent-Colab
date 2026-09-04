# Soak memory growth: what it is, and what it is not

The 24-hour soak's server private memory grows sub-linearly and is projected to land between
1.10x and 1.19x, against a 1.10x bound. This records what was measured before the run finished, so
the result is read against evidence rather than a guess. The bound was not changed.

## Ruled out

| Hypothesis | Measurement | Result |
|---|---|---|
| Python object retention, command path | 4,000 real commands in-process, tracemalloc + gc | heap +0.01 MB, objects −1. Flat. |
| Python object retention, HTTP path | 42,000 requests, one worker, growing database | private flat for 14 consecutive batches; 5 bytes/request in the second half |
| Retention under the full soak mix | writes + reads + heartbeats in-process, tracemalloc | traced heap 0.08 → 0.09 MB, objects flat, over 3,000 requests |
| Logging | same probe with the server's JSON handler enabled | heap still flat at 0.09 MB |
| Copy-on-write un-sharing | `Shared_Dirty` across both live process trees | zero. Un-sharing completes within seconds; RSS growth *is* private growth |
| Allocator arena proliferation | threads and 64 MB anon mappings in a live worker | 4 threads, 1 arena. Not thread-pool arena growth |

## What is left

Growth is anonymous, the Python heap under it is flat, and the mapping count is constant at 402.
That combination is intra-arena fragmentation: memory Python has freed which glibc holds rather
than returning. It grows sub-linearly — √t fits the server series at R²=0.992 against a straight
line at R²=0.948 — which is the shape of fragmentation approaching saturation, not of a leak.

Measured per-endpoint retention at steady state, for scale:

| Path | Retained per request |
|---|---|
| `POST /api/v1/tasks` + `GET /api/v1/tasks` | 5 bytes |
| `POST /api/v1/agents/{id}/heartbeat` | 105 bytes |

The heartbeat figure is genuine and worth reducing, but at the soak's one beat per second it
accounts for roughly 0.4 MB/h of an observed 7.4 MB/h. It does not explain the trend.

## Why the bound was left alone

Moving `PRIVATE_GROWTH_LIMIT` after seeing the trajectory would make the evidence worthless. If the
run ends above 1.10x it is recorded as a failure of the criterion as written, with this
investigation attached, and the cause is addressed rather than the threshold.
