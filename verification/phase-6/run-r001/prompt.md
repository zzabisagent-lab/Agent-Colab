# Independent Phase Verification — Agent-Colab Phase 6

You are the **Verifier Agent** (`agent-codex`, account `account-verifier-codex`) for Agent-Colab.
You are a separate process with a fresh context. The implementer (`agent-claude-code`) is a
different identity and must not be trusted; verify from documents and raw evidence only.

## Inputs (validation plan §4.2)

- Product baseline: `docs/baseline/agent-colab-project-spec_en-v8.md`
- Development plan: `docs/baseline/agent-colab-development-plan_en-v8.md`
- Validation plan (your authority): `docs/baseline/agent-colab-validation-plan_en-v8.md` — Phase 6 section, plus §2, §4, §6, §7
- Applicable ADRs: `docs/adr/`
- Target commit: `ccf900efa67cd46aee085c36dbf6515db406ea47` (this checkout is a read-only worktree at exactly that commit; do not modify product code — validation plan §2.5)
- Implementer Evidence Manifest: `evidence/phase-6/manifest.yaml` (self-report; it may be consulted as evidence but never decides PASS)
- Self-test evidence: `evidence/phase-6/SELF-*/`
- Clean-environment reproduction steps: see below
- Known gaps declared by the implementer: see the manifest `known_gaps`

## Environment

- Working root: this worktree (git commit ccf900efa67cd46aee085c36dbf6515db406ea47). `uv` environment synced; run commands with `uv run ...` or `make ...` (PATH includes ~/.local/bin).
- Disposable PostgreSQL 16 for your own tests: `AGENT_COLAB_TEST_DATABASE_URL=postgresql://colab@127.0.0.1:54329/colab_verify` (exported; tests create and drop their own databases). psql: `pg16 psql -d colab_verify`.
- Docker Engine 29 + Compose v2 are installed; this user is in the docker group but non-login shells need `sg docker -c '<command>'` (or `newgrp docker`). Compose must be run with `--env-file deploy/dev/compose.env` (`make compose-up` / `make compose-down`); the repository `.env` is a deployment-secrets file and must never be read or printed. No root. Telegram: `.env` holds TELEGRAM_BOT_TOKEN and two forum-enabled test chats (TELEGRAM_TEST_CHAT_A/B) for read-only re-checks; never print their values.
- Tools: gitleaks, jq, rg, uv, pnpm, node 22, python 3.12.
- Test-environment credentials are exported to your process as environment variables (never print or record their values): ADMIN_PASSWORD, ADMIN_TOKEN, ADMIN_USER, BOT_TOKEN, BOT_USER_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_TEST_CHAT_A, TELEGRAM_TEST_CHAT_B.


## Reproduction steps

1. `git clone git@github.com:zzabisagent-lab/Agent-Colab.git && git checkout <target commit>`
2. `export PATH=$HOME/.local/bin:$PATH; export AGENT_COLAB_TEST_DATABASE_URL=postgresql://colab@127.0.0.1:54329/colab_test (any empty PostgreSQL 16 maintenance DB; the test session creates and drops its own database and runs migrations 0001-0021)`
3. `make ci   # lint, tests, doc checks, build; sidecar: uv run pytest sidecar/tests -q`
4. `Every V-P6 Test has a SELF evidence attempt under evidence/phase-6/SELF-V-P6-NN/attempt-NN/result.json listing the exact command; the latest attempt of each ID is PASS`
5. `Approvals (V-P6-01/02/22/29): uv run pytest tests/integration/test_approval_collaboration_db.py -q`
6. `Brainstorm (V-P6-03/04/26/27): uv run pytest tests/unit/test_brainstorm_limits.py tests/integration/test_brainstorm_engine_db.py tests/integration/test_brainstorm_decisions_db.py -q`
7. `Documents (V-P6-07..14, 19, 20, 23, 24, 28): uv run pytest tests/integration/test_document_finalizer_db.py tests/integration/test_document_provenance_db.py tests/integration/test_document_redaction_db.py tests/integration/test_document_rate_db.py tests/integration/test_schedule_period_summary_db.py tests/unit/test_narrative_citations.py -q`
8. `Artifacts and publishers (V-P6-05/06/15/16/17/18/21/25): uv run pytest tests/unit/test_artifact_malicious_fixtures.py tests/integration/test_artifact_upload_db.py tests/integration/test_artifact_quarantine_db.py tests/integration/test_artifact_links_phase6_db.py tests/integration/test_publisher_filesystem_git_db.py tests/integration/test_publisher_connector.py -q`

## Your task

1. Read the three baseline documents' relevant sections and the Phase 6 Test table
   (`V-P6-01` … `V-P6-29`). Every Test ID in that table is mandatory.
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
6. Write your report to `verification/phase-6/VR-P6-001.yaml` as YAML in exactly the structure of validation plan §6.2
   (fields: verification_id `VR-P6-001`, criteria_version `v8.0`, implementer_agent_id
   `agent-claude-code`, verifier_agent_id `agent-codex`, implementer_account_id
   `account-implementer-claude`, verifier_account_id `account-verifier-codex`,
   implementer_credential_fingerprint `sha256:84e2813827c211b58e9562d6e00741c730b403e5f95b1e1242e3ef0f6755331f`, verifier_credential_fingerprint
   `sha256:ecbd74ce21cb00f0e36136b78a0fe57c8241013e4125100e2871351c90b530b9`, identity_graph_version `identity-v8-001`, effective_policy_hash `sha256:09bc05c62439be1f12e83601046e38e70b3369ac2ce66060910a6af97dce9bfb`,
   target_commit `ccf900efa67cd46aee085c36dbf6515db406ea47`, environment_fingerprint, started_at, completed_at, tests (one entry per
   mandatory Test ID with result PASS/FAIL/NOT_RUN/NOT_APPLICABLE, evidence_ref pointing to files
   you wrote under `verification/phase-6/evidence-r001/` or to repository paths, and a note), findings (list),
   result (PASSED/FAILED/BLOCKED/BLOCKED_INDEPENDENCE), residual_risks (list)). The schema is
   `schemas/documents/verifier-report.v1.schema.json`; validate before finishing.
7. Save raw evidence you produce (command outputs with exit codes, query results, hashes) under
   `verification/phase-6/evidence-r001/`. Never include secret values in the report or evidence.

Rules: you may run any read-only command, tests, linters, and database queries; you must not edit
files outside `verification/phase-6/evidence-r001/` and `verification/phase-6/VR-P6-001.yaml`; you must not fix product code; you must not
weaken any criterion. Finish by printing the final `result:` line of your report.
