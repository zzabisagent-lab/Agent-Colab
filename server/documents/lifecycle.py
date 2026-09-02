"""Two-stage document lifecycle (spec §14.1, development plan §10.1).

``DRAFT_PRE_VERIFICATION`` is produced after ``IMPLEMENTATION_SUBMITTED`` and never contains a
final verdict. When a VerificationRun becomes terminal, ``FAILED``/``BLOCKED`` produce an
immutable ``ATTEMPT_FINALIZED`` version (Task not completed); only ``PASSED`` produces a
``FINALIZED`` version, which the Task completion gate requires. Versions are write-once in the
canonical store and append-only in ``document_versions`` (DB trigger).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.documents.builder import (
    BuiltDocument,
    DocumentBuildError,
    build_skeleton,
    collect_task_sources,
    document_id_for_task,
    freeze_for_task,
)
from server.documents.store import DocumentStore, DocumentStoreError
from server.events.store import AppendRequest, AppendResult, EventStore, EventStoreError


class DocumentLifecycleError(ValueError):
    def __init__(self, code: str, detail: str, status: int = 409) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class DocumentVersion:
    document_id: str
    version: int
    status: str
    sha256: str
    storage_uri: str
    verification_id: str | None
    verification_result: str | None
    event_id: str
    source_freeze_event_seq: int
    replayed: bool = False


@dataclass(frozen=True)
class DocumentActor:
    account_uuid: str
    correlation_id: str
    idempotency_key: str


# ---------------------------------------------------------------- reads


def document_for_task(session: Session, task_id: str) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT document_id, workspace_id::text AS workspace_id, current_version, status "
                "FROM documents WHERE source_type = 'task' AND source_id = :t"
            ),
            {"t": task_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def list_versions(session: Session, document_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT version, status, verification_id, verification_result, storage_uri, sha256, "
            "source_freeze_event_seq, event_id FROM document_versions WHERE document_id = :d "
            "ORDER BY version"
        ),
        {"d": document_id},
    ).mappings()
    return [dict(r) for r in rows]


def finalized_version_for(
    session: Session, task_id: str, verification_id: str
) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT v.document_id, v.version, v.sha256, v.storage_uri FROM document_versions v "
                "JOIN documents d ON d.document_id = v.document_id "
                "WHERE d.source_type = 'task' AND d.source_id = :t AND v.status = 'FINALIZED' "
                "AND v.verification_id = :v ORDER BY v.version DESC LIMIT 1"
            ),
            {"t": task_id, "v": verification_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _version_for_event(session: Session, event_id: str) -> DocumentVersion | None:
    row = (
        session.execute(
            text(
                "SELECT document_id, version, status, sha256, storage_uri, verification_id, "
                "verification_result, event_id, source_freeze_event_seq FROM document_versions "
                "WHERE event_id = :e"
            ),
            {"e": event_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return DocumentVersion(**{**dict(row), "replayed": True})


# ---------------------------------------------------------------- writes


def _ensure_document(session: Session, workspace_id: str, task_id: str, document_id: str) -> int:
    """Insert the documents row if needed (locked) and return the next version number."""
    row = session.execute(
        text("SELECT current_version FROM documents WHERE document_id = :d FOR UPDATE"),
        {"d": document_id},
    ).first()
    if row is None:
        session.execute(
            text(
                "INSERT INTO documents (id, document_id, workspace_id, doc_type, source_type, "
                "source_id, current_version, status) VALUES (:id, :d, CAST(:ws AS uuid), 'task', "
                "'task', :t, 0, 'DRAFT_PRE_VERIFICATION')"
            ),
            {"id": uuid.uuid4(), "d": document_id, "ws": workspace_id, "t": task_id},
        )
        return 1
    return int(row[0]) + 1


def _persist_version(
    session: Session,
    storage: DocumentStore,
    built: BuiltDocument,
    *,
    workspace_id: str,
    document_id: str,
    version: int,
    status: str,
    verification_id: str | None,
    verification_result: str | None,
    event_id: str,
    freeze_seq: int,
) -> DocumentVersion:
    try:
        stored = storage.write_version(
            workspace_id, document_id, version, built.markdown, built.manifest
        )
    except DocumentStoreError as exc:
        raise DocumentLifecycleError(exc.code, exc.detail) from exc
    session.execute(
        text(
            "INSERT INTO document_versions (id, document_id, version, status, verification_id, "
            "verification_result, storage_uri, sha256, manifest, source_freeze_event_seq, "
            "event_id) "
            "VALUES (:id, :d, :v, :s, :vid, :vr, :uri, :sha, CAST(:m AS jsonb), :seq, :e)"
        ),
        {
            "id": uuid.uuid4(),
            "d": document_id,
            "v": version,
            "s": status,
            "vid": verification_id,
            "vr": verification_result,
            "uri": stored.storage_uri,
            "sha": built.sha256,
            "m": json.dumps(built.manifest, ensure_ascii=False, sort_keys=True),
            "seq": freeze_seq,
            "e": event_id,
        },
    )
    session.execute(
        text(
            "UPDATE documents SET current_version = :v, status = :s, updated_at = now() "
            "WHERE document_id = :d"
        ),
        {"v": version, "s": status, "d": document_id},
    )
    return DocumentVersion(
        document_id,
        version,
        status,
        built.sha256,
        stored.storage_uri,
        verification_id,
        verification_result,
        event_id,
        freeze_seq,
    )


def _append(store: EventStore, req: AppendRequest) -> AppendResult:
    try:
        return store.append(req)
    except EventStoreError as exc:
        raise DocumentLifecycleError(exc.code, exc.detail) from exc


def _replay(
    session: Session, store: EventStore, workspace_id: str, document_id: str, scope: str, key: str
) -> DocumentVersion | None:
    for ev in store.stream(workspace_id, "document", document_id):
        if ev.get("idempotency_scope") == scope and ev.get("idempotency_key") == key:
            return _version_for_event(session, ev["event_id"])
    return None


def draft_document(
    session: Session,
    store: EventStore,
    storage: DocumentStore,
    *,
    task_id: str,
    actor: DocumentActor,
    policy_version: str = "policy-v1",
) -> DocumentVersion:
    """Create a DRAFT_PRE_VERIFICATION version from a fresh source freeze (idempotent per key)."""
    freeze = freeze_for_task(session, task_id)
    try:
        sources = collect_task_sources(session, task_id, freeze)
    except DocumentBuildError as exc:
        raise DocumentLifecycleError(
            exc.code, exc.detail, 404 if exc.code == "TASK_NOT_FOUND" else 409
        ) from exc
    workspace_id = sources.workspace_id
    document_id = document_id_for_task(task_id)
    replay = _replay(
        session, store, workspace_id, document_id, "document:draft", actor.idempotency_key
    )
    if replay is not None:
        return replay
    version = _ensure_document(session, workspace_id, task_id, document_id)
    built = build_skeleton(
        sources, "DRAFT_PRE_VERIFICATION", document_id=document_id, version=version
    )
    res = _append(
        store,
        AppendRequest(
            workspace_id=workspace_id,
            aggregate_type="document",
            aggregate_id=document_id,
            type="DOCUMENT_DRAFTED",
            actor_account_id=actor.account_uuid,
            correlation_id=actor.correlation_id,
            idempotency_scope="document:draft",
            idempotency_key=actor.idempotency_key,
            payload={
                "document_id": document_id,
                "source_type": "task",
                "source_id": task_id,
                "version": version,
                "sha256": built.sha256,
                "source_freeze_event_seq": freeze.up_to_recorded_seq,
            },
            policy_version=policy_version,
            task_id=task_id,
            channel_id=sources.state.channel_id if sources.state else None,
            expected_seq=version,
        ),
    )
    return _persist_version(
        session,
        storage,
        built,
        workspace_id=workspace_id,
        document_id=document_id,
        version=version,
        status="DRAFT_PRE_VERIFICATION",
        verification_id=None,
        verification_result=None,
        event_id=res.event_id,
        freeze_seq=freeze.up_to_recorded_seq,
    )


def finalize_attempt(
    session: Session,
    store: EventStore,
    storage: DocumentStore,
    *,
    task_id: str,
    verification_id: str,
    actor: DocumentActor,
    policy_version: str = "policy-v1",
) -> DocumentVersion:
    """After a terminal verdict: FAILED/BLOCKED → ATTEMPT_FINALIZED; PASSED → FINALIZED.

    Idempotent per (verification_id, revision): a second call for the same terminal revision
    returns the existing version instead of writing another one.
    """
    run = session.execute(
        text(
            "SELECT target_type, target_id, current_revision, result, status "
            "FROM verification_runs "
            "WHERE verification_id = :v"
        ),
        {"v": verification_id},
    ).first()
    if run is None or run[0] != "task" or run[1] != task_id:
        raise DocumentLifecycleError(
            "VERIFICATION_NOT_FOUND", f"{verification_id} is not a verification of {task_id}", 404
        )
    revision, result = int(run[2]), run[3]
    if revision < 1 or result not in ("PASSED", "FAILED", "BLOCKED"):
        raise DocumentLifecycleError(
            "VERIFICATION_NOT_TERMINAL", f"{verification_id}: status {run[4]}, result {result}"
        )
    freeze = freeze_for_task(session, task_id)
    sources = collect_task_sources(session, task_id, freeze)
    workspace_id = sources.workspace_id
    document_id = document_id_for_task(task_id)
    existing = session.execute(
        text(
            "SELECT event_id FROM document_versions v WHERE v.document_id = :d "
            "AND v.verification_id = :v "
            "AND v.status IN ('ATTEMPT_FINALIZED', 'FINALIZED') "
            "AND (v.manifest -> 'verification' ->> 'revision')::int = :r"
        ),
        {"d": document_id, "v": verification_id, "r": revision},
    ).first()
    if existing is not None:
        found = _version_for_event(session, str(existing[0]))
        if found is not None:
            return found
    stage = "FINALIZED" if result == "PASSED" else "ATTEMPT_FINALIZED"
    scope = "document:finalize" if stage == "FINALIZED" else "document:attempt_finalize"
    replay = _replay(session, store, workspace_id, document_id, scope, actor.idempotency_key)
    if replay is not None:
        return replay
    version = _ensure_document(session, workspace_id, task_id, document_id)
    try:
        built = build_skeleton(
            sources,
            stage,
            document_id=document_id,
            version=version,
            verification_id=verification_id,
        )
    except DocumentBuildError as exc:
        raise DocumentLifecycleError(exc.code, exc.detail) from exc
    payload: dict[str, Any] = {
        "document_id": document_id,
        "version": version,
        "verification_id": verification_id,
        "sha256": built.sha256,
        "source_freeze_event_seq": freeze.up_to_recorded_seq,
    }
    if stage == "ATTEMPT_FINALIZED":
        payload["result"] = result
    res = _append(
        store,
        AppendRequest(
            workspace_id=workspace_id,
            aggregate_type="document",
            aggregate_id=document_id,
            type="DOCUMENT_FINALIZED" if stage == "FINALIZED" else "DOCUMENT_ATTEMPT_FINALIZED",
            actor_account_id=actor.account_uuid,
            correlation_id=actor.correlation_id,
            idempotency_scope=scope,
            idempotency_key=actor.idempotency_key,
            payload=payload,
            policy_version=policy_version,
            task_id=task_id,
            channel_id=sources.state.channel_id if sources.state else None,
            expected_seq=version,
        ),
    )
    return _persist_version(
        session,
        storage,
        built,
        workspace_id=workspace_id,
        document_id=document_id,
        version=version,
        status=stage,
        verification_id=verification_id,
        verification_result=result,
        event_id=res.event_id,
        freeze_seq=freeze.up_to_recorded_seq,
    )


# ---------------------------------------------------------------- completion gate


def expected_document_id(session: Session, task_id: str) -> str | None:
    """The FINALIZED version id (``document_id``) that CompleteTask.document_id must reference."""
    gate_row = session.execute(
        text(
            "SELECT verification_id FROM verification_runs WHERE target_type = 'task' "
            "AND target_id = :t AND result = 'PASSED' AND status <> 'CANCELLED' "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        {"t": task_id},
    ).first()
    if gate_row is None:
        return None
    version = finalized_version_for(session, task_id, str(gate_row[0]))
    return None if version is None else str(version["document_id"])


def finalized_document_check(state: Any, session: Session | None) -> str | None:
    """Completion prerequisite (spec §21.1 Task closure, V-P6-19): FINALIZED document for the
    latest PASSED verification, else ``COMPLETION_PREREQUISITE_MISSING``."""
    if session is None:
        return "COMPLETION_PREREQUISITE_MISSING"
    return (
        None if expected_document_id(session, state.task_id) else "COMPLETION_PREREQUISITE_MISSING"
    )
