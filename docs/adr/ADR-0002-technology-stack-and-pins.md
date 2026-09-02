# ADR-0002: Technology stack and version pins

- Status: Accepted (Phase 0)
- Date: 2026-09-02

## Decision

The development plan §5 stack is adopted; exact versions are locked by `uv.lock` and
`web-admin/pnpm-lock.yaml` (P0-01). Pins at Phase 0:

| Area | Choice | Pinned |
|---|---|---|
| Runtime | Python | 3.12.3 |
| Web framework | FastAPI / Pydantic v2 / uvicorn | 0.141.1 / 2.13.5 / 0.52.4 |
| DB | PostgreSQL / SQLAlchemy / Alembic / psycopg | 16.15 / 2.0.52 / 1.19.1 / 3.3.5 |
| Contracts | jsonschema (Draft 2020-12) / PyYAML | 4.26.0 / 6.x |
| Agent protocol | `mcp` Python SDK (Streamable HTTP) | 2.1.1 |
| Crypto | `cryptography` | 50.0.1 |
| Tests | pytest / hypothesis / Playwright (Phase 4+) | 9.1.1 / 6.167.1 |
| Quality | ruff / mypy (strict) / bandit / pip-audit / oxlint / tsc | 0.16.5 / 2.3.1 / — / — / 1.79 / 6.0 |
| Admin web | React / Vite / TypeScript, pnpm | 19.2 / 8.2 / 6.0, pnpm 11.25 |
| Mattermost | Team Edition (spike and integration test target) | 11.10.1 |
| Delivery | OCI images + Docker Compose; GitHub Actions CI | compose.yaml (P0-04) |

Rules:

- Canonical JSON is RFC 8785 (JCS); hashes are SHA-256 (development plan §6.3).
- The cron parser is implemented in-house against spec §8.6; `croniter` is a dev-only reference
  implementation for cross-checking previews (V-P5-02) and never a runtime dependency.
- Dependency upgrades land only through `uv lock --upgrade-package` with CI green; no floating
  versions at build time.
