# Working agreements for Agents in this repository

- Product name is **Agent-Colab** (this capitalization and hyphen) everywhere user-facing.
- The three documents in `docs/baseline/` are the source of truth; never edit them.
- Implementer (Claude Code) and Verifier (Codex) are different identities. The implementer never
  edits anything under `verification/`; the verifier never edits product code.
- Never commit secrets. `.env` is ignored; evidence and reports must be redacted.
- No Agent product name or machine is hard-coded as a core role (spec §2 principle 3).
- Work packages start only when their prerequisites are `IMPLEMENTED` (`PROGRESS.md`).
- Time-dependent code takes an injectable `Clock`; tests never wait in real time.
- Run `make ci` before committing.
