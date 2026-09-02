# Approval Core (P1-08)

Authority: spec §8.4, §9.1, §9.3; development plan §6.1, §6.7, §7E, §7.2, §21.1.

## Data authority

| Table | Role |
|---|---|
| `approval_grants` | command authority: subject, action, resource scope, risk, validity, `max_uses`, quorum, status |
| `approval_decisions` | append-only ledger of every APPROVE/REJECT with `decided_by` and credential snapshot; `UNIQUE(approval_id, decided_by)` makes "the same Human twice" impossible |
| `approval_consumptions` | append-only consumption ledger; `UNIQUE(approval_id, consumption_key)` gives idempotent consumption |
| `approvals_projection` | display only; rebuilt from `APPROVAL_*` Events + the decision ledger; never consulted for decisions |

Every state change appends exactly one Event on the `approval` aggregate (`APPROVAL_REQUESTED`,
`APPROVAL_GRANTED`, `APPROVAL_REJECTED`, `APPROVAL_CANCELLED`, `APPROVAL_EXPIRED`,
`APPROVAL_ESCALATED`, `APPROVAL_REVOKED`, `APPROVAL_CONSUMED`) with `expected_seq` compare-and-swap
and updates the grant status and the projection read-after-write in the same transaction.

## States

`PENDING → APPROVED → PARTIALLY_CONSUMED* → CONSUMED`; from `PENDING|APPROVED|PARTIALLY_CONSUMED`
to `REJECTED|CANCELLED|EXPIRED|REVOKED`. Terminal states reject every write (`APPROVAL_TERMINAL`).
`APPROVAL_REQUESTED` creates the grant in `PENDING` (spec §8.4 "REQUESTED → PENDING").

## Subjects

`task` and `action` are active in Phase 1; `schedule` and `run` return
`SUBJECT_TYPE_NOT_ACTIVE` until P5-01 activates them. Exactly one `(subject_type, subject_id)`
identifies the target; a consumption for a different subject is `APPROVAL_SCOPE_MISMATCH`.

## Approver eligibility (§7E)

Order of checks in `server/approvals/eligibility.py`: principal active → not the requester, the
implementing Agent, or an alias of either (`account_aliases`, `effective_principal`) →
not already decided (`APPROVER_DUPLICATE`) → Human-only for risk HIGH+ or
`requires_human_approval` → `approval.decide` via the Policy Engine (channel membership,
explicit deny, capability) → a matched Role with `max_risk ≥ risk` → re-authentication for
HIGH+ (`REAUTH_REQUIRED`). Quorum comes from `policy/risk-rules.yaml` (LOW 0, MEDIUM 1, HIGH 1,
CRITICAL 2 distinct Humans). `APPROVED` is appended only when the quorum is met; earlier approvals
are ledger rows only.

Denials are audited (`approval.deny` / `policy.deny`, redacted) in an **independent transaction**
so that the audit survives the rejected command's rollback.

## Bounded atomic consume

`consume_approval(session, store, clock, *, approval_id, consumption_key, consumed_by,
consumed_for, correlation_id)` locks the grant row (`FOR UPDATE`), validates status and the
validity window with the injected Clock, checks the subject, counts the ledger, inserts the
consumption, appends `APPROVAL_CONSUMED`, and advances the status — all inside the caller's
transaction, so the Task creation / Run claim / action outbox commits together with it (§6.7).
A retry with the same `consumption_key` replays the recorded result; over-use is
`APPROVAL_EXHAUSTED`; expired/revoked/rejected/cancelled grants are `APPROVAL_NOT_USABLE`.

## Expiry and escalation

`ExpireApprovals` (service identity) expires every non-terminal grant whose validity ended,
appending `APPROVAL_EXPIRED` and `APPROVAL_ESCALATED` (`escalation_role` from the catalog,
default `role-administrator`). Reminder time = 50% of the validity window (`reminder_at`).

## Commands (bus)

`RequestApproval`, `DecideApproval`, `CancelApproval`, `RevokeApproval`, `ExpireApprovals`,
`ConsumeApproval` (internal). Error codes are stable and transport-neutral.

## Baseline interpretations

- Partial approvals (quorum not yet met) are recorded only in the append-only decision ledger;
  the Event catalog has no "decision recorded" type. Adding an additive extension Event would make
  the projection rebuildable from Events alone (recommended for the parent).
- `requires_human_approval` on a LOW action raises the quorum to 1 Human.
