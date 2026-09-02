# ADR-0004: Deterministic verification defaults

- Status: Accepted (Phase 0)
- Date: 2026-09-02

## Decision

Every default in development plan §21.1 is adopted unchanged. No default is loosened; none is
tightened in Phase 0. The values are encoded once in `server/domain/defaults.py` and referenced
by policy files, schemas, and tests so that implementation and verification share one source.

Time-dependent behaviour (heartbeat, timeouts, retention, cron, DST, retry backoff) always takes
an injectable `Clock` (`server/domain/clock.py`); production uses the system UTC clock and tests
use a fixed or stepping clock with pinned `tzdata`. No test waits in real time.
