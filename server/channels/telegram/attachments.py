"""Attachment policy for Bridge traffic (spec §10.2, §15.5; V-P2-11).

Every attachment is evaluated before any download or relay: size limit, MIME allow/deny list,
file-name rules shared with the Artifact storage (no traversal, no control characters, no denied
extensions), and an optional scanner hook (ClamAV quarantine arrives with P6-03). Allowed
attachments are downloaded into content-addressed Artifact storage with their SHA-256.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from server.artifacts.storage import (
    ArtifactStorage,
    ArtifactStorageError,
    Scanner,
    StoredBlob,
    validate_filename,
)
from server.channels.telegram.client import TelegramClient
from server.channels.telegram.intake import InboundAttachment

POLICY_SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "schemas"
    / "api"
    / "telegram"
    / "attachment-policy.v1.schema.json"
)
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_ALLOWED_MIME_PREFIXES: tuple[str, ...] = (
    "image/",
    "application/pdf",
    "text/",
    "application/json",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
)
DEFAULT_DENIED_MIME_PREFIXES: tuple[str, ...] = (
    "application/x-msdownload",
    "application/x-executable",
    "application/x-sh",
    "application/x-shellscript",
    "application/x-dosexec",
    "application/x-msi",
    "application/java-archive",
    "application/x-ms-shortcut",
    "application/vnd.microsoft.portable-executable",
    "application/zip",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
    "application/x-tar",
    "application/gzip",
    "text/x-shellscript",
    "text/x-python",
    "application/javascript",
    "text/javascript",
)

ATTACHMENT_TOO_LARGE = "ATTACHMENT_TOO_LARGE"
ATTACHMENT_MIME_DENIED = "ATTACHMENT_MIME_DENIED"
ATTACHMENT_PATH_INVALID = "ATTACHMENT_PATH_INVALID"
ATTACHMENT_SCAN_PENDING = "ATTACHMENT_SCAN_PENDING"
ATTACHMENT_SCAN_FAILED = "ATTACHMENT_SCAN_FAILED"


@dataclass(frozen=True)
class AttachmentPolicy:
    max_bytes: int = DEFAULT_MAX_BYTES
    allowed_mime_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_MIME_PREFIXES
    denied_mime_prefixes: tuple[str, ...] = DEFAULT_DENIED_MIME_PREFIXES
    require_scan: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttachmentPolicy:
        errors = sorted(_policy_validator().iter_errors(data), key=str)
        if errors:
            raise ValueError(f"attachment policy invalid: {errors[0].message}")
        return cls(
            max_bytes=int(data.get("max_bytes", DEFAULT_MAX_BYTES)),
            allowed_mime_prefixes=tuple(
                data.get("allowed_mime_prefixes", DEFAULT_ALLOWED_MIME_PREFIXES)
            ),
            denied_mime_prefixes=tuple(
                data.get("denied_mime_prefixes", DEFAULT_DENIED_MIME_PREFIXES)
            ),
            require_scan=bool(data.get("require_scan", False)),
        )


@lru_cache(maxsize=1)
def _policy_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(POLICY_SCHEMA.read_text(encoding="utf-8")))


DEFAULT_POLICY = AttachmentPolicy()


@dataclass(frozen=True)
class AttachmentDecision:
    allowed: bool
    reason_code: str | None = None
    detail: str = ""


def evaluate_attachment(
    meta: InboundAttachment, policy: AttachmentPolicy = DEFAULT_POLICY
) -> AttachmentDecision:
    """Deny-by-default policy decision from metadata only (before any download)."""
    if meta.file_size is not None and meta.file_size > policy.max_bytes:
        return AttachmentDecision(
            False, ATTACHMENT_TOO_LARGE, f"{meta.file_size} > {policy.max_bytes}"
        )
    mime = (meta.mime_type or "").strip().lower()
    if not mime or any(mime.startswith(p) for p in policy.denied_mime_prefixes):
        return AttachmentDecision(False, ATTACHMENT_MIME_DENIED, mime or "missing MIME type")
    if not any(mime.startswith(p) for p in policy.allowed_mime_prefixes):
        return AttachmentDecision(False, ATTACHMENT_MIME_DENIED, f"{mime} not in the allow list")
    name = meta.file_name or f"{meta.kind}-{meta.file_id}.bin"
    try:
        validate_filename(name)
    except ArtifactStorageError as exc:
        code = (
            ATTACHMENT_PATH_INVALID
            if exc.code == "ARTIFACT_PATH_INVALID"
            else ATTACHMENT_MIME_DENIED
        )
        return AttachmentDecision(False, code, exc.detail)
    if policy.require_scan:
        return AttachmentDecision(True, ATTACHMENT_SCAN_PENDING, "scan required before relay")
    return AttachmentDecision(True)


@dataclass(frozen=True)
class FetchedAttachment:
    blob: StoredBlob
    filename: str
    mime: str
    decision: AttachmentDecision


def fetch_to_artifact(
    client: TelegramClient,
    storage: ArtifactStorage,
    workspace_id: str,
    meta: InboundAttachment,
    policy: AttachmentPolicy = DEFAULT_POLICY,
    scanner: Scanner | None = None,
) -> FetchedAttachment:
    """Download an allowed attachment into Artifact storage; size is re-checked on the bytes."""
    decision = evaluate_attachment(meta, policy)
    if not decision.allowed:
        raise ArtifactStorageError(decision.reason_code or ATTACHMENT_MIME_DENIED, decision.detail)
    info = client.get_file(meta.file_id)
    if info.file_size is not None and info.file_size > policy.max_bytes:
        raise ArtifactStorageError(ATTACHMENT_TOO_LARGE, f"{info.file_size} > {policy.max_bytes}")
    if not info.file_path:
        raise ArtifactStorageError(ATTACHMENT_PATH_INVALID, "no file path returned")
    data = client.download_file(info.file_path)
    if len(data) > policy.max_bytes:
        raise ArtifactStorageError(ATTACHMENT_TOO_LARGE, f"{len(data)} > {policy.max_bytes}")
    filename = meta.file_name or f"{meta.kind}-{meta.file_id}.bin"
    mime = (meta.mime_type or "application/octet-stream").lower()
    blob = storage.write_bytes(workspace_id, filename, mime, data)
    if scanner is not None:
        result = scanner.scan(storage.path_for(blob.storage_uri))
        if not result.clean:
            raise ArtifactStorageError(ATTACHMENT_SCAN_FAILED, result.reason_code or "scan failed")
    return FetchedAttachment(blob, filename, mime, decision)
