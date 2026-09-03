# RB-NAS-FULL — the artifact or document store (NAS volume) is out of space

- **Id:** `RB-NAS-FULL`
- **Trigger:** alert `DEPENDENCY_FAILED` for `storage`, upload failures, or document drafts
  recording a write failure reason code.
- **Severity:** critical. Event writes continue, but artifacts, documents and backups stop.

## Detection

1. `GET /api/v1/ops/dependencies?refresh=1` — the `storage` probe reports `failed` with its
   detail; the dashboard reflects it within 60 seconds of the probe failing.
2. `GET /api/v1/ops/overview` — check `alerts` and the outbox counters for a growing backlog.
3. On the host, confirm the real cause (`df -h` on the artifact and document roots) so a quota
   problem is not mistaken for a full disk.

## Isolation

1. Enter maintenance mode so non-administrator writes stop with 503 and a `Retry-After`:
   `POST /api/v1/maintenance/enter` with `{"reason": "storage full"}` (needs MFA re-authentication).
   The outbox drain keeps running and reads stay available.
2. Pause schedules that produce large artifacts:
   `/colab schedule pause sch-…` or `POST /api/v1/schedules/{id}/pause`.

## Recovery

1. Free space: expire retained messages and documents per policy, move cold backups off the
   volume, or extend the volume. Never delete artifact blobs by hand — the hard-delete workflow
   owns deletion (`RB-HARD-DELETE-RESTORE`).
2. Re-run the probe: `GET /api/v1/ops/dependencies?refresh=1` until `storage` is `ok`.
3. Exit maintenance mode: `POST /api/v1/maintenance/exit`.

## Post-verification

1. `GET /api/v1/ops/overview` shows `storage` ok, no alerts, and maintenance inactive.
2. Re-drive one artifact upload and one document draft and confirm both succeed.
3. Confirm the enter and exit of maintenance mode are audited and announced in the ops channel.

## Evidence to capture

The failing and recovered probe payloads, the maintenance enter/exit audit rows and announcement,
free-space figures before and after, and the successful upload and draft after recovery.
