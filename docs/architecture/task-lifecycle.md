# Task lifecycle (P1-04)

Authority: spec §8.2, §9.1, §9.3; development plan §6.1, §6.8, §7.5, §21.1.

## State machine

`server/domain/task.py` holds the only transition table (`TRANSITIONS`), mirrored one-to-one by
`tests/fixtures/tasks/transitions.yaml` (a test asserts equality). States:
`OPEN → DELEGATED → ACCEPTED → RUNNING ↔ WAITING → IMPLEMENTED → VERIFYING → VERIFIED → COMPLETED`.

| From | Event | To |
|---|---|---|
| OPEN | TASK_DELEGATED | DELEGATED |
| DELEGATED | TASK_REASSIGNED / TASK_ACCEPTED | DELEGATED / ACCEPTED |
| ACCEPTED | TASK_STARTED | RUNNING |
| RUNNING | TASK_WAITING / TASK_PROGRESS_REPORTED / IMPLEMENTATION_SUBMITTED | WAITING / RUNNING / IMPLEMENTED |
| WAITING | TASK_STARTED | RUNNING |
| IMPLEMENTED | TASK_VERIFICATION_STARTED | VERIFYING |
| VERIFYING | VERIFICATION_PASSED / VERIFICATION_FAILED / VERIFICATION_BLOCKED | VERIFIED / RUNNING / WAITING |
| VERIFIED | TASK_COMPLETED | COMPLETED |
| OPEN, DELEGATED, ACCEPTED | TASK_CANCELLED | CANCELLED |
| RUNNING, WAITING, IMPLEMENTED, VERIFYING | TASK_CANCEL_REQUESTED | CANCEL_REQUESTED |
| CANCEL_REQUESTED | TASK_CANCELLED | CANCELLED |

`COMPLETED` and `CANCELLED` are terminal: every write is `TASK_TERMINAL`. Any other pair is
`TASK_TRANSITION_INVALID`. Both are raised before any Event append (zero side effects).

## Where verification results live

`VERIFICATION_PASSED|FAILED|BLOCKED` belong to the `verification_run` aggregate (spec §9.3) and
carry the Task in the envelope `task_id`. The Task state is therefore folded from its own
stream **merged with** the result Events of the verifications started on it
(`TASK_VERIFICATION_STARTED.verification_id`), ordered by `recorded_seq`. Results for a
verification that is not the active one are ignored by the fold and rejected by the command
(`RecordVerificationResult` → `TASK_TRANSITION_INVALID`).

## Commands (`server/application/tasks.py`)

`CreateTask`, `CreateSubtask`, `DelegateTask`, `ReassignTask`, `AcceptTask`, `StartTask`,
`ReportProgress`, `MarkWaiting`, `SubmitImplementation`, `StartVerification`,
`RecordVerificationResult`, `CompleteTask`, `RequestCancel`, `CancelTask` — all registered on the
common bus (`@handles`). Each handler: `require_permission(ctx, "task.<verb>", channel_id, domain)`
→ `load_task` (fold from streams, never from `tasks_projection`) → replay detection (same actor,
scope, idempotency key already in the stream → the original Event is returned, `replayed=True`)
→ transition validation → exactly one `store.append` with `idempotency_scope="task:<verb>"`,
`expected_seq = last + 1`, `caused_by = last Event` → synchronous `tasks_projection` upsert
(read-after-write) → append-only `task_assignments` (delegate/reassign) or `task_edges`
(sub-task; the DB trigger rejects self/ancestor cycles → `TASK_GRAPH_CYCLE`).

Hooks for later packages: `PRE_SUBMIT_CHECKS` (P1-11 acceptance-criteria/evidence gate) and
`server.domain.task.register_completion_check` (P1-10 adds the FINALIZED Document requirement;
`VERIFICATION_REQUIRED` is built in, V-P1-14). `CompleteTask` reports the first unmet code and
lists all of them in `extra["missing"]`.

## Projection and rebuild (`server/projections/`)

`TasksProjector.apply` folds Events into `tasks_projection` using the same domain fold.
`runner.rebuild(session, "tasks")` deletes the table, replays every Event in `recorded_seq`
order, and records `projection_checkpoints`. `runner.snapshot_hash` = SHA-256 of the RFC 8785
canonical JSON of all rows ordered by `task_id`, **every column included**: all values derive
from Events (timestamps are the Events' `occurred_at`), so a rebuild reproduces the identical
hash (V-P1-10). CLI: `python -m server.projections.runner rebuild|snapshot tasks`.
