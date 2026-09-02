# Secret scan record (P0-06 / V-P0-09)

Criterion: repo history, working tree, and configuration contain zero real credentials.

## Tooling

- `gitleaks` 8.30.1 with `.gitleaks.toml` (default rules; allow-list only for the DLP canary
  pattern `CANARY-NOT-A-SECRET-\d+`, the untouched `docs/baseline/*.md`, and `.venv`/`node_modules`).
- `.env` leakage check: a script reads `.env`, and for every value counts byte matches in all
  tracked files plus untracked `evidence/` and `docs/security/` files. It prints only lengths,
  character classes, match counts, and masked context — never a value.

## Commands and results (2026-09-02, branch `phase-0`)

| Scan | Command | Exit | Findings |
|---|---|---|---|
| history | `gitleaks git --no-banner --redact -v --config .gitleaks.toml .` | 0 | 0 leaks (3 commits, ~767 KB) |
| working tree | `gitleaks dir --no-banner --redact --config .gitleaks.toml .` | 0 | 0 leaks (~1.05 MB) |
| ignore check | `git check-ignore .env` | 0 | `.env` is ignored |
| `.env` values | masked grep over 95 tracked files + untracked evidence/docs | 0 | see below |

`.env` value results (4 keys: server name, ip address, id, password):

| Value | Length | Class | Tracked matches | Assessment |
|---|---|---|---|---|
| #1 server name | 2 | letters | not grepped (too short to be meaningful) | — |
| #2 ip address | 14 | digits/punctuation | 0 | clean |
| #3 id | 6 | lowercase letters | 11 (all inside the fixed phrase "… key" in the v8 baseline documents and text quoting them) | dictionary-word collision, not a credential occurrence; the phrase predates this repository |
| #4 password | 9 | mixed | 0 | clean |

Conclusion: zero real credentials in history, working tree, or configuration. The only textual
overlap is a common English word that the baseline documents use in an unrelated phrase.

## Ongoing controls

- `make secret-scan` and the CI `gitleaks` step run on every push.
- Evidence and Verifier reports are scanned before commit; `.env` values are never echoed.
- Test canaries must use the `CANARY-NOT-A-SECRET-<digits>` form so that any other secret-looking
  string is a finding.

## Incident record: F-P0-002-02 (Verifier Report VR-P0-002, 2026-09-02)

- **What**: `evidence/phase-0/spikes/mattermost/slash-command-delivery.json` (committed in
  `0e43f20`) contained callback material issued by the *local* Mattermost Team Edition test
  instance during the P0-10 spike: a `trigger_id`, a `response_url` hook capability, and
  (redacted at the time) the slash-command verification token. The raw Codex run log of r002
  (`verification/phase-0/run-r002/events.jsonl`, commit `4c1adf6`) reproduced the same content.
- **Exposure**: the instance listens on `127.0.0.1:8065` only and is stopped between uses; the
  repository is private. No production or third-party credential was involved.
- **Revocation**: the slash-command token was regenerated with `PUT /api/v4/commands/{id}/regen_token`
  (HTTP 200, 2026-09-02 13:37 UTC); `trigger_id` and `response_url` hooks expire by design.
- **Remediation**: all spike artifacts and the runner log were redacted (`<redacted:len=…>`), a
  dedicated gitleaks rule (`agent-colab-mattermost-callback-material`) now blocks recurrence, and
  the two pre-remediation commits are listed in the gitleaks commit allowlist with this record as
  the reason. The Verifier Report and verifier evidence were not modified.
- **Fixture false positives** (`generic-api-key`): `token_hash` SHA-256 values in
  `tests/fixtures/setup/store-documents.yaml` and generated `idempotency_key` values in
  `tests/fixtures/events/valid/` are allow-listed by line pattern; they are hashes/keys of fake data.
