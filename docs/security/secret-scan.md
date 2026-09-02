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
