# Agent-Colab

Agent-Colab is a self-hosted collaboration operations platform where humans and diverse AI Agents
deliberate in Mattermost channels, divide work, approve, verify results, and can reconstruct every
process and deliverable.

The product baseline is the three v8 documents under `docs/baseline/` (protected; see
`docs/baseline/SHA256SUMS`). Implementation follows the development plan, and every phase is
verified independently by a separate Verifier Agent (validation plan). Progress is tracked in
`PROGRESS.md`.

## Clean bootstrap (development plan P0-01, validation V-P0-03)

Prerequisites (versions are pinned by `uv.lock` and `pnpm-lock.yaml`):

- Python 3.12 and [`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Node.js 22 and `pnpm` (`npm install -g pnpm`)
- PostgreSQL 16 for integration tests (Docker Compose stack in `compose.yaml`, or any reachable
  instance via `AGENT_COLAB_TEST_DATABASE_URL`)
- Docker Engine with the Compose plugin for the Compose stack (`make compose-up`)

From a fresh clone:

```bash
make bootstrap   # uv sync --all-extras && pnpm install --frozen-lockfile
make lint        # ruff, mypy, bandit, oxlint, tsc
make test        # pytest (unit/contract; db-marked tests skip without a database)
make build       # python wheel + web-admin production bundle
make check-docs  # traceability, deterministic-criteria, phase-DAG, plan-baseline linters
make ci          # everything above
```

Every target runs from a clean checkout without network access beyond package registries.
Never commit `.env`; copy `.env.example` and fill values locally.

## Repository layout

See development plan §4. Contracts live in `schemas/` and `policy/`; server code in `server/`;
the admin console in `web-admin/`; ADRs in `docs/adr/`; verification reports (immutable) in
`verification/phase-<n>/`; self-test evidence in `evidence/phase-<n>/`.
