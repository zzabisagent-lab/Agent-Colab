"""Artifact upload, download and quarantine endpoints (P6-03; V-P6-05/V-P6-06).

Upload streams the body to content-addressed storage, checks the declared MIME against the
sniffed content, re-reads the stored bytes to confirm the SHA-256, and scans for malware. A
failed check either refuses before anything is registered (name, MIME, size) or quarantines the
artifact (checksum, malware), which makes it unreadable through ``/content`` until released.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.dispatch import Runtime, command_error_to_api, to_bus_principal
from server.api.errors import ApiError
from server.application import artifacts as art
from server.application import bus
from server.artifacts import quarantine as qtn
from server.artifacts.scan import default_scanner, report_for
from server.artifacts.service import can_read, get_artifact
from server.artifacts.storage import ArtifactStorage
from server.artifacts.upload import ArtifactUploadError, readback_hash, store_upload
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]

_REFUSE_BEFORE_REGISTER = (
    "ARTIFACT_PATH_INVALID",
    "ARTIFACT_MIME_DENIED",
    "ARTIFACT_TOO_LARGE",
    "ARTIFACT_MIME_MISMATCH",
)


class ReleaseBody(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


def artifact_storage(request: Request) -> ArtifactStorage:
    """One storage instance per app, wired with the configured malware scanner."""
    existing = getattr(request.app.state, "artifact_storage", None)
    if isinstance(existing, ArtifactStorage):
        return existing
    settings = getattr(request.app.state, "settings", None)
    root = getattr(settings, "artifact_root", None)
    storage = ArtifactStorage(root=root, scanner=default_scanner())
    request.app.state.artifact_storage = storage
    return storage


def _context(request: Request, principal: Principal, session: Any, idem: str) -> bus.CommandContext:
    runtime: Runtime = request.app.state.runtime
    return bus.CommandContext(
        session=session,
        store=runtime.store_for(session),
        authorizer=runtime.authorizer,
        clock=runtime.clock,
        principal=to_bus_principal(principal),
        workspace_id=runtime.resolve_workspace(session, principal.account_uuid),
        correlation_id=request.headers.get("X-Correlation-ID") or f"corr-{uuid.uuid4().hex[:16]}",
        idempotency_key=idem,
        extras={"artifact_storage": artifact_storage(request)},
    )


@router.post("/upload", status_code=201)
async def upload(
    request: Request,
    principal: PrincipalDep,
    file: Annotated[UploadFile, File()],
    mime: Annotated[str, Form()],
    subject_type: Annotated[str | None, Form()] = None,
    subject_id: Annotated[str | None, Form()] = None,
    relation: Annotated[str, Form()] = "attachment",
) -> dict[str, Any]:
    idem = request.headers.get("Idempotency-Key")
    if not idem:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header required")
    runtime: Runtime = request.app.state.runtime
    storage = artifact_storage(request)
    with session_scope(runtime.session_factory) as session:
        ctx = _context(request, principal, session, idem)
        # 1-3: name, MIME and size are settled before anything is registered
        try:
            stored = store_upload(
                storage,
                workspace_id=ctx.workspace_id,
                filename=file.filename or "upload.bin",
                mime=mime,
                stream=file.file,
            )
        except ArtifactUploadError as exc:
            status = 413 if exc.code == "ARTIFACT_TOO_LARGE" else 400
            raise ApiError(status, exc.code, exc.detail) from exc
        try:
            result = bus.execute(
                art.RegisterArtifact(
                    filename=stored.filename,
                    mime=stored.mime,
                    storage_uri=stored.blob.storage_uri,
                    sha256=stored.blob.sha256,
                    size=stored.blob.size,
                ),
                ctx,
            )
        except bus.CommandError as exc:
            raise command_error_to_api(exc) from exc
        artifact_id = result.resource_id
        # 4-5: readback checksum, then malware scan; either failure quarantines the artifact
        reason: str | None = None
        detail: str | None = None
        try:
            readback_hash(storage, stored.blob.storage_uri, stored.blob.sha256)
        except ArtifactUploadError as exc:
            reason, detail = exc.code, exc.detail
        if reason is None:
            report = report_for(storage.scanner, storage.path_for(stored.blob.storage_uri))
            qtn.record_scan(session, artifact_id, report, ctx.clock)
            if not report.clean:
                reason = report.reason_code or "ARTIFACT_MALWARE"
                detail = report.detail
        if reason is not None:
            qtn.quarantine(
                session,
                workspace_id=ctx.workspace_id,
                artifact_id=artifact_id,
                reason_code=reason,
                detail=detail,
                actor_account_uuid=principal.account_uuid,
                actor_label=principal.account_id,
                correlation_id=ctx.correlation_id,
                clock=ctx.clock,
            )
            return {
                "artifact_id": artifact_id,
                "status": "quarantined",
                "reason_code": reason,
                "sha256": stored.blob.sha256,
                "size": stored.blob.size,
            }
        linked = None
        if subject_type and subject_id:
            try:
                bus.execute(
                    art.LinkArtifact(artifact_id, subject_type, subject_id, relation),
                    _context(request, principal, session, f"{idem}:link"),
                )
            except bus.CommandError as exc:
                raise command_error_to_api(exc) from exc
            linked = {"subject_type": subject_type, "subject_id": subject_id, "relation": relation}
        return {
            "artifact_id": artifact_id,
            "status": "registered",
            "sha256": stored.blob.sha256,
            "size": stored.blob.size,
            "mime": stored.mime,
            "filename": stored.filename,
            "sniffed_mime": stored.sniffed,
            "event_id": result.event_id,
            "linked": linked,
        }


@router.get("/{artifact_id}")
def metadata(artifact_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        workspace_id = runtime.resolve_workspace(session, principal.account_uuid)
        record = get_artifact(session, workspace_id, artifact_id)
        if record is None or not can_read(session, record, principal.account_uuid):
            raise ApiError(404, "NOT_FOUND", "artifact not found")
        held = qtn.status_of(session, artifact_id)
        return {
            **record.as_dict(),
            "quarantine": None
            if held is None
            else {"reason_code": held.reason_code, "open": held.open},
            "scans": qtn.scans_of(session, artifact_id),
        }


@router.get("/{artifact_id}/content")
def content(artifact_id: str, request: Request, principal: PrincipalDep) -> Response:
    """Readback: the ACL is enforced, the checksum re-verified, quarantine refuses."""
    runtime: Runtime = request.app.state.runtime
    storage = artifact_storage(request)
    with session_scope(runtime.session_factory) as session:
        workspace_id = runtime.resolve_workspace(session, principal.account_uuid)
        record = get_artifact(session, workspace_id, artifact_id)
        if record is None or not can_read(session, record, principal.account_uuid):
            raise ApiError(404, "NOT_FOUND", "artifact not found")
        if record.status == "quarantined":
            raise ApiError(409, "ARTIFACT_QUARANTINED", "artifact is quarantined")
        try:
            data = storage.read(record.storage_uri, record.sha256)
        except Exception as exc:  # storage errors carry a stable code
            code = getattr(exc, "code", "ARTIFACT_MISSING")
            raise ApiError(409, str(code), getattr(exc, "detail", "unreadable")) from exc
        return Response(
            content=data,
            media_type=record.mime,
            headers={"X-Artifact-Sha256": record.sha256},
        )


@router.post("/{artifact_id}/quarantine/release")
def release(
    artifact_id: str, body: ReleaseBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ctx = _context(request, principal, session, request.headers.get("Idempotency-Key") or "rel")
        try:
            bus.require_permission(ctx, "artifact.write", action="api:artifact_archive")
        except bus.CommandError as exc:
            raise command_error_to_api(exc) from exc
        ok = qtn.release(
            session,
            workspace_id=ctx.workspace_id,
            artifact_id=artifact_id,
            released_by=principal.account_uuid,
            actor_label=principal.account_id,
            reason=body.reason,
            correlation_id=ctx.correlation_id,
            clock=ctx.clock,
        )
        if not ok:
            raise ApiError(409, "ARTIFACT_NOT_QUARANTINED", "no open quarantine for that artifact")
        return {"artifact_id": artifact_id, "status": "registered"}
