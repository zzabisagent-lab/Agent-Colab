"""Document commands on the command bus (development plan §7.2 Document row, §10.1).

``DraftDocument`` runs after ``IMPLEMENTATION_SUBMITTED`` (call ``on_implementation_submitted``
from the Task package or the API layer); ``FinalizeAttempt`` runs after a terminal verdict.
Importing this module registers the FINALIZED-document completion prerequisite on the Task
domain (``register_completion_check``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.documents.lifecycle import (
    DocumentActor,
    DocumentLifecycleError,
    DocumentVersion,
    draft_document,
    finalize_attempt,
    finalized_document_check,
)
from server.documents.store import DocumentStore
from server.domain.task import register_completion_check


@dataclass(frozen=True)
class DraftDocument(Command):
    """Produce the pre-verification draft of a Task (new version each time)."""

    task_id: str
    idempotency_scope: str = "document:draft"


@dataclass(frozen=True)
class FinalizeAttempt(Command):
    """Produce the ATTEMPT_FINALIZED (FAILED/BLOCKED) or FINALIZED (PASSED) version."""

    task_id: str
    verification_id: str
    idempotency_scope: str = "document:finalize"


def _storage(ctx: CommandContext) -> DocumentStore:
    storage = ctx.extras.get("document_store")
    return storage if isinstance(storage, DocumentStore) else DocumentStore()


def _actor(ctx: CommandContext) -> DocumentActor:
    return DocumentActor(ctx.principal.account_uuid, ctx.correlation_id, ctx.idempotency_key)


def _result(v: DocumentVersion) -> CommandResult:
    return CommandResult(
        resource_id=v.document_id,
        event_id=v.event_id,
        aggregate_seq=v.version,
        aggregate_type="document",
        replayed=v.replayed,
        data={
            "version": v.version,
            "status": v.status,
            "sha256": v.sha256,
            "storage_uri": v.storage_uri,
            "verification_id": v.verification_id,
            "verification_result": v.verification_result,
            "source_freeze_event_seq": v.source_freeze_event_seq,
        },
    )


def _channel_of(ctx: CommandContext, task_id: str) -> str | None:
    from server.application.tasks import load_task

    return load_task(ctx, task_id).channel_id


@handles(DraftDocument)
def draft(cmd: DraftDocument, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "document.draft", channel_id=_channel_of(ctx, cmd.task_id))
    try:
        return _result(
            draft_document(
                ctx.session, ctx.store, _storage(ctx), task_id=cmd.task_id, actor=_actor(ctx)
            )
        )
    except DocumentLifecycleError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc


@handles(FinalizeAttempt)
def finalize(cmd: FinalizeAttempt, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "document.finalize", channel_id=_channel_of(ctx, cmd.task_id))
    try:
        version = finalize_attempt(
            ctx.session,
            ctx.store,
            _storage(ctx),
            task_id=cmd.task_id,
            verification_id=cmd.verification_id,
            actor=_actor(ctx),
        )
    except DocumentLifecycleError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    return _result(version)


def on_implementation_submitted(ctx: CommandContext, task_id: str) -> CommandResult:
    """Automatic draft after IMPLEMENTATION_SUBMITTED (spec §14.1): the Documentation Service
    acts on behalf of the system, so no ``document.draft`` permission of the submitter is needed;
    the DOCUMENT_DRAFTED Event records the triggering actor.

    Phase 6 runs the whole pipeline here: the freeze ledger, provenance links with checksums,
    redaction counts and the optional narrative layer (development plan §10.1).
    """
    from server.documents import finalizer

    try:
        pipeline = finalizer.draft_task(
            ctx.session,
            ctx.store,
            _storage(ctx),
            task_id=task_id,
            actor=_actor(ctx),
            now=ctx.clock.now(),
            clock=ctx.clock,
        )
    except DocumentLifecycleError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    assert pipeline.document_version is not None
    return _result(pipeline.document_version)


def on_verification_terminal(
    ctx: CommandContext, task_id: str, verification_id: str
) -> CommandResult:
    """Automatic attempt/final version after a terminal verdict (system-triggered, no
    ``document.finalize`` permission of the verifier required)."""
    from server.documents import finalizer

    try:
        pipeline = finalizer.finalize_task(
            ctx.session,
            ctx.store,
            _storage(ctx),
            task_id=task_id,
            verification_id=verification_id,
            actor=_actor(ctx),
            now=ctx.clock.now(),
        )
    except DocumentLifecycleError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    assert pipeline.document_version is not None
    return _result(pipeline.document_version)


def register_hooks() -> None:
    register_completion_check(finalized_document_check)


register_hooks()

__all__: list[str] = [
    "DraftDocument",
    "FinalizeAttempt",
    "auto_draft_subject",
    "ensure_task_document",
    "on_brainstorm_closed",
    "on_implementation_submitted",
    "on_schedule_period_closed",
    "on_schedule_run_terminal",
    "on_verification_terminal",
    "register_hooks",
    "register_phase6_hooks",
]


def _unused(_: Any) -> None:  # keep Any imported for type checkers on older tooling
    return None


# ---------------------------------------------------------------- Phase 6 automatic drafting


def _now(ctx: CommandContext) -> Any:
    return ctx.clock.now()


def _pipeline_actor(ctx: CommandContext, suffix: str) -> DocumentActor:
    """A distinct idempotency key per subject keeps one automatic draft per trigger."""
    return DocumentActor(
        ctx.principal.account_uuid, ctx.correlation_id, f"{ctx.idempotency_key}:{suffix}"
    )


def auto_draft_subject(
    ctx: CommandContext, subject_type: str, subject_id: str, **kwargs: Any
) -> Any:
    """Draft a Brainstorm / Schedule Run / Schedule period document (V-P6-08, V-P6-09).

    Never raises: a subject that cannot be documented records a stable reason code for the
    generation-rate report (V-P6-20) and returns ``None``.
    """
    from server.documents import finalizer, sources

    now = _now(ctx)
    workspace_id: str | None = ctx.workspace_id
    try:
        if subject_type == "schedule_run":
            src = sources.collect_schedule_run(ctx.session, subject_id, now)
        elif subject_type == "brainstorm":
            src = sources.collect_brainstorm(ctx.session, subject_id, now)
        elif subject_type == "schedule_period":
            src = sources.collect_schedule_period(ctx.session, subject_id, now=now, **kwargs)
        else:
            raise sources.SourceError("DOCUMENT_SUBJECT_UNSUPPORTED", subject_type)
        return finalizer.draft_subject(
            ctx.session,
            ctx.store,
            _storage(ctx),
            src,
            actor=_pipeline_actor(ctx, f"{subject_type}:{subject_id}"),
            now=now,
            clock=ctx.clock,
        )
    except sources.SourceError as exc:
        sources.record_failure(
            ctx.session,
            workspace_id=workspace_id,
            subject_type=subject_type,
            subject_id=str(subject_id),
            reason_code=exc.code,
            detail=exc.detail,
            now=now,
        )
        return None
    except DocumentLifecycleError as exc:
        sources.record_failure(
            ctx.session,
            workspace_id=workspace_id,
            subject_type=subject_type,
            subject_id=str(subject_id),
            reason_code=exc.code,
            detail=exc.detail,
            now=now,
        )
        return None


def on_schedule_run_terminal(ctx: CommandContext, run_id: str) -> Any:
    """Automatic draft when a Schedule Run reaches a terminal status (V-P6-09)."""
    return auto_draft_subject(ctx, "schedule_run", run_id)


def on_brainstorm_closed(ctx: CommandContext, brainstorm_id: str) -> Any:
    """Automatic draft when a Brainstorm session closes (V-P6-08, spec §7F ``close``)."""
    return auto_draft_subject(ctx, "brainstorm", brainstorm_id)


def on_schedule_period_closed(
    ctx: CommandContext, schedule_id: str, *, period: str, start: Any, end: Any
) -> Any:
    """Automatic per-period summary of a Schedule's Runs (P6-08)."""
    return auto_draft_subject(
        ctx, "schedule_period", schedule_id, period=period, start=start, end=end
    )


def ensure_task_document(ctx: CommandContext, task_id: str) -> Any:
    """A Task reaching a terminal state always has a document (V-P6-07, V-P6-20).

    Completion already requires a FINALIZED version; this covers the other terminal paths and
    records a reason code when a draft is impossible.
    """
    from sqlalchemy import text as _text

    from server.documents import sources

    existing = ctx.session.execute(
        _text("SELECT 1 FROM documents WHERE source_type = 'task' AND source_id = :t"),
        {"t": task_id},
    ).first()
    if existing is not None:
        return None
    try:
        return _result(
            draft_document(
                ctx.session,
                ctx.store,
                _storage(ctx),
                task_id=task_id,
                actor=_pipeline_actor(ctx, f"task:{task_id}"),
            )
        )
    except DocumentLifecycleError as exc:
        sources.record_failure(
            ctx.session,
            workspace_id=ctx.workspace_id,
            subject_type="task",
            subject_id=task_id,
            reason_code=exc.code,
            detail=exc.detail,
            now=_now(ctx),
        )
        return None


def _task_terminal_hook(ctx: CommandContext, task_id: str) -> None:
    """Terminal Task → its own document, and the Schedule Run that created it (if any)."""
    from sqlalchemy import text as _text

    ensure_task_document(ctx, task_id)
    row = ctx.session.execute(
        _text(
            "SELECT run_id FROM schedule_runs WHERE task_id = :t "
            "AND status IN ('SUCCEEDED','FAILED','TIMED_OUT','CANCELLED','SKIPPED') "
            "ORDER BY run_id LIMIT 1"
        ),
        {"t": task_id},
    ).first()
    if row is not None:
        on_schedule_run_terminal(ctx, str(row[0]))


def register_phase6_hooks() -> None:
    from server.application.tasks import register_terminal_hook

    register_terminal_hook(_task_terminal_hook)


register_phase6_hooks()
