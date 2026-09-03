# Backup and restore

Default objectives: **RPO 24 hours, RTO 4 hours** (development plan §21.1). The rehearsals in
`tests/integration/test_recovery_db.py` measure a restore and assert it finishes well inside the
RTO; run them after any change to this procedure.

## What a backup contains

One manifest, `<backup_id>.manifest.json`, lists every part with its size and SHA-256:

| Part | File | Contents |
|---|---|---|
| `database` | `<backup_id>.dump` | `pg_dump` custom format, the whole database |
| `artifacts` | `<backup_id>.artifacts.tar.gz` | the artifact storage root |
| `documents` | `<backup_id>.documents.tar.gz` | the document storage root |
| `settings` | `<backup_id>.settings.json` | key, version, layer and value fingerprint — never a value |
| `ledger` | `<backup_id>.ledger.json` | the signed key-tombstone ledger |

The manifest also records `schema_revision`, the Alembic revision the dump was taken at, so an
operator knows which release can read it.

**The master key and the ledger signing key are never in a backup.** They live in the environment
or the OS secret store. That separation is what makes a stolen dump useless (V-P4-17), and it is
why a restore needs the key material supplied separately.

## Taking a backup

```bash
python -m tools.backup \
  --database-url "$AGENT_COLAB_DATABASE_URL" \
  --out /var/backups/agent-colab \
  --record --ledger --storage
```

`--record` writes the catalog row in `backups`; `--ledger` exports the tombstone ledger;
`--storage` includes both storage roots. All three belong in a scheduled full backup.

Every file is written owner-only (`0600`).

## Retention

Windows come from settings, with built-in defaults of 7 daily, 4 weekly and 6 monthly:

| Setting | Default | Keeps |
|---|---|---|
| `backup.retention_daily` | 7 | the newest backup of each of the last 7 days |
| `backup.retention_weekly` | 4 | the newest of each of the last 4 ISO weeks |
| `backup.retention_monthly` | 6 | the newest of each of the last 6 months |

A backup kept by any window is kept. Pruning deletes the catalog row and every file of that
backup id:

```bash
python -m tools.backup --database-url "$URL" --out /var/backups/agent-colab --apply-retention
python -m tools.backup ... --apply-retention --dry-run   # plan only
```

`--now <iso>` supplies the instant used for the decision. That is how the rehearsal crosses a
daily, weekly or monthly boundary without waiting; nothing else about the path differs. A clock
that moves *backwards* deletes nothing: backups newer than the given instant are always kept.

## Restoring

```bash
python -m tools.restore \
  --manifest /var/backups/agent-colab/<backup_id>.manifest.json \
  --database-url "postgresql://…/<empty database>" \
  --artifact-root /var/lib/agent-colab/artifacts \
  --document-root /var/lib/agent-colab/documents
```

The order is fixed and the tool enforces it:

1. **Verify** every part against its SHA-256. A mismatch stops the restore before anything is
   loaded, and the gate stays closed.
2. **Close the gate.** A marker is written beside the sealed bootstrap state
   (`restore-pending.json`, or `AGENT_COLAB_RESTORE_MARKER`). While it exists `/readyz` answers
   **503** with `restore.pending`, so the instance cannot be put into service half-restored.
3. **Load** the database, then each storage root.
4. **Reconcile the tombstone ledger.** Every DEK destroyed after the backup was taken is shredded
   again and tombstoned, so a restore can never resurrect hard-deleted content (V-P7-20).
5. **Open the gate** — only when the ledger verified and nothing is left unknown. An interrupted
   restore therefore leaves a closed instance, never an open one.

Exit code 1 means the gate is still closed; read the report's `ledger` and
`checksum_mismatches` fields before retrying.

After restoring, supply the master key and ledger key the same way the original instance had them,
then start the service and confirm `/readyz` is 200.

## Verifying a restore

```bash
python -m server.projections.runner rebuild tasks    # and approvals, agents, schedules
```

A full Event replay must reproduce the live snapshot hashes (V-P7-08). The rehearsal test
compares Event, Schedule, ScheduleVersion, Run, Approval, ExternalIdentity, Task and Artifact
state between source and restored instance and requires them to be equal.
