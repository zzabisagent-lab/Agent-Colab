# Task acceptance criteria (P1-11)

Authority: development plan §7D.1/§7D.2, spec §9.1 (AcceptanceCriteria), V-P1-28.

## Model

- `AcceptanceCriterion(criteria_id, statement, check_type, required)`; `check_type` ∈
  `evidence | test_command | artifact_hash | human_attest`; statement 1–2000 characters; 1–50
  entries per revision (`schemas/api/task/acceptance-criteria.v1.schema.json`).
- `criteria_id = crit-<sha256(task_id|revision|index|statement)[:16]>` — server-assigned and
  deterministic; ids sent by clients are ignored.
- Default templates per channel type live in `server/domain/criteria.py::DEFAULT_TEMPLATES`
  (`default_criteria_for(channel_type)`); they prefill `--criteria`/REST/MCP inputs. A Task
  created without criteria stays criteria-less and cannot be delegated.

## Revisions and Events

| Revision | Where pinned | Rows |
|---|---|---|
| 1 | `TASK_CREATED` / `SUBTASK_CREATED` payload field `criteria` | `task_acceptance_criteria` rows referencing that Event |
| ≥ 2 | `ACCEPTANCE_CRITERIA_REVISED` on the `task_criteria` aggregate (aggregate_id = task id, seq = revision − 1) | rows referencing that Event |

Rows are append-only (DB trigger `IMMUTABLE_ROW`); the current revision is the maximum revision
of the rows. `ReviseCriteria(task_id, criteria)` (`task.delegate` permission; terminal Tasks
rejected) is idempotent per actor/key. The `task_criteria` aggregate keeps the Task fold
(`server.domain.task`) unchanged; `tasks_projection.criteria_revision` is set by the
`IMPLEMENTATION_SUBMITTED` payload, which must carry the current revision.

## Gates (zero side effects on rejection)

- `DelegateTask`: `PRE_DELEGATE_CHECKS` → `ACCEPTANCE_CRITERIA_REQUIRED` when revision 0.
- `SubmitImplementation`: `PRE_SUBMIT_CHECKS` → `ACCEPTANCE_CRITERIA_REQUIRED` (no criteria),
  `CRITERIA_REVISION_STALE` (submitted `criteria_revision` ≠ current), `EVIDENCE_REQUIRED`
  (a required criterion has no evidence; `extra.missing` lists the ids).
- Evidence representation: `SubmitImplementation.evidence_refs` entries are
  `"<criteria_id>:<ref>"` (evidence for that criterion) or bare `"<ref>"` (general evidence that
  satisfies no criterion).

## Interpretations

- §7D.1 "default templates per channel type are provided" is implemented as templates offered to
  clients, not as implicit criteria: otherwise "delegate without criteria" (V-P1-28) could never
  occur and the requester would never state the acceptance baseline explicitly.
- `ACCEPTANCE_CRITERIA_REVISED` is an additive catalog extension (spec §9.3 has no criteria
  Event). Its required payload is `task_id, criteria_revision`; `criteria_ids[]` and `criteria[]`
  are always present but typed as additional properties because the schema generator's field
  typing table is outside this package.
