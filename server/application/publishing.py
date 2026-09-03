"""Publish destinations, publish review and publishing commands (P6-06/P6-07).

Publishing a document version is gated three ways (development plan §10.1, §10.3):

* the caller holds ``document.publish``;
* the version is ``FINALIZED`` (a pre-verification draft is never published);
* an approved :class:`ReviewDocumentPublish` decision exists for exactly that version.

Publishing is exactly once per ``(document, version, destination)``: the row is unique, every
attempt is recorded in ``publish_attempts``, and a retry after a destination outage either
completes the first publication or reports the existing one without a second side effect.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.documents.publishers.base import (
    PublishError,
    PublishTarget,
    publisher_for,
    publisher_kinds,
)
from server.documents.store import DocumentStore, DocumentStoreError
from server.events.store import AppendRequest, AppendResult, EventStoreError
from server.observability.audit import append_audit

DESTINATION_KINDS: tuple[str, ...] = ("filesystem", "git", "bookstack", "wikijs")


# ---------------------------------------------------------------- helpers


def _ws(ctx: CommandContext) -> uuid.UUID:
    return uuid.UUID(ctx.workspace_id)


def _append(
    ctx: CommandContext, cmd: Command, document_id: str, event_type: str, payload: dict[str, Any]
) -> AppendResult:
    try:
        return ctx.store.append(
            AppendRequest(
                workspace_id=ctx.workspace_id,
                aggregate_type="document",
                aggregate_id=document_id,
                type=event_type,
                actor_account_id=ctx.principal.account_uuid,
                correlation_id=ctx.correlation_id,
                idempotency_scope=cmd.idempotency_scope,
                idempotency_key=ctx.idempotency_key,
                payload=payload,
            )
        )
    except EventStoreError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc


def _version_row(ctx: CommandContext, document_id: str, version: int) -> dict[str, Any]:
    row = (
        ctx.session.execute(
            text(
                "SELECT v.version, v.status, v.sha256, v.storage_uri, d.workspace_id::text AS ws "
                "FROM document_versions v JOIN documents d ON d.document_id = v.document_id "
                "WHERE v.document_id = :d AND v.version = :v"
            ),
            {"d": document_id, "v": version},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CommandError("DOCUMENT_VERSION_NOT_FOUND", f"{document_id} v{version}", status=404)
    if str(row["ws"]) != ctx.workspace_id:
        raise CommandError("DOCUMENT_VERSION_NOT_FOUND", "another workspace", status=404)
    return dict(row)


def _destination(ctx: CommandContext, destination_id: str) -> dict[str, Any]:
    row = (
        ctx.session.execute(
            text(
                "SELECT destination_id, kind, config, credential_ref, status "
                "FROM publish_destinations WHERE destination_id = :d AND workspace_id = :w"
            ),
            {"d": destination_id, "w": _ws(ctx)},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CommandError("PUBLISH_DESTINATION_NOT_FOUND", destination_id, status=404)
    if row["status"] != "active":
        raise CommandError("PUBLISH_DESTINATION_DISABLED", destination_id, status=409)
    return dict(row)


def _resolve_credential(ctx: CommandContext, credential_ref: str | None) -> str | None:
    """Destinations store a Secret Broker reference; the value is resolved at publish time."""
    if not credential_ref:
        return None
    resolver = ctx.extras.get("publish_credential_resolver")
    if callable(resolver):
        value = resolver(credential_ref)
        return None if value is None else str(value)
    return None


def _approved_review(ctx: CommandContext, document_id: str, version: int) -> dict[str, Any] | None:
    row = (
        ctx.session.execute(
            text(
                "SELECT review_id, reviewer_account_id, decision, decided_at "
                "FROM publish_reviews WHERE document_id = :d AND version = :v "
                "ORDER BY decided_at DESC, id DESC LIMIT 1"
            ),
            {"d": document_id, "v": version},
        )
        .mappings()
        .first()
    )
    if row is None or row["decision"] != "APPROVED":
        return None
    return dict(row)


def _next_attempt_no(ctx: CommandContext, document_id: str, version: int, dest: str) -> int:
    value = ctx.session.execute(
        text(
            "SELECT COALESCE(max(attempt_no), 0) + 1 FROM publish_attempts "
            "WHERE document_id = :d AND version = :v AND destination_id = :dest"
        ),
        {"d": document_id, "v": version, "dest": dest},
    ).scalar_one()
    return int(value)


def _record_attempt(
    ctx: CommandContext,
    *,
    document_id: str,
    version: int,
    destination_id: str,
    attempt_no: int,
    ok: bool,
    error_code: str | None,
    detail: str | None,
) -> None:
    ctx.session.execute(
        text(
            "INSERT INTO publish_attempts (workspace_id, document_id, version, destination_id, "
            "attempt_no, ok, error_code, detail, attempted_at) VALUES (:w, :d, :v, :dest, :n, "
            ":ok, :code, :detail, :at)"
        ),
        {
            "w": _ws(ctx),
            "d": document_id,
            "v": version,
            "dest": destination_id,
            "n": attempt_no,
            "ok": ok,
            "code": error_code,
            "detail": (detail or "")[:300] or None,
            "at": ctx.clock.now(),
        },
    )


def _target(
    ctx: CommandContext, document_id: str, version: int, row: dict[str, Any]
) -> PublishTarget:
    store = ctx.extras.get("document_store") or DocumentStore()
    try:
        markdown, manifest = store.read_version(ctx.workspace_id, document_id, version)
    except DocumentStoreError as exc:
        raise CommandError(exc.code, exc.detail, status=409) from exc
    return PublishTarget(
        workspace_id=ctx.workspace_id,
        document_id=document_id,
        version=version,
        markdown=markdown,
        manifest=manifest,
        checksum=str(row["sha256"]),
        title=str(manifest.get("title") or f"{document_id} v{version}"),
    )


# ---------------------------------------------------------------- destinations


@dataclass(frozen=True)
class RegisterPublishDestination(Command):
    destination_id: str
    kind: str
    display_name: str
    config: dict[str, Any]
    credential_ref: str | None = None
    idempotency_scope: str = "document:publish"


@handles(RegisterPublishDestination)
def register_destination(cmd: RegisterPublishDestination, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "admin.settings", action="api:settings_apply")
    if cmd.kind not in DESTINATION_KINDS:
        raise CommandError("PUBLISH_DESTINATION_KIND_INVALID", cmd.kind, status=400)
    if cmd.kind not in publisher_kinds():
        raise CommandError("PUBLISH_DESTINATION_KIND_UNSUPPORTED", cmd.kind, status=400)
    for key, value in cmd.config.items():
        if isinstance(value, str) and key.lower() in ("token", "password", "secret", "api_key"):
            raise CommandError(
                "PUBLISH_DESTINATION_SECRET_VALUE",
                f"{key} must be a Secret Broker reference, not a value",
                status=400,
            )
    exists = ctx.session.execute(
        text("SELECT 1 FROM publish_destinations WHERE destination_id = :d"),
        {"d": cmd.destination_id},
    ).first()
    if exists:
        raise CommandError("PUBLISH_DESTINATION_EXISTS", cmd.destination_id, status=409)
    ctx.session.execute(
        text(
            "INSERT INTO publish_destinations (id, destination_id, workspace_id, kind, "
            "display_name, config, credential_ref, created_by) VALUES (:id, :d, :w, :k, :n, "
            "CAST(:c AS jsonb), :cr, :by)"
        ),
        {
            "id": uuid.uuid4(),
            "d": cmd.destination_id,
            "w": _ws(ctx),
            "k": cmd.kind,
            "n": cmd.display_name,
            "c": json.dumps(cmd.config, sort_keys=True),
            "cr": cmd.credential_ref,
            "by": uuid.UUID(ctx.principal.account_uuid),
        },
    )
    append_audit(
        ctx.session,
        action="publish.destination_registered",
        target_type="publish_destination",
        target_id=cmd.destination_id,
        result="OK",
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=_ws(ctx),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata={"kind": cmd.kind, "credential_ref": cmd.credential_ref},
        clock=ctx.clock,
    )
    return CommandResult(cmd.destination_id, "", 0, "publish_destination", data={"kind": cmd.kind})


def list_destinations(ctx: CommandContext) -> list[dict[str, Any]]:
    require_permission(ctx, "document.read", action="api:document_read")
    rows = ctx.session.execute(
        text(
            "SELECT destination_id, kind, display_name, credential_ref, status, created_at "
            "FROM publish_destinations WHERE workspace_id = :w ORDER BY destination_id"
        ),
        {"w": _ws(ctx)},
    ).mappings()
    return [
        {**dict(r), "created_at": r["created_at"].isoformat()}  # config stays server-side
        for r in rows
    ]


# ---------------------------------------------------------------- review (P6-07)


@dataclass(frozen=True)
class ReviewDocumentPublish(Command):
    document_id: str
    version: int
    decision: str  # APPROVED | REJECTED
    reason: str
    idempotency_scope: str = "document:review"


@handles(ReviewDocumentPublish)
def review_document(cmd: ReviewDocumentPublish, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "document.review", action="api:document_review")
    if cmd.decision not in ("APPROVED", "REJECTED"):
        raise CommandError("PUBLISH_REVIEW_DECISION_INVALID", cmd.decision, status=400)
    if not cmd.reason.strip():
        raise CommandError("PUBLISH_REVIEW_REASON_REQUIRED", "a reason is required", status=400)
    row = _version_row(ctx, cmd.document_id, cmd.version)
    if row["status"] != "FINALIZED":
        raise CommandError(
            "DOCUMENT_NOT_FINALIZED",
            f"v{cmd.version} is {row['status']}; only FINALIZED versions are reviewed",
            status=409,
        )
    result = _append(
        ctx,
        cmd,
        cmd.document_id,
        "DOCUMENT_REVIEWED",
        {
            "document_id": cmd.document_id,
            "version": cmd.version,
            "reviewer_account_id": ctx.principal.account_id,
            "result": cmd.decision,
        },
    )
    if result.replayed:
        return CommandResult(cmd.document_id, result.event_id, result.aggregate_seq, "document")
    review_id = "rev-" + uuid.uuid4().hex[:20]
    ctx.session.execute(
        text(
            "INSERT INTO publish_reviews (id, review_id, workspace_id, document_id, version, "
            "reviewer_account_id, decision, reason, event_id, decided_at) VALUES (:id, :r, :w, "
            ":d, :v, :who, :dec, :reason, :e, :at)"
        ),
        {
            "id": uuid.uuid4(),
            "r": review_id,
            "w": _ws(ctx),
            "d": cmd.document_id,
            "v": cmd.version,
            "who": uuid.UUID(ctx.principal.account_uuid),
            "dec": cmd.decision,
            "reason": cmd.reason[:500],
            "e": result.event_id,
            "at": ctx.clock.now(),
        },
    )
    return CommandResult(
        review_id,
        result.event_id,
        result.aggregate_seq,
        "document",
        data={"decision": cmd.decision, "document_id": cmd.document_id, "version": cmd.version},
    )


def reviews_of(ctx: CommandContext, document_id: str) -> list[dict[str, Any]]:
    require_permission(ctx, "document.read", action="api:document_read")
    rows = ctx.session.execute(
        text(
            "SELECT review_id, version, reviewer_account_id::text AS reviewer, decision, reason, "
            "decided_at FROM publish_reviews WHERE document_id = :d AND workspace_id = :w "
            "ORDER BY decided_at"
        ),
        {"d": document_id, "w": _ws(ctx)},
    ).mappings()
    return [{**dict(r), "decided_at": r["decided_at"].isoformat()} for r in rows]


# ---------------------------------------------------------------- publish (P6-06)


@dataclass(frozen=True)
class PublishDocument(Command):
    document_id: str
    version: int
    destination_id: str
    correction_of_version: int | None = None
    correction_reason: str | None = None
    idempotency_scope: str = "document:publish"


@handles(PublishDocument)
def publish_document(cmd: PublishDocument, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "document.publish", action="api:document_publish")
    row = _version_row(ctx, cmd.document_id, cmd.version)
    if row["status"] != "FINALIZED":
        raise CommandError(
            "DOCUMENT_NOT_FINALIZED",
            f"v{cmd.version} is {row['status']}; only FINALIZED versions are published",
            status=409,
        )
    destination = _destination(ctx, cmd.destination_id)
    existing = (
        ctx.session.execute(
            text(
                "SELECT external_ref, external_version, checksum, state FROM published_documents "
                "WHERE document_id = :d AND version = :v AND destination_id = :dest"
            ),
            {"d": cmd.document_id, "v": cmd.version, "dest": cmd.destination_id},
        )
        .mappings()
        .first()
    )
    if existing is not None:  # exactly once: a retry reports the first publication (V-P6-16)
        return CommandResult(
            cmd.document_id,
            "",
            0,
            "document",
            data={**dict(existing), "already_published": True},
            replayed=True,
        )
    if cmd.correction_of_version is not None and not (cmd.correction_reason or "").strip():
        raise CommandError(
            "PUBLISH_CORRECTION_REASON_REQUIRED", "a correction states its reason", status=400
        )
    review = _approved_review(ctx, cmd.document_id, cmd.version)
    if review is None:
        raise CommandError(
            "PUBLISH_REVIEW_REQUIRED",
            f"{cmd.document_id} v{cmd.version} has no approved publish review",
            status=409,
        )
    target = _target(ctx, cmd.document_id, cmd.version, row)
    config = dict(destination["config"] or {})
    token = _resolve_credential(ctx, destination["credential_ref"])
    if token is not None:
        config["token"] = token
    overrides = ctx.extras.get("publisher_config_overrides") or {}
    config.update(dict(overrides.get(cmd.destination_id, {})))
    attempt_no = _next_attempt_no(ctx, cmd.document_id, cmd.version, cmd.destination_id)
    try:
        publisher = publisher_for(str(destination["kind"]), config)
        record = (
            publisher.update(target)
            if cmd.correction_of_version is not None
            else publisher.publish(target)
        )
    except PublishError as exc:
        _record_attempt(
            ctx,
            document_id=cmd.document_id,
            version=cmd.version,
            destination_id=cmd.destination_id,
            attempt_no=attempt_no,
            ok=False,
            error_code=exc.code,
            detail=exc.detail,
        )
        append_audit(
            ctx.session,
            action="document.publish_failed",
            target_type="document",
            target_id=cmd.document_id,
            result="FAIL",
            actor_label=ctx.principal.account_id,
            correlation_id=ctx.correlation_id,
            workspace_id=_ws(ctx),
            actor_account_id=uuid.UUID(ctx.principal.account_uuid),
            error_code=exc.code,
            metadata={"destination_id": cmd.destination_id, "version": cmd.version},
            clock=ctx.clock,
        )
        raise CommandError(exc.code, exc.detail, status=502 if exc.retryable else 409) from exc
    result = _append(
        ctx,
        cmd,
        cmd.document_id,
        "DOCUMENT_PUBLISHED",
        {
            "document_id": cmd.document_id,
            "version": cmd.version,
            "publisher": str(destination["kind"]),
            "external_ref": record.external_ref,
        },
    )
    ctx.session.execute(
        text(
            "INSERT INTO published_documents (id, workspace_id, document_id, version, "
            "destination_id, external_ref, external_version, checksum, state, "
            "correction_of_version, correction_reason, published_by, published_at, event_id) "
            "VALUES (:id, :w, :d, :v, :dest, :ref, :xv, :sum, 'published', :cov, :cr, :by, :at, :e)"
        ),
        {
            "id": uuid.uuid4(),
            "w": _ws(ctx),
            "d": cmd.document_id,
            "v": cmd.version,
            "dest": cmd.destination_id,
            "ref": record.external_ref,
            "xv": record.external_version,
            "sum": target.checksum,
            "cov": cmd.correction_of_version,
            "cr": (cmd.correction_reason or None),
            "by": uuid.UUID(ctx.principal.account_uuid),
            "at": ctx.clock.now(),
            "e": result.event_id,
        },
    )
    _record_attempt(
        ctx,
        document_id=cmd.document_id,
        version=cmd.version,
        destination_id=cmd.destination_id,
        attempt_no=attempt_no,
        ok=True,
        error_code=None,
        detail=None,
    )
    return CommandResult(
        cmd.document_id,
        result.event_id,
        result.aggregate_seq,
        "document",
        data={
            "external_ref": record.external_ref,
            "external_version": record.external_version,
            "checksum": target.checksum,
            "destination_id": cmd.destination_id,
            "version": cmd.version,
            "review_id": review["review_id"],
            "correction_of_version": cmd.correction_of_version,
        },
    )


@dataclass(frozen=True)
class VerifyPublishedDocument(Command):
    document_id: str
    version: int
    destination_id: str
    idempotency_scope: str = "document:publish"


@handles(VerifyPublishedDocument)
def verify_published(cmd: VerifyPublishedDocument, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "document.publish", action="api:document_publish")
    published = (
        ctx.session.execute(
            text(
                "SELECT external_ref, checksum, state FROM published_documents "
                "WHERE document_id = :d AND version = :v AND destination_id = :dest "
                "AND workspace_id = :w"
            ),
            {
                "d": cmd.document_id,
                "v": cmd.version,
                "dest": cmd.destination_id,
                "w": _ws(ctx),
            },
        )
        .mappings()
        .first()
    )
    if published is None:
        raise CommandError("PUBLISH_NOT_FOUND", "not published to that destination", status=404)
    destination = _destination(ctx, cmd.destination_id)
    config = dict(destination["config"] or {})
    token = _resolve_credential(ctx, destination["credential_ref"])
    if token is not None:
        config["token"] = token
    overrides = ctx.extras.get("publisher_config_overrides") or {}
    config.update(dict(overrides.get(cmd.destination_id, {})))
    try:
        publisher = publisher_for(str(destination["kind"]), config)
        outcome = publisher.verify(str(published["external_ref"]), str(published["checksum"]))
    except PublishError as exc:
        raise CommandError(exc.code, exc.detail, status=502 if exc.retryable else 409) from exc
    return CommandResult(
        cmd.document_id,
        "",
        0,
        "document",
        data={
            "ok": outcome.ok,
            "checksum": outcome.checksum,
            "expected": published["checksum"],
            "detail": outcome.detail,
        },
    )


@dataclass(frozen=True)
class ArchivePublishedDocument(Command):
    document_id: str
    version: int
    destination_id: str
    idempotency_scope: str = "document:publish"


@handles(ArchivePublishedDocument)
def archive_published(cmd: ArchivePublishedDocument, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "document.publish", action="api:document_publish")
    published = (
        ctx.session.execute(
            text(
                "SELECT external_ref, state FROM published_documents WHERE document_id = :d "
                "AND version = :v AND destination_id = :dest AND workspace_id = :w FOR UPDATE"
            ),
            {
                "d": cmd.document_id,
                "v": cmd.version,
                "dest": cmd.destination_id,
                "w": _ws(ctx),
            },
        )
        .mappings()
        .first()
    )
    if published is None:
        raise CommandError("PUBLISH_NOT_FOUND", "not published to that destination", status=404)
    if published["state"] == "archived":
        return CommandResult(
            cmd.document_id, "", 0, "document", data={"state": "archived"}, replayed=True
        )
    destination = _destination(ctx, cmd.destination_id)
    config = dict(destination["config"] or {})
    token = _resolve_credential(ctx, destination["credential_ref"])
    if token is not None:
        config["token"] = token
    overrides = ctx.extras.get("publisher_config_overrides") or {}
    config.update(dict(overrides.get(cmd.destination_id, {})))
    try:
        publisher = publisher_for(str(destination["kind"]), config)
        publisher.archive(str(published["external_ref"]))
    except PublishError as exc:
        raise CommandError(exc.code, exc.detail, status=502 if exc.retryable else 409) from exc
    ctx.session.execute(
        text(
            "UPDATE published_documents SET state = 'archived', archived_at = :at "
            "WHERE document_id = :d AND version = :v AND destination_id = :dest"
        ),
        {"at": ctx.clock.now(), "d": cmd.document_id, "v": cmd.version, "dest": cmd.destination_id},
    )
    append_audit(
        ctx.session,
        action="document.publish_archived",
        target_type="document",
        target_id=cmd.document_id,
        result="OK",
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=_ws(ctx),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata={"destination_id": cmd.destination_id, "version": cmd.version},
        clock=ctx.clock,
    )
    return CommandResult(cmd.document_id, "", 0, "document", data={"state": "archived"})


def published_versions(ctx: CommandContext, document_id: str) -> list[dict[str, Any]]:
    require_permission(ctx, "document.read", action="api:document_read")
    rows = ctx.session.execute(
        text(
            "SELECT document_id, version, destination_id, external_ref, external_version, "
            "checksum, state, correction_of_version, correction_reason, published_at, archived_at "
            "FROM published_documents WHERE document_id = :d AND workspace_id = :w "
            "ORDER BY version, destination_id"
        ),
        {"d": document_id, "w": _ws(ctx)},
    ).mappings()
    return [
        {
            **dict(r),
            "published_at": r["published_at"].isoformat(),
            "archived_at": None if r["archived_at"] is None else r["archived_at"].isoformat(),
        }
        for r in rows
    ]


def publish_attempts(ctx: CommandContext, document_id: str, version: int) -> list[dict[str, Any]]:
    require_permission(ctx, "document.read", action="api:document_read")
    rows = ctx.session.execute(
        text(
            "SELECT destination_id, attempt_no, ok, error_code, detail, attempted_at "
            "FROM publish_attempts WHERE document_id = :d AND version = :v AND workspace_id = :w "
            "ORDER BY attempt_no"
        ),
        {"d": document_id, "v": version, "w": _ws(ctx)},
    ).mappings()
    return [{**dict(r), "attempted_at": r["attempted_at"].isoformat()} for r in rows]
