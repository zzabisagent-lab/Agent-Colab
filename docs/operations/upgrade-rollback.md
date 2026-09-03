# Upgrade, rollback and forward fix

Target: any of these completes inside the **RTO of 4 hours** (development plan §21.1). The
rehearsals in `tests/integration/test_upgrade_rollback.py` measure each one.

## Before an upgrade

1. Take a full backup and confirm the manifest (`docs/operations/backup-restore.md`).
2. Note the current Alembic revision: `SELECT version_num FROM alembic_version`.
3. Rehearse the upgrade against a copy:

```bash
python -m tools.upgrade_rehearsal \
  --database-url "$AGENT_COLAB_DATABASE_URL" \
  --from-ref <previous release tag> --scenario upgrade
```

The rehearsal creates its own temporary database, applies the **previous tag's own migrations**
(extracted with `git archive`, so it is a genuine upgrade rather than a fresh install), seeds
workspace, account, channel, settings and secret-reference rows, upgrades to the working tree's
head, and compares fingerprints per area. It exits non-zero if any area changed or a table
disappeared.

## Upgrading

```bash
python -m alembic upgrade head     # or the release's documented entry point
```

Migrations are additive by convention: new tables and nullable columns, no destructive change to
an existing column. That convention is what makes the rollback below safe, so a release that
must break it needs the forward-fix plan written **before** it ships.

## Rolling the application back

When the new release fails but its migrations already ran, roll back the *application* and leave
the schema alone:

```bash
python -m tools.upgrade_rehearsal --database-url "$URL" --from-ref <previous tag> --scenario rollback
```

The rehearsal reports `columns_lost_for_old_release`. Empty means the previous release can read
the upgraded schema and a plain application rollback is safe. Non-empty means it cannot: restore
from backup instead, accepting the RPO, or fix forward.

## Forward fix after an irreversible migration

A migration that drops a column or rewrites data cannot be undone by downgrading — the values are
gone. The recovery is a new migration forward:

```bash
python -m tools.upgrade_rehearsal --database-url "$URL" --scenario forward-fix
```

The rehearsal performs an irreversible change, shows that downgrading cannot restore it, applies a
corrective migration, and checks the schema is serviceable again. Its `ledger` lists both steps;
record the same two steps in the change log for the real incident, because `alembic_version` alone
shows only where the schema ended, not what was lost on the way.

Procedure during a real incident:

1. Stop the application (leave the database running).
2. Write the corrective migration and rehearse it on a restored copy of the current backup.
3. Apply it forward, then restart the application.
4. Verify: `/readyz` is 200, and a projection rebuild reproduces the live snapshot hashes.

## After any of the three

- Confirm `/readyz` answers 200.
- Rebuild projections and compare snapshot hashes (V-P7-08).
- Take a fresh backup: the pre-upgrade one no longer matches the running schema.
