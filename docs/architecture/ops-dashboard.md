# Operations dashboard and audit explorer (P4-02)

## Dependency probes (`server/ops/probes.py`)

`postgres`, `secret_provider` (`AGENT_COLAB_SECRET_PROVIDER`), `mattermost`
(`AGENT_COLAB_MATTERMOST_URL`, `/api/v4/system/ping`), `storage` (artifact root writable),
`telegram` (`TELEGRAM_BOT_TOKEN`, `getMe`; the token never appears in details) and `smtp`
(`AGENT_COLAB_SMTP_HOST` TCP connect). Optional dependencies that are not configured report
`unconfigured` (not an alert). Results are cached in `dependency_probes`; a read re-probes entries
older than 60 s (`STALE_S`) or all of them with `?refresh=1`, so an injected failure is reflected
within one window (V-P4-16). Probers are replaceable (`set_prober`) for fault injection.

## Overview (`GET /api/v1/ops/overview`, permission `admin.settings`)

`dependencies`, `alerts` (critical for postgres/secret provider, warning otherwise), Tasks by
status, Agents by status/online, outbox pending/failed/dead per kind prefix, `last_backup`
(`backups` metadata table), `maintenance` (P4-13 status when present) and pending hard-delete
requests. `GET /api/v1/ops/dependencies`, `GET /api/v1/ops/backups`.

## Audit explorer (`server/ops/audit_search.py`; permission `admin.audit`)

`GET /api/v1/audit?from=&to=&actor=&action=&target_type=&target_id=&result=&cursor=&limit=`
(stable order `occurred_at, id`, opaque cursor, `limit ≤ 100`; `action` accepts a `prefix.*`).
`GET /api/v1/audit/export?format=csv|jsonl` streams the same rows. Rows are served as stored:
`redacted_metadata` is never de-redacted (V-P4-23).
