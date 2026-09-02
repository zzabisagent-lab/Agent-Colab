"""Artifact commands on the common command bus (P1-09; development plan §6.8, §7.2 Artifact row).

Every write appends an ``ARTIFACT_*`` Event through the Event store and updates the authority
tables in the same transaction. ``artifact.write``/``artifact.read`` are enforced through the
policy authorizer; unauthorized reads are normalized to ``NOT_FOUND`` (development plan §7.5).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    handles,
    require_permission,
)
from server.artifacts.links import REGISTRY, ArtifactLinkError, SubjectRegistry, link_artifact
from server.artifacts.service import ArtifactRecord, can_read, get_artifact, links_of
from server.artifacts.storage import (
    ArtifactStorage,
    ArtifactStorageError,
    validate_filename,
    validate_mime,
)
from server.events.store import AppendRequest, EventStoreError

_SHA_RE = r"^[0-9a-f]{64}$"


def _storage(ctx: CommandContext) -> ArtifactStorage:
    storage = ctx.extras.get("artifact_storage")
    if not isinstance(storage, ArtifactStorage):
        raise CommandError("ARTIFACT_STORAGE_UNAVAILABLE", "no artifact storage configured", 503)
    return storage


def _registry(ctx: CommandContext) -> SubjectRegistry:
    registry = ctx.extras.get("subject_registry")
    return registry if isinstance(registry, SubjectRegistry) else REGISTRY


def _append(
    ctx: CommandContext, artifact_id: str, event_type: str, operation: str, payload: dict[str, Any]
) -> Any:
    try:
        return ctx.store.append(
            AppendRequest(
                workspace_id=ctx.workspace_id,
                aggregate_type="artifact",
                aggregate_id=artifact_id,
                type=event_type,
                actor_account_id=ctx.principal.account_uuid,
                correlation_id=ctx.correlation_id,
                idempotency_scope=f"artifact:{operation}",
                idempotency_key=ctx.idempotency_key,
                payload=payload,
                expected_seq=ctx.expected_seq,
            )
        )
    except EventStoreError as exc:
        raise CommandError(exc.code, exc.detail) from exc


def _wrap(exc: ArtifactStorageError | ArtifactLinkError) -> CommandError:
    status = {
        "ARTIFACT_TOO_LARGE": 413,
        "ARTIFACT_MIME_DENIED": 415,
        "ARTIFACT_PATH_INVALID": 400,
        "ARTIFACT_LINK_RELATION_INVALID": 400,
        "SUBJECT_TYPE_UNKNOWN": 400,
        "SUBJECT_TYPE_NOT_ACTIVE": 409,
        "ARTIFACT_NOT_FOUND": 404,
        "SUBJECT_NOT_FOUND": 404,
        "WORKSPACE_MISMATCH": 404,
    }.get(exc.code, 409)
    return CommandError(exc.code, exc.detail, status)


def _load(ctx: CommandContext, artifact_id: str) -> ArtifactRecord:
    record = get_artifact(ctx.session, ctx.workspace_id, artifact_id)
    if record is None:
        raise CommandError("NOT_FOUND", "artifact not found", 404)
    return record


@dataclass(frozen=True)
class RegisterArtifact(Command):
    """Register bytes (``content``) or an externally stored blob (``storage_uri`` + ``sha256``)."""

    filename: str
    mime: str
    content: bytes | None = None
    storage_uri: str | None = None
    sha256: str | None = None
    size: int | None = None
    readers: tuple[str, ...] = field(default_factory=tuple)
    idempotency_scope: str = "artifact:register"


@handles(RegisterArtifact)
def register_artifact(cmd: RegisterArtifact, ctx: CommandContext) -> Any:
    from server.application.bus import CommandResult

    require_permission(ctx, "artifact.write", action="artifact_register")
    try:
        validate_filename(cmd.filename)
        mime = validate_mime(cmd.mime)
        if cmd.content is not None:
            blob = _storage(ctx).write_bytes(ctx.workspace_id, cmd.filename, mime, cmd.content)
            storage_uri, sha256, size = blob.storage_uri, blob.sha256, blob.size
        else:
            import re

            if (
                not cmd.storage_uri
                or not cmd.sha256
                or not re.match(_SHA_RE, cmd.sha256)
                or cmd.size is None
            ):
                raise CommandError(
                    "ARTIFACT_METADATA_INVALID", "storage_uri, sha256 and size required", 400
                )
            if cmd.size < 0:
                raise CommandError("ARTIFACT_METADATA_INVALID", "size must be >= 0", 400)
            storage_uri, sha256, size = cmd.storage_uri, cmd.sha256, cmd.size
    except ArtifactStorageError as exc:
        raise _wrap(exc) from exc
    # deterministic ID from the idempotency scope so retries replay the same artifact
    seed = (
        f"{ctx.workspace_id}|{ctx.principal.account_uuid}|artifact:register|{ctx.idempotency_key}"
    )
    artifact_id = "art-" + hashlib.sha256(seed.encode()).hexdigest()[:20]
    result = _append(
        ctx,
        artifact_id,
        "ARTIFACT_REGISTERED",
        "register",
        {
            "artifact_id": artifact_id,
            "sha256": sha256,
            "size": size,
            "mime": mime,
            "filename": cmd.filename,
        },
    )
    if result.replayed:
        prior = ctx.session.execute(
            text("SELECT artifact_id FROM artifacts WHERE source_event_id = :e"),
            {"e": result.event_id},
        ).scalar()
        return CommandResult(
            str(prior), result.event_id, result.aggregate_seq, "artifact", replayed=True
        )
    ctx.session.execute(
        text(
            "INSERT INTO artifacts (id, artifact_id, workspace_id, creator_account_id, "
            "storage_uri, mime, size, sha256, acl, status, source_event_id) VALUES "
            "(:id, :a, :ws, :c, :u, :m, :s, :h, CAST(:acl AS jsonb), 'registered', :e)"
        ),
        {
            "id": uuid.uuid4(),
            "a": artifact_id,
            "ws": uuid.UUID(ctx.workspace_id),
            "c": uuid.UUID(ctx.principal.account_uuid),
            "u": storage_uri,
            "m": mime,
            "s": size,
            "h": sha256,
            "acl": json.dumps({"readers": list(cmd.readers)}),
            "e": result.event_id,
        },
    )
    return CommandResult(
        artifact_id,
        result.event_id,
        result.aggregate_seq,
        "artifact",
        data={"sha256": sha256, "size": size, "storage_uri": storage_uri},
    )


@dataclass(frozen=True)
class VerifyArtifact(Command):
    artifact_id: str
    idempotency_scope: str = "artifact:verify"


@handles(VerifyArtifact)
def verify_artifact(cmd: VerifyArtifact, ctx: CommandContext) -> Any:
    from server.application.bus import CommandResult

    require_permission(ctx, "artifact.write", action="artifact_verify")
    record = _load(ctx, cmd.artifact_id)
    if record.status == "archived":
        raise CommandError("ARTIFACT_ARCHIVED", "archived artifacts are immutable", 409)
    storage = _storage(ctx)
    try:
        storage.verify(record.storage_uri, record.sha256)
        scan = storage.scan(record.storage_uri)
    except ArtifactStorageError as exc:
        if exc.code in ("ARTIFACT_CHECKSUM_MISMATCH", "ARTIFACT_MISSING"):
            result = _append(
                ctx,
                record.artifact_id,
                "ARTIFACT_QUARANTINED",
                "verify",
                {"artifact_id": record.artifact_id, "reason_code": exc.code},
            )
            ctx.session.execute(
                text("UPDATE artifacts SET status = 'quarantined' WHERE artifact_id = :a"),
                {"a": record.artifact_id},
            )
            return CommandResult(
                record.artifact_id,
                result.event_id,
                result.aggregate_seq,
                "artifact",
                data={"status": "quarantined", "reason_code": exc.code},
            )
        raise _wrap(exc) from exc
    if not scan.clean:
        result = _append(
            ctx,
            record.artifact_id,
            "ARTIFACT_QUARANTINED",
            "verify",
            {"artifact_id": record.artifact_id, "reason_code": scan.reason_code or "MALWARE"},
        )
        ctx.session.execute(
            text("UPDATE artifacts SET status = 'quarantined' WHERE artifact_id = :a"),
            {"a": record.artifact_id},
        )
        return CommandResult(
            record.artifact_id,
            result.event_id,
            result.aggregate_seq,
            "artifact",
            data={"status": "quarantined", "reason_code": scan.reason_code},
        )
    result = _append(
        ctx,
        record.artifact_id,
        "ARTIFACT_VERIFIED",
        "verify",
        {"artifact_id": record.artifact_id, "sha256": record.sha256},
    )
    ctx.session.execute(
        text("UPDATE artifacts SET status = 'verified' WHERE artifact_id = :a"),
        {"a": record.artifact_id},
    )
    return CommandResult(
        record.artifact_id,
        result.event_id,
        result.aggregate_seq,
        "artifact",
        data={"status": "verified", "sha256": record.sha256},
    )


@dataclass(frozen=True)
class LinkArtifact(Command):
    artifact_id: str
    subject_type: str
    subject_id: str
    relation: str = "attachment"
    idempotency_scope: str = "artifact:link"


@handles(LinkArtifact)
def link_artifact_cmd(cmd: LinkArtifact, ctx: CommandContext) -> Any:
    from server.application.bus import CommandResult

    require_permission(ctx, "artifact.write", action="artifact_link")
    record = _load(ctx, cmd.artifact_id)
    if record.status == "quarantined":
        raise CommandError("ARTIFACT_QUARANTINED", "quarantined artifacts cannot be linked", 409)
    try:
        link_artifact(
            ctx.session,
            workspace_id=ctx.workspace_id,
            artifact_id=cmd.artifact_id,
            subject_type=cmd.subject_type,
            subject_id=cmd.subject_id,
            relation=cmd.relation,
            linked_by=ctx.principal.account_uuid,
            registry=_registry(ctx),
        )
    except ArtifactLinkError as exc:
        raise _wrap(exc) from exc
    return CommandResult(
        cmd.artifact_id,
        record.source_event_id,
        0,
        "artifact",
        data={"links": links_of(ctx.session, cmd.artifact_id)},
    )


@dataclass(frozen=True)
class ArchiveArtifact(Command):
    artifact_id: str
    idempotency_scope: str = "artifact:archive"


@handles(ArchiveArtifact)
def archive_artifact(cmd: ArchiveArtifact, ctx: CommandContext) -> Any:
    from server.application.bus import CommandResult

    require_permission(ctx, "artifact.write", action="artifact_archive")
    record = _load(ctx, cmd.artifact_id)
    if record.status != "archived":
        ctx.session.execute(
            text("UPDATE artifacts SET status = 'archived' WHERE artifact_id = :a"),
            {"a": record.artifact_id},
        )
    return CommandResult(
        record.artifact_id, record.source_event_id, 0, "artifact", data={"status": "archived"}
    )


def read_artifact(
    ctx: CommandContext,
    artifact_id: str,
    *,
    with_content: bool = False,
    workspace_admin: bool = False,
) -> dict[str, Any]:
    """Query: metadata (and optionally bytes) for a principal allowed by the ACL; else NOT_FOUND."""
    require_permission(ctx, "artifact.read", action="artifact_read")
    record = get_artifact(ctx.session, ctx.workspace_id, artifact_id)
    if record is None or not can_read(
        ctx.session,
        record,
        ctx.principal.account_uuid,
        workspace_admin=workspace_admin,
        registry=_registry(ctx),
    ):
        raise CommandError("NOT_FOUND", "artifact not found", 404)
    out = record.as_dict()
    out["links"] = links_of(ctx.session, artifact_id)
    if with_content:
        try:
            out["content"] = _storage(ctx).read(record.storage_uri, record.sha256)
        except ArtifactStorageError as exc:
            raise CommandError(exc.code, exc.detail, 409) from exc
    return out
