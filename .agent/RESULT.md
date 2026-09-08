# Agent-Colab implementation result

## Summary

Implemented the simple connection slice with test-first runner, instruction and relay contracts.
The runner supports all seven requested kinds, env overrides, native stdin commands (ChatGPT
requires an operator wrapper), redacted config/output, once/dry-run operation and structured
work results. Authenticated REST poll/start reuse the existing command bus and ownership checks.
Web Admin shows one-time registration tokens and per-Agent connection instructions; Bridges
shows concrete Mattermost/Telegram recipes. A local relay provides Mattermost WebSocket intake,
Telegram registration/webhook setup and persistent-offset private-network polling. The existing
Telegram bridge Test API now receives its configured client. Operator runbook includes exact
values, private callback URLs, API bodies, manual/systemd operation and verification.

No baseline or verification files were edited. No live provider calls/messages were made.
Product names occur only in executable catalogs, not core roles. The name-role lint now permits
exactly those two catalogs; a regression test still rejects product names in core role code.

## Exact files changed

- `.agent/RESULT.md`
- `docs/runbooks/connections.md`
- `server/agents/runner_catalog.py`
- `server/runner.py`
- `server/connections.py`
- `server/connection_relay.py`
- `server/api/v1/agents.py`
- `server/api/v1/channels.py`
- `server/api/v1/work.py`
- `server/main.py`
- `tests/unit/test_agent_runner.py`
- `tests/unit/test_connection_instructions.py`
- `tests/unit/test_connection_relay.py`
- `tests/unit/test_runner_catalog_lint.py`
- `tests/unit/test_runner_routes.py`
- `tests/integration/test_work_inbox_db.py`
- `tools/name_role_lint.py`
- `web-admin/src/features/agents/AgentsPage.tsx`
- `web-admin/src/features/agents/runnerKinds.ts`
- `web-admin/src/features/bridges/BridgesPage.tsx`
- `web-admin/src/features/bridges/ProviderConnections.tsx`

The pre-existing `.agent/TASK.md`, `.agent/ACCEPTANCE.md`, `verification/phase-7/`, and unrelated
untracked `docs/architecture/agent-connection-model.md` are excluded from the commit.

## Commands and outcomes

Commands ran from the repository root unless stated otherwise. No credential files were read.

- `cat .agent/TASK.md .agent/ACCEPTANCE.md`: read before implementation.
- Read `AGENTS.md`, `PROGRESS.md`, `Makefile`, `pyproject.toml`, existing work/agent/provider APIs,
  command handlers, schemas, frontend and tests using `cat`, `sed`, `rg`, and `rg --files`.
- Initial `uv run pytest tests/unit/test_agent_runner.py -q`: could not start (`uv` missing).
  Retrying after adding `$HOME/.local/bin` did not resolve it.
- `.venv/bin/python -m pytest tests/unit/test_agent_runner.py tests/unit/test_runner_routes.py -q`:
  expected RED, missing `server.runner`, before production implementation.
- `.venv/bin/python -m pytest tests/unit/test_connection_instructions.py -q`:
  expected RED, missing `server.connections`, before production implementation.
- `.venv/bin/python -m pytest tests/unit/test_connection_relay.py -q`:
  expected RED, missing `server.connection_relay`, before production implementation.
- `.venv/bin/python -m pip install uv -q`: installed the missing tool into the existing local venv;
  no dependency manifest/lockfile change.
- `.venv/bin/uv run pytest tests/unit/test_agent_runner.py -q`: native-default test failed before
  replacing wrapper defaults with documented OpenClaw/Hermes/Gemini commands; green afterward.
  Other runner tests cover redaction, seven overrides/prompts, safe real `/bin/cat` execution,
  no-side-effect config, success/nonzero/timeout/missing executable and result schema validation.
- `.venv/bin/uv run pytest tests/unit/test_connection_instructions.py -q`: strengthened panel-render
  test failed before adding the missing `<ProviderConnections />` render; green afterward.
- `.venv/bin/uv run pytest tests/unit/test_runner_catalog_lint.py -q`: RED before exact catalog
  exceptions; green afterward, including rejection of a hard-coded core role.
- Targeted `.venv/bin/ruff check <changed Python files> --fix` and
  `.venv/bin/ruff format <changed Python files>`: fixed imports/formatting. Initial multiline-string
  formatting attempt caused a syntax error; corrected before green tests.
- `.venv/bin/ruff format docs/runbooks/connections.md`: formatted Python code fences required by CI.
- `.venv/bin/ruff check . --output-format concise`: passed.
- `.venv/bin/mypy`: passed (final CI checks 325 source files).
- `.venv/bin/uv run bandit -q -c pyproject.toml -r server/runner.py server/connections.py server/connection_relay.py`:
  passed after line-specific exceptions for literal instruction placeholders and operator-controlled
  subprocess argv with `shell=False`. No security check was globally disabled.
- `pnpm run lint && pnpm exec tsc -b && pnpm run build` in `web-admin`: passed. Three existing-style
  warnings remain (state-in-effect in Roles/Agents and fast-refresh exports in auth/session).
- `.venv/bin/uv run pytest tests/unit tests/integration -q`: passed with database tests skipped
  because `AGENT_COLAB_TEST_DATABASE_URL` was unset.
- `.venv/bin/uv run pytest tests/unit tests/integration -q -o addopts=''`: final explicit-count run;
  **1314 passed, 323 skipped in 8.21s**.
- `docker ps --format '{{.Names}}'`: direct access denied. The documented `sg docker -c` wrapper
  worked; no running deployment containers were changed.
- `sg docker -c 'make test-db-up TEST_POSTGRES_CONTAINER=agent-colab-runner-test-pg TEST_POSTGRES_PORT=54339'`:
  started a dedicated disposable PostgreSQL, using only repository-default disposable test credentials.
- Database runs used `AGENT_COLAB_TEST_DATABASE_URL` pointing at that disposable localhost:54339
  maintenance database. Its fixture creates/migrates/drops a fresh database for every pytest session.
  Connection-string credentials are intentionally omitted from this report.
- With that env set: `.venv/bin/uv run pytest tests/unit/test_agent_runner.py tests/unit/test_connection_instructions.py tests/unit/test_connection_relay.py tests/unit/test_runner_routes.py tests/unit/test_runner_catalog_lint.py tests/integration/test_work_inbox_db.py -q -o addopts=''`:
  **41 passed in 7.31s**. Includes actual credential lookup, unauthenticated rejection, own work,
  spoofed Agent rejection, full runner REST roundtrip and all seven instruction endpoints.
- Intermediate DB roundtrip failures exposed missing pricing activation in the test fixture and
  an incorrect expected inbox state (`RESULT_RECEIVED`, not result status `SUCCEEDED`); corrected.
  One intermediate full inbox rerun hit the existing concurrent-poll timing assertion, and its
  shared inbox leftovers affected the new roundtrip test. New tests now create isolated identities;
  the final full targeted run passed. The legacy concurrency test itself was not changed.
- `make ci UV=.venv/bin/uv`: initial attempts stopped at doc formatting, placeholder/subprocess
  Bandit findings, executable-name lint, and an import re-export type error. All corrected.
  Final completed CI: **1314 passed, 335 skipped in 8.16s**, lint/typecheck/Bandit/docs checks and Python/Web
  builds passed. DB tests are skipped in this general CI run because its database env is unset;
  the separate 41-test run above includes the new DB coverage.
- `git diff --check`: passed. Scoped diff reviewed. No `.env` or real credentials included.
  `gitleaks` was not installed; the separate optional `make secret-scan` target was not run.

- Final `make ci UV=.venv/bin/uv` rerun after implementation/test changes: exit 0.
- `sg docker -c 'docker rm -f agent-colab-runner-test-pg'`: removed only this task's disposable DB container.
- `git diff --cached --check`: passed; exactly the 21 files above staged for the scoped commit.

## Remaining gaps and operational limits

- No live third-party authentication or end-to-end provider acceptance was attempted. ChatGPT needs
  an operator-supplied stdin wrapper. Installed native CLI versions must support the documented flags;
  all seven kinds support explicit command overrides. No OAuth integration was added.
- Provider wizard creation UI is not implemented; concrete instructions/API bodies and local relay
  commands are supplied. One Mattermost origin and Telegram bot credential per server process.
- Long-running subprocesses emit no interim heartbeats. Output capture is in memory and result text
  is truncated. The runner has no durable local result spool/retry after execution; existing work
  timeout/operator recovery applies to crashes or result transport failures.
- Redaction covers secret-looking assignments/bearer strings and secret-named env values. Unknown
  unlabelled secrets or transformed secrets cannot be detected reliably. Do not put secret values in
  work payloads or request credential dumps. Exceptions from runner/relay are not printed verbatim.
- Telegram polling requires one poller per bot and persisted offset state; webhook mode requires
  reachable HTTPS ingress. Private deployments can stay private using polling.
- Full database-enabled repository CI was not run; targeted PostgreSQL coverage passed. The existing
  concurrent-poll test had one intermittent failure as recorded above.

## H verification

1. Follow `docs/runbooks/connections.md`; create/activate an Agent with `work.poll` and `agent.self`
   permissions and an initialized pricing policy. Save the one-time token in a mode 0600 host env file.
2. Select each runner kind and inspect Connection instructions; stored credentials must never appear.
3. Run `python -m server.runner --print-config`, then `--once`; use `/bin/cat` as a temporary command
   override for a credential-free work smoke test. Confirm heartbeat and `RESULT_RECEIVED` receipt.
4. Configure Mattermost through the documented register/import POSTs; test `/colab help` and actions.
5. Register Telegram locally, configure chat/topic/direction/thread mode, enable/test bridge, and run
   the documented private polling and Mattermost listener processes. Confirm both bridge directions.
6. Restore actual CLI commands, then use the supplied systemd templates for persistent operation.
