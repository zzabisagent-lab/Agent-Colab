# RB-HARD-DELETE-RESTORE — restoring a backup taken before a hard delete

- **Id:** `RB-HARD-DELETE-RESTORE`
- **Trigger:** a restore whose backup predates an executed hard delete, or an alert that a
  destroyed key was offered as a decryption target.
- **Severity:** critical. A careless restore would resurrect content the owner ordered destroyed.

## Detection

1. Compare the restore point with the deletion: `GET /api/v1/hard-delete/requests` lists executed
   requests with their execution time; a backup created before it contains the deleted content's
   ciphertext.
2. Read the key tombstone ledger export shipped beside the backup: every destroyed DEK is listed
   with its chained, signed entry.

## Isolation

1. Keep the service closed until reconciliation finishes. The restore tool performs the
   reconciliation before opening; do not start the application against a restored database by
   hand first.
2. Do not re-register a pre-deletion backup as a decryption target for anything.

## Recovery

1. Restore with the tool, which verifies the ledger chain and reconciles tombstones before the
   service opens: `uv run python -m tools.restore --backup <backup_id> --database-url <url>`.
2. Reconciliation re-shreds every secret version and sensitive key named by a tombstone in the
   restored database, so the destroyed material stays destroyed.
3. Only then start the application and exit maintenance mode.

## Post-verification

1. `server.secrets.ledger.verify_chain` passes and `is_destroyed` returns true for every DEK named
   in the ledger.
2. Attempting to read any hard-deleted target fails: the content is undecryptable and the
   `hard_delete_tombstones` marker is present.
3. Event bytes and chain hashes are unchanged by the deletion and the restore — compare the Event
   dump digest with the pre-deletion value.

## Evidence to capture

The ledger export and its verification result, the reconciliation report from the restore tool,
the failed decryption attempt, and the Event chain digest comparison.
