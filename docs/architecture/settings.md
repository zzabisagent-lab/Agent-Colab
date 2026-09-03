# Settings (P4-04) and maintenance mode (P4-13)

## Settings (development plan §8.2)

Registry `server/settings/registry.py`: each setting has `scope`, `type` (string, integer,
boolean, url, path, enum, timezone, language, channel_ref), default, validator, `restart_required`,
`secret`, and an optional `preflight` group (`mattermost | storage | secrets`).

Resolution `server/settings/store.py`: `emergency env (AGENT_COLAB_EMERGENCY_<KEY>) > runtime
version > setup default version > built-in default`. Versions are immutable rows in
`settings_versions`; secret values are envelope-encrypted (DEK in `sensitive_keys`), non-secret
values are JSON. Every change writes a `settings.change` audit row with a redacted diff
(`before/after`; secrets carry fingerprints only) linked by `version`/`previous_version`, and a
`SETTING_CHANGED` Event (aggregate `set-<key>`). Rollback creates a new version with the old value.

REST `/api/v1/settings` (`admin.settings`, catalog action `api:settings_apply`; denial is a
normalized 404):

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/settings` | metadata + redacted values (`<redacted>` + fingerprint for secrets) |
| GET | `/api/v1/settings/{key}` | view + version history (secrets redacted) |
| PUT | `/api/v1/settings/{key}` | `{value, reason}`; validated before apply (`SETTING_TYPE_INVALID | SETTING_RANGE_INVALID | SETTING_ENDPOINT_INVALID | SETTING_PATH_INVALID | SETTING_ENUM_INVALID | SETTING_TIMEZONE_INVALID | SETTING_LANGUAGE_INVALID | SETTING_UNKNOWN`); integration settings re-run their preflight probe and fail with 409 `SETTING_PREFLIGHT_FAILED` |
| POST | `/api/v1/settings/{key}/rollback/{version}` | new version carrying the old value |
| POST | `/api/v1/settings/preflight` | `{changes}` → probe results with guidance, nothing persisted |

Secret values are never re-displayed by any endpoint.

## Maintenance mode (§11.1)

`server/maintenance/mode.py` + `/api/v1/maintenance` (`ops.manage`, actions
`api:maintenance_enter|exit`, plus `require_recent_mfa`). While active, `MaintenanceMiddleware`
answers `503 application/problem+json` (`MAINTENANCE_MODE`) with `Retry-After` for
POST/PUT/PATCH/DELETE under `/api/` and `/mcp` from principals without `ops.manage`; reads,
`/setup`, `/api/v1/auth`, `/api/v1/mfa` and the maintenance endpoints stay open. The outbox drain
is not affected; `scheduler_paused(session)` is the Phase 6 scheduler hook. Enter/exit write
`maintenance.enter|exit` audit rows and one announcement each to the ops channel
(`ops.channel_id`) through the notification outbox.
