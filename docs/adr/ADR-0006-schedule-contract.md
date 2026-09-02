# ADR-0006: Schedule contract decisions (P0-08)

- Status: Accepted (Phase 0)
- Date: 2026-09-02

## Decisions

1. **In-house cron parser.** The normative grammar of spec §8.6 is narrower than any library
   (numeric-only, no names/aliases/extended tokens, DOW 7 rejected, single-value steps rejected).
   `server/schedules/cron.py` implements it directly; `croniter` stays a dev-only reference for
   cross-checking previews and is never a runtime dependency (ADR-0002).
2. **Vixie DOM/DOW OR semantics** when both fields are restricted; AND semantics otherwise.
3. **DST**: gap minutes are skipped and surfaced as `DST_GAP`; fold minutes run once at the first
   UTC instant and are surfaced as `DST_FOLD`. This follows spec §8.6 ("run once per wall-clock
   occurrence key"). Development plan §10A.3 says "by default", but no alternative behaviour is
   specified anywhere, so no configuration knob is offered.
4. **Occurrence key format**: `SHA-256("<schedule_id>|<timezone>|<YYYY-MM-DDTHH:mm>")`, hex,
   exactly as development plan §10A.3. Manual/retry Runs use `MANUAL:` / `RETRY:` prefixed
   deterministic keys; scheduled Runs use `SCHEDULED:<schedule_id>:<occurrence_key>`.
5. **Minimum interval** is computed exactly (minimum wall-clock gap over a 28-year window) rather
   than heuristically; default 5 minutes, floor 1 minute (§21.1 unchanged).
6. **Defaults unchanged**: concurrency `FORBID`, missed run `RUN_ONCE`, retry 3 × (1/5/25 s,
   0–20 % jitter), REPLACE timeout 60 s, poll 15 s / claim lease 60 s / heartbeat 15 s.
7. **Cancel classification**: `PENDING|DUE` are "pending" (immediate `CANCELLED`);
   `CLAIMED|TASK_CREATED|RUNNING|VERIFYING` are "running" (`CANCEL_REQUESTED` first). A claimed
   Run may already hold a lease and a Task, so it is treated as running.
8. **Lease-expiry recovery** is modelled as `CLAIMED→DUE` (the claim is released and re-claimed by
   exactly one runner); no other backward transition exists.
9. **Action templates** are validated structurally (allowed actions) and content-wise
   (shell keys/strings, secret-valued keys) so that V-P5-26 has a single enforcement point.

## Consequences

- Phase 5 (P5-01..P5-06) implements persistence and execution on top of these pure functions and
  schemas; DB CHECK constraints in the Phase 5 migration must mirror the tables here.
- Any change to the grammar, key format, or defaults is a change-managed decision (spec §21).
