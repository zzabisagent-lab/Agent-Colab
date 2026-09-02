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
    the DOCUMENT_DRAFTED Event records the triggering actor."""
    try:
        return _result(
            draft_document(
                ctx.session, ctx.store, _storage(ctx), task_id=task_id, actor=_actor(ctx)
            )
        )
    except DocumentLifecycleError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc


def on_verification_terminal(
    ctx: CommandContext, task_id: str, verification_id: str
) -> CommandResult:
    """Automatic attempt/final version after a terminal verdict (system-triggered, no
    ``document.finalize`` permission of the verifier required)."""
    try:
        return _result(
            finalize_attempt(
                ctx.session,
                ctx.store,
                _storage(ctx),
                task_id=task_id,
                verification_id=verification_id,
                actor=_actor(ctx),
            )
        )
    except DocumentLifecycleError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc


def register_hooks() -> None:
    register_completion_check(finalized_document_check)


register_hooks()

__all__: list[str] = [
    "DraftDocument",
    "FinalizeAttempt",
    "on_implementation_submitted",
    "on_verification_terminal",
    "register_hooks",
]


def _unused(_: Any) -> None:  # keep Any imported for type checkers on older tooling
    return None
