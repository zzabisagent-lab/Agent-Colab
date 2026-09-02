"""Acceptance-criteria commands and Task hooks (P1-11; development plan §7D.1).

Authority: append-only ``task_acceptance_criteria`` rows (one row per criterion per revision),
each referencing the Event that pinned the revision — ``TASK_CREATED``/``SUBTASK_CREATED``
(revision 1, criteria carried in the payload) or ``ACCEPTANCE_CRITERIA_REVISED`` (revision ≥ 2,
appended on the ``task_criteria`` aggregate keyed by the Task id). Two gates are registered on
the Task handlers: delegation needs ≥ 1 criterion (``ACCEPTANCE_CRITERIA_REQUIRED``) and
implementation submit needs evidence for every required criterion of the current revision
(``EVIDENCE_REQUIRED``); both run before any Event append (zero side effects).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.application import tasks as task_app
from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.domain.criteria import (
    AcceptanceCriterion,
    CriteriaError,
    build_revision,
    evidence_satisfies,
    parse_evidence_refs,
)
from server.domain.task import TaskState, TaskStatus
from server.events.store import AppendRequest, EventStoreError

CRITERIA_AGGREGATE = "task_criteria"


@dataclass(frozen=True)
class ReviseCriteria(Command):
    """Pin a new criteria revision for a Task (previous revisions stay untouched)."""

    task_id: str
    criteria: tuple[dict[str, Any], ...]
    idempotency_scope: str = "task_criteria:revise"


@dataclass(frozen=True)
class CriteriaRevision:
    revision: int
    criteria: tuple[AcceptanceCriterion, ...]


# ---------------------------------------------------------------- reads


def current_criteria(session: Session, task_id: str) -> CriteriaRevision:
    """Latest pinned revision from the append-only rows (revision 0 = no criteria)."""
    rows = session.execute(
        text(
            "SELECT criteria_id, revision, statement, check_type, required "
            "FROM task_acceptance_criteria WHERE task_id = :t "
            "AND revision = "
            "(SELECT max(revision) FROM task_acceptance_criteria WHERE task_id = :t) "
            "ORDER BY criteria_id"
        ),
        {"t": task_id},
    ).all()
    if not rows:
        return CriteriaRevision(0, ())
    return CriteriaRevision(
        int(rows[0][1]),
        tuple(
            AcceptanceCriterion(str(r[0]), str(r[2]), str(r[3]), bool(r[4]))
            for r in sorted(rows, key=lambda r: str(r[0]))
        ),
    )


# ---------------------------------------------------------------- creation (revision 1)


def prepare_initial(task_id: str, raw: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate creation-time criteria before any Event append; empty input pins nothing."""
    if not raw:
        return []
    try:
        return [c.as_dict() for c in build_revision(task_id, 1, raw)]
    except CriteriaError as exc:
        raise CommandError(exc.code, exc.detail, status=400) from exc


def _insert_rows(
    session: Session,
    task_id: str,
    revision: int,
    criteria: Sequence[Mapping[str, Any]],
    event_id: str,
) -> None:
    for c in criteria:
        session.execute(
            text(
                "INSERT INTO task_acceptance_criteria (criteria_id, task_id, revision, statement, "
                "check_type, required, event_id) VALUES (:c, :t, :r, :s, :k, :q, :e)"
            ),
            {
                "c": c["criteria_id"],
                "t": task_id,
                "r": revision,
                "s": c["statement"],
                "k": c["check_type"],
                "q": bool(c.get("required", True)),
                "e": event_id,
            },
        )


def persist_initial(
    ctx: CommandContext, task_id: str, event_id: str, criteria: Sequence[Mapping[str, Any]]
) -> None:
    if criteria:
        _insert_rows(ctx.session, task_id, 1, criteria, event_id)


# ---------------------------------------------------------------- revise (revision >= 2)


@handles(ReviseCriteria)
def revise_criteria(cmd: ReviseCriteria, ctx: CommandContext) -> CommandResult:
    state = task_app.load_task(ctx, cmd.task_id)
    require_permission(ctx, "task.delegate", channel_id=state.channel_id, domain=state.domain)
    if state.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
        raise CommandError("TASK_TERMINAL", f"{cmd.task_id} is {state.status.value}", status=409)
    stream = ctx.store.stream(ctx.workspace_id, CRITERIA_AGGREGATE, cmd.task_id)
    for ev in stream:  # idempotent retry: return the original revision Event
        if (
            ev.get("idempotency_scope") == cmd.idempotency_scope
            and ev.get("idempotency_key") == ctx.idempotency_key
            and ev.get("actor_account_id") == ctx.principal.account_uuid
        ):
            return CommandResult(
                resource_id=cmd.task_id,
                event_id=ev["event_id"],
                aggregate_seq=ev["aggregate_seq"],
                aggregate_type=CRITERIA_AGGREGATE,
                replayed=True,
                data={"criteria_revision": ev["payload"]["criteria_revision"]},
            )
    current = current_criteria(ctx.session, cmd.task_id)
    revision = current.revision + 1
    try:
        criteria = [c.as_dict() for c in build_revision(cmd.task_id, revision, cmd.criteria)]
    except CriteriaError as exc:
        raise CommandError(exc.code, exc.detail, status=400) from exc
    try:
        res = ctx.store.append(
            AppendRequest(
                workspace_id=ctx.workspace_id,
                aggregate_type=CRITERIA_AGGREGATE,
                aggregate_id=cmd.task_id,
                type="ACCEPTANCE_CRITERIA_REVISED",
                actor_account_id=ctx.principal.account_uuid,
                correlation_id=ctx.correlation_id,
                idempotency_scope=cmd.idempotency_scope,
                idempotency_key=ctx.idempotency_key,
                payload={
                    "task_id": cmd.task_id,
                    "criteria_revision": revision,
                    "criteria_ids": [c["criteria_id"] for c in criteria],
                    "criteria": criteria,
                },
                channel_id=state.channel_id,
                task_id=cmd.task_id,
                caused_by=state.last_event_id,
                expected_seq=len(stream) + 1,
            )
        )
    except EventStoreError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc
    if not res.replayed:
        _insert_rows(ctx.session, cmd.task_id, revision, criteria, res.event_id)
    return CommandResult(
        resource_id=cmd.task_id,
        event_id=res.event_id,
        aggregate_seq=res.aggregate_seq,
        aggregate_type=CRITERIA_AGGREGATE,
        replayed=res.replayed,
        data={"criteria_revision": revision, "criteria_ids": [c["criteria_id"] for c in criteria]},
    )


# ---------------------------------------------------------------- gates on the Task handlers


def delegate_requires_criteria(ctx: CommandContext, state: TaskState, cmd: Any) -> str | None:
    if current_criteria(ctx.session, state.task_id).revision == 0:
        return "ACCEPTANCE_CRITERIA_REQUIRED"
    return None


def submit_requires_evidence(
    ctx: CommandContext, state: TaskState, cmd: task_app.SubmitImplementation
) -> str | None:
    current = current_criteria(ctx.session, state.task_id)
    if current.revision == 0:
        return "ACCEPTANCE_CRITERIA_REQUIRED"
    if cmd.criteria_revision != current.revision:
        raise CommandError(
            "CRITERIA_REVISION_STALE",
            f"submitted revision {cmd.criteria_revision}, current is {current.revision}",
            status=409,
            extra={"current_revision": current.revision},
        )
    by_criterion, _general = parse_evidence_refs(cmd.evidence_refs)
    missing = evidence_satisfies(current.criteria, by_criterion)
    if missing:
        raise CommandError(
            "EVIDENCE_REQUIRED",
            f"required criteria without evidence: {', '.join(missing)}",
            status=422,
            extra={"missing": missing, "criteria_revision": current.revision},
        )
    return None


def register_hooks() -> None:
    if delegate_requires_criteria not in task_app.PRE_DELEGATE_CHECKS:
        task_app.PRE_DELEGATE_CHECKS.append(delegate_requires_criteria)
    if submit_requires_evidence not in task_app.PRE_SUBMIT_CHECKS:
        task_app.PRE_SUBMIT_CHECKS.append(submit_requires_evidence)


register_hooks()
