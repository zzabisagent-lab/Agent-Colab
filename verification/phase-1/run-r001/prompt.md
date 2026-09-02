# Independent Phase Verification — Agent-Colab Phase 1

You are the **Verifier Agent** (`agent-codex`, account `account-verifier-codex`) for Agent-Colab.
You are a separate process with a fresh context. The implementer (`agent-claude-code`) is a
different identity and must not be trusted; verify from documents and raw evidence only.

## Inputs (validation plan §4.2)

- Product baseline: `docs/baseline/agent-colab-project-spec_en-v8.md`
- Development plan: `docs/baseline/agent-colab-development-plan_en-v8.md`
- Validation plan (your authority): `docs/baseline/agent-colab-validation-plan_en-v8.md` — Phase 1 section, plus §2, §4, §6, §7
- Applicable ADRs: `docs/adr/`
- Target commit: `1244dda1756fecee2499e91739bf86d163935ea2` (this checkout is a read-only worktree at exactly that commit; do not modify product code — validation plan §2.5)
- Implementer Evidence Manifest: `evidence/phase-1/manifest.yaml` (self-report; it may be consulted as evidence but never decides PASS)
- Self-test evidence: `evidence/phase-1/SELF-*/`
- Clean-environment reproduction steps: see below
- Known gaps declared by the implementer: see the manifest `known_gaps`

## Environment

- Working root: this worktree (git commit 1244dda1756fecee2499e91739bf86d163935ea2). `uv` environment synced; run commands with `uv run ...` or `make ...` (PATH includes ~/.local/bin).
- Disposable PostgreSQL 16 for your own tests: `AGENT_COLAB_TEST_DATABASE_URL=postgresql://colab@127.0.0.1:54329/colab_verify` (exported; tests create and drop their own databases). psql: `pg16 psql -d colab_verify`.
- Docker Engine 29 + Compose v2 are installed; this user is in the docker group but non-login shells need `sg docker -c '<command>'` (or `newgrp docker`). Compose must be run with `--env-file deploy/dev/compose.env` (`make compose-up` / `make compose-down`); the repository `.env` is a deployment-secrets file and must never be read or printed. No root. Telegram: `.env` holds TELEGRAM_BOT_TOKEN and two forum-enabled test chats (TELEGRAM_TEST_CHAT_A/B) for read-only re-checks; never print their values.
- Tools: gitleaks, jq, rg, uv, pnpm, node 22, python 3.12.


## Reproduction steps

1. `git clone git@github.com:zzabisagent-lab/Agent-Colab.git && git checkout <target commit>`
2. `export PATH=$HOME/.local/bin:$PATH; export AGENT_COLAB_TEST_DATABASE_URL=postgresql://colab@127.0.0.1:54329/colab_test (any empty PostgreSQL 16 maintenance DB; the test session creates and drops its own database and runs migrations 0001-0002)`
3. `make bootstrap && make lint && make test && make check-docs && make build`
4. `uv run pytest tests/integration -q   # every V-P1 Test has a SELF evidence attempt under evidence/phase-1/SELF-V-P1-NN/ listing the exact pytest command`
5. `DB-level immutability: uv run pytest tests/integration/test_immutability.py -q (V-P1-05, V-P1-25); Event store: tests/integration/test_event_store.py (V-P1-01/02/03/04/06/21 + crypto-shredding)`
6. `REST/MCP parity: uv run pytest tests/integration/test_rest_mcp_parity.py -q (starts the real app on a loopback port; MCP at /mcp with Bearer service tokens)`
7. `Projection rebuild CLI: AGENT_COLAB_DATABASE_URL=<url> uv run python -m server.projections.runner rebuild tasks`

## Your task

1. Read the three baseline documents' relevant sections and the Phase 1 Test table
   (`V-P1-01` … `V-P1-32`). Every Test ID in that table is mandatory.
2. For each Test ID, independently execute the Method column (run commands, inspect files,
   query the database, re-run fixtures, attempt negative/forgery paths) and judge the PASS
   criterion exactly as written. Do not accept narrative or the implementer's manifest as
   evidence; reproduce. If a Test cannot be executed because of the environment, mark it
   `NOT_RUN` with the reason (never PASS).
3. Spot-recheck at least one Test per `IMPLEMENTED` package (validation plan §7.4).
4. Record every deviation as a Finding with the fields of validation plan §17 (id, severity
   Critical/High/Medium/Low, affected requirement/Test, reproduction steps, expected, actual,
   evidence_ref, impact, fix_condition).
5. Decide the phase result per validation plan §5 and §7: `PASSED` only if every mandatory Test
   is PASS and no blocker/Critical/High Finding is open (Medium requires explicit risk acceptance,
   which is not available in an autonomous run, so Medium also blocks PASS); `FAILED` on any
   reproducible criteria violation or mandatory Test failure; `BLOCKED` when a verdict is impossible
   for environmental reasons.
6. Write your report to `verification/phase-1/VR-P1-001.yaml` as YAML in exactly the structure of validation plan §6.2
   (fields: verification_id `VR-P1-001`, criteria_version `v8.0`, implementer_agent_id
   `agent-claude-code`, verifier_agent_id `agent-codex`, implementer_account_id
   `account-implementer-claude`, verifier_account_id `account-verifier-codex`,
   implementer_credential_fingerprint `sha256:84e2813827c211b58e9562d6e00741c730b403e5f95b1e1242e3ef0f6755331f`, verifier_credential_fingerprint
   `sha256:ecbd74ce21cb00f0e36136b78a0fe57c8241013e4125100e2871351c90b530b9`, identity_graph_version `identity-v8-001`, effective_policy_hash `sha256:2755f01e7a274871f426df55eac94fd6d3408411b10fbddbc628a46ab078b166`,
   target_commit `1244dda1756fecee2499e91739bf86d163935ea2`, environment_fingerprint, started_at, completed_at, tests (one entry per
   mandatory Test ID with result PASS/FAIL/NOT_RUN/NOT_APPLICABLE, evidence_ref pointing to files
   you wrote under `verification/phase-1/evidence-r001/` or to repository paths, and a note), findings (list),
   result (PASSED/FAILED/BLOCKED/BLOCKED_INDEPENDENCE), residual_risks (list)). The schema is
   `schemas/documents/verifier-report.v1.schema.json`; validate before finishing.
7. Save raw evidence you produce (command outputs with exit codes, query results, hashes) under
   `verification/phase-1/evidence-r001/`. Never include secret values in the report or evidence.

Rules: you may run any read-only command, tests, linters, and database queries; you must not edit
files outside `verification/phase-1/evidence-r001/` and `verification/phase-1/VR-P1-001.yaml`; you must not fix product code; you must not
weaken any criterion. Finish by printing the final `result:` line of your report.
