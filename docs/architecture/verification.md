# VerificationRun core (P1-06)

Authority: spec §8.5, §9.1 (VerificationRun), §15.12/20; development plan §3.1 Verification,
§6.4; validation plan §4.1, §5, §6.2. Tests: V-P1-12, V-P1-13, V-P1-14, V-P1-24.

## Model

| Table | Role |
|---|---|
| `verification_runs` | one run per (target, implementer, verifier) with `status`, `current_revision`, `result`, `snapshot_hash`; identity columns are immutable (trigger) and DB CHECKs reject equal implementer/verifier account, agent, or credential fingerprint |
| `credential_identity_snapshots` | immutable creation-time snapshot: both accounts, agent ids, credential fingerprints, alias edges touching either party, identity graph version, effective policy hash, criteria version, target commit; `snapshot_hash` = SHA-256 of the RFC 8785 JSON |
| `verification_revisions` | append-only hash chain (`server.events.chain.VERIFICATION_CHAIN`): one row per verdict with the report, its SHA-256, the submitter identity, and the Event id |
| `verification_evidence`, `verification_findings` | append-only evidence refs and findings per revision |

States (validation plan §5): `PLANNED → ASSIGNED → RUNNING → PASSED | FAILED | BLOCKED | CANCELLED`,
`FAILED|BLOCKED → FIX_SUBMITTED → RECHECK_ASSIGNED → RUNNING`. `PASSED` and `CANCELLED` are terminal
(`VERIFICATION_TERMINAL`); every other pair is `VERIFICATION_TRANSITION_INVALID`. The table is
`server.verification.runs.TRANSITIONS` and is exhaustively tested.

## Commands (bus; REST at `/api/v1/verification-runs`)

| Command | Who | Effect |
|---|---|---|
| `CreateVerificationRun` | `verification.assign` | independence check (account, agent, credential, alias graph via `account_aliases`, DB CHECK) → run + snapshot + `VERIFIER_ASSIGNED` |
| `AssignVerifier`, `RequestRecheck`, `CancelVerification` | `verification.assign` | state moves, audited |
| `StartVerification` | the verifier | `ASSIGNED|RECHECK_ASSIGNED → RUNNING` |
| `SubmitEvidence` | implementer or verifier | evidence rows for the next revision |
| `SubmitVerdict` | the verifier only | validates the report (`schemas/documents/verification-verdict.v1.schema.json`; `PASSED` needs every test PASS/NOT_APPLICABLE and no open Medium+ finding), appends `VERIFICATION_<RESULT>` on the `verification_run` aggregate (Task targets go through `RecordVerificationResult` so the Task projection moves in the same transaction), then the chained revision row |
| `SubmitFix` | the implementer | `FAILED|BLOCKED → FIX_SUBMITTED` |

Self-verification (V-P1-12): the implementer account, any alias resolving to the same effective
principal, or any credential with the implementer's fingerprint gets `SELF_VERIFICATION_FORBIDDEN`
(409) and an audit row `verification.self_submit_rejected` written in an autonomous transaction so
it survives the command rollback; the revision INSERT is additionally guarded by a `WHERE NOT
EXISTS` against the run's implementer identity, and same-identity runs cannot be created at all
(DB CHECK). Any other non-verifier account is `VERIFIER_MISMATCH`.

Revisions (V-P1-13): results are corrected only by a new revision; rows are protected by the
`IMMUTABLE_ROW` trigger and by the missing UPDATE/DELETE grant of the application roles, and the
chain is recomputable (`verify_chain`).

Snapshot (V-P1-24): renaming accounts, rotating credentials, or adding alias edges later never
changes the stored snapshot or hash; `independence_from_snapshot` re-evaluates independence purely
from the stored bytes.

## Completion gate (V-P1-14)

`server.verification.gate.verification_gate(session, target_type, target_id)` reads the latest
non-cancelled run; `require_verified` raises `VERIFICATION_REQUIRED`. The Task package derives the
same rule from the Event stream (`completion_prerequisites`); P1-10 registers the FINALIZED
Document check through `register_completion_check`.

## Notes for the parent

- `Runtime.resolve_workspace` picks the first workspace; multi-workspace test databases must set
  `app.state.runtime.workspace_id` (the tests do). Deriving the workspace from the principal's
  account would be more robust.
- Non-spec state moves (assign/start/fix/recheck/cancel) are audited rather than evented because
  spec §9.3 defines only `VERIFIER_ASSIGNED` and the three result Events; if Events are wanted,
  add additive catalog types.
