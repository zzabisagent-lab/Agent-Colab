# Deployment decision record

Validation plan V-P7-18 requires that the final development report is delivered, that the user's
explicit deployment decision is recorded, and that **zero deployment action precedes that
decision**. This file is that record. It is machine-checked by `tools/deployment_decision.py`,
which is what the V-P7-18 evidence runs.

```yaml
state: PENDING_USER_DECISION
report: REPORT.md
report_delivered_at: null
target: env:deploy-server
decision: none
decided_at: null
decided_by: null
deployment_actions: none
deferred_by_owner: 2026-09-04
```

## The decision is deliberately deferred

On 2026-09-04 the System Owner directed: *"wait my order to deploy after p7 jobs"*. The decision is
therefore not missing, it is **withheld until Phase 7 is finished**, by the person whose decision
it is. `deferred_by_owner` records that, so a reader can tell a deliberate deferral from an
implementer who never asked.

This has a consequence worth stating plainly, because the Verifier has now failed V-P7-18 twice for
it. The criterion asks for the user's explicit decision to be recorded, while the development plan
(§12.2, §27A) sequences that decision *after* Phase 7 passes. A Phase cannot pass a criterion whose
satisfaction is defined to occur after it passes. The two halves of V-P7-18 are not equally
blocked, though, and only one of them is:

| Half of the criterion | State |
|---|---|
| zero deployment without approval | **satisfied** — the ledger is empty, and `tools/deployment_decision.py` proves it |
| explicit approval recorded | **not satisfied, by the owner's instruction** |

Nothing here is worked around. The Test fails while the decision is outstanding, which is the
honest state, and it is the ordering in the baseline rather than the implementation that makes it
so.

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

`.env` records one deployment server under the heading "Deploy Server Information": a server name,
an IP address, a login id and a password. Those are credentials, so the target is referred to here
by the symbolic name `env:deploy-server` and never by its values. The same rule holds for the
report, for evidence, for commit messages and for logs; `.env` is in `.gitignore`. Read the values
from `.env` at deployment time.

The record does not say the target is ready. It says where to look. Whether the host has Docker, a
database, a reverse proxy and TLS is established by the preflight in the report's deployment plan,
not by this file.

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
