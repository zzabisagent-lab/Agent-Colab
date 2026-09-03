# Hard delete, key tombstones, backup and restore (P4-11)

## Workflow (`server/application/hard_delete.py`, `/api/v1/hard-delete`)

1. `POST /requests` (`RequestHardDelete`, permission `admin.hard_delete`; Accounts also via
   `POST /api/v1/accounts/{id}/deletion-request`) creates `hard_delete_requests` row `hd-…`
   (`PENDING_APPROVAL`), appends `HARD_DELETE_REQUESTED` and opens a **CRITICAL** approval
   (subject `action:hard-delete:<request_id>`, quorum 2 distinct Humans, self-approval forbidden).
2. `POST /requests/{id}/decide` (`ApproveHardDelete`): each decision requires a fresh MFA
   re-authentication proof (`server.security.reauth.require_recent_mfa`, fail-closed:
   `REAUTH_REQUIRED` 401). The second APPROVE moves the request to `APPROVED_WAITING`,
   `executable_at = approved_at + hard_delete.waiting_period_hours` (setting, else
   `AGENT_COLAB_HARD_DELETE_WAITING_HOURS`, default 24) and appends `HARD_DELETE_APPROVED`.
   A REJECT ends the request (`HARD_DELETE_REJECTED`); `POST /requests/{id}/cancel` likewise.
3. `POST /requests/{id}/execute` (`ExecuteHardDelete`, re-authenticated) only after the waiting
   period (`HARD_DELETE_WAITING_PERIOD` before, `HARD_DELETE_NOT_APPROVED` without quorum).
   Execution shreds the target's DEKs (`sensitive_keys.wrapped_dek = NULL`, `status = destroyed`),
   appends a key-tombstone ledger entry per DEK, writes the immutable display-redaction marker
   `hard_delete_tombstones` (approvals, keys destroyed, ledger hash, Event-chain digest before and
   after) and appends `HARD_DELETE_EXECUTED` (+ `ACCOUNT_HARD_DELETED` for Accounts).
   Direct `DELETE` endpoints answer 405 `HARD_DELETE_WORKFLOW_REQUIRED`.

Targets: `account` (status `DELETED`, display name redacted, credentials revoked, role
assignments revoked), `conversation` (message bodies shredded + message tombstones),
`artifact` (blob unlinked), `document` (version files unlinked). Event rows are never modified:
the digest over the Workspace Event chain is identical before and after, save for the appended
Events (V-P4-25).

## Ledger

`server.secrets.ledger` (signed with `AGENT_COLAB_LEDGER_KEY_B64`, P4-05/06) records every
tombstone; without a ledger key the local `key_tombstones` hash chain is used (`LocalLedger`).
`export_ledger` produces a portable, verifiable copy (`verify_exported_ledger`) that is stored
**separately from database backups**.

## Backup and restore (`tools/backup.py`, `tools/restore.py`)

- `python -m tools.backup --database-url … --out DIR --record --ledger`: `pg_dump` custom format
  (0600), manifest with SHA-256, metadata row in `backups`; the master key and ledger key are never
  included; the ledger export is written next to the dump.
- `python -m tools.restore --dump FILE --database-url <empty db> --ledger LEDGER.json`:
  `pg_restore`, then `reconcile_tombstones` **before the service opens**: entries missing from the
  restored chain are re-appended and every listed DEK is shredded again, so a backup taken before a
  hard delete can never resurrect the destroyed key (V-P4-29). Exit 1 on ledger verification
  failure or unknown key references.
