# Deployment decision record

Validation plan V-P7-18 requires that the final development report is delivered, that the user's
explicit deployment decision is recorded, and that **zero deployment action precedes that
decision**. This file is that record. It is machine-checked by `tools/deployment_decision.py`,
which is what the V-P7-18 evidence runs.

```yaml
state: PENDING_USER_DECISION
report: REPORT.md
report_delivered_at: null
target: none recorded
decision: none
decided_at: null
decided_by: null
deployment_actions: none
```

## States

| State | Meaning | Required fields |
|---|---|---|
| `PENDING_USER_DECISION` | The report exists; the user has not answered. Nothing may be deployed. | `decision: none`, no deployment ledger entries |
| `APPROVED` | The user answered yes, naming a target. Deployment may proceed. | `decision: approved`, `decided_at`, `decided_by`, `target` |
| `DECLINED` | The user answered no. Nothing may be deployed. | `decision: declined`, `decided_at`, `decided_by` |
| `DEPLOYED` | A deployment ran after an `APPROVED` decision. | everything `APPROVED` requires, plus a ledger entry per action |

`report_delivered_at` is filled in when the report is handed to the user, which happens after
Phase 7 passes and the source is pushed.

## Target

The repository's `.env` names **no deployment target**: it carries channel credentials only, no
host and no method. Until a target is supplied, `APPROVED` cannot be recorded, because the
decision would not name what it approves. Secret values are never copied into this file, into the
report, or into any evidence; only the presence or absence of a target key is reported.

## Deployment ledger

Every deployment action appends one JSON file to `release/deployments/`, recording the action, the
image digests, the target host and method (never credentials), the operator, and the timestamp.
While the state is not `APPROVED` or `DEPLOYED`, that directory must be empty. The checker treats
any entry found under a non-approved state as a violation, which is the evidence that no
deployment preceded approval.

## Changing this record

Only a human decision changes `state`. The implementing agent may fill `report_delivered_at` and
may never write `APPROVED` on its own behalf. Approval is recorded with the user's own words
quoted in `decided_by`'s adjacent note, and the corresponding Event is appended to the audit trail
by the operator performing the deployment.
