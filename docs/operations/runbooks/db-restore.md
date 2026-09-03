# RB-DB-RESTORE — restore the database after loss or corruption

- **Id:** `RB-DB-RESTORE`
- **Trigger:** alert `DEPENDENCY_FAILED` for `postgres`, readiness failures, or a confirmed data
  loss decision by the System Owner.
- **Severity:** critical. Default recovery objectives are RPO 24 hours and RTO 4 hours.

## Detection

1. `GET /api/v1/ops/dependencies?refresh=1` — the `postgres` probe reports `failed`; the write API
   answers 503 and readiness fails within 30 seconds of the outage.
2. Confirm the scope: connection loss, disk failure or logical corruption. Check the PostgreSQL
   log before assuming data loss.
3. List the restore points: `GET /api/v1/ops/backups` (or `SELECT * FROM backups ORDER BY created_at DESC;`),
   noting each backup's checksum and whether its ledger export is present.

## Isolation

1. Enter maintenance mode if the service is still reachable
   (`POST /api/v1/maintenance/enter`) so no new writes are accepted during the decision.
2. Stop the scheduler workers so no Run claims a lease against a database that is about to be
   replaced (SIGTERM the worker processes; they stop after the current tick).

## Recovery

1. Restore into an empty database with the repository tool, which verifies the ledger and
   reconciles key tombstones **before** the service opens:
   `uv run python -m tools.restore --backup <backup_id> --database-url <url>`.
   It exits non-zero if the ledger fails verification or a tombstone cannot be reconciled.
2. Bring the application up against the restored database and re-run migrations to head if the
   backup predates a deployed migration.
3. Exit maintenance mode and restart the workers.

## Post-verification

1. Compare hashes and state: ScheduleVersion snapshot hashes, Run states, Event chain digests,
   Approval states and ExternalIdentity links must equal the values recorded with the backup.
2. Rebuild projections from the Event stream and confirm an identical snapshot
   (`python -m server.projections.runner rebuild tasks`).
3. Confirm destroyed DEKs stay destroyed: nothing named in the key tombstone ledger decrypts, and
   `server.secrets.ledger.verify_chain` passes.
4. Record the achieved RPO (age of the restore point) and RTO (detection to service open).

## Evidence to capture

The backup manifest and checksum, the restore tool output including the reconciliation report, the
hash comparison, the projection rebuild result, and the RPO/RTO timings.
