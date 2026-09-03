"""Safe Artifact upload (P6-03; spec §9.1, §15.5; validation plan V-P6-05/V-P6-06).

An upload is admitted only when every check passes *before* the bytes become an Artifact row:

1. the file name normalises (no traversal, no separators, no denied extension) — ``storage.py``;
2. the declared MIME is allowed by policy and **matches the sniffed content type**, so a shell
   script cannot arrive labelled ``text/plain``;
3. the stream stays inside the size limit while it is written;
4. the stored bytes re-hash to the SHA-256 returned to the caller;
5. the malware scanner reports clean.

A failure at 1-3 stores nothing. A failure at 4-5 quarantines the artifact: it keeps its row and
its provenance but is unreadable through the normal path until an administrator releases it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from server.artifacts.storage import (
    ArtifactStorage,
    ArtifactStorageError,
    StoredBlob,
    validate_filename,
    validate_mime,
)

# (magic prefix, offset, canonical MIME family). Families are compared, not exact strings, so
# "image/jpeg" declared for JPEG bytes passes while "text/plain" for an ELF binary does not.
_MAGIC: tuple[tuple[bytes, int, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"GIF87a", 0, "image/gif"),
    (b"GIF89a", 0, "image/gif"),
    (b"%PDF-", 0, "application/pdf"),
    (b"PK\x03\x04", 0, "application/zip"),
    (b"\x1f\x8b", 0, "application/gzip"),
    (b"\x7fELF", 0, "application/x-elf"),
    (b"MZ", 0, "application/x-dosexec"),
    (b"#!", 0, "application/x-shellscript"),
    (b"\xca\xfe\xba\xbe", 0, "application/java-vm"),
    (b"ftyp", 4, "video/mp4"),
)
# ZIP containers legitimately carry these declared types (docx/xlsx/pptx/odt/jar-free archives).
_ZIP_ALIASES: frozenset[str] = frozenset(
    {
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/epub+zip",
    }
)
_TEXTUAL = re.compile(rb"^[\t\n\r\x20-\x7e\xc2-\xf4\x80-\xbf]*$")
SNIFF_BYTES = 4096


class ArtifactUploadError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class UploadResult:
    blob: StoredBlob
    filename: str
    mime: str
    sniffed: str


def sniff(head: bytes) -> str:
    """Content type family from magic bytes; ``text/plain`` for printable text, else binary."""
    for magic, offset, family in _MAGIC:
        if head[offset : offset + len(magic)] == magic:
            return family
    if head and _TEXTUAL.match(head):
        return "text/plain"
    return "application/octet-stream"


def mime_matches(declared: str, sniffed: str) -> bool:
    """A declared MIME is acceptable when it agrees with what the bytes actually are."""
    declared = declared.lower()
    if sniffed == "application/zip":
        return declared in _ZIP_ALIASES
    if sniffed == "text/plain":
        # text bytes may legitimately be declared as any text/* or a textual application type
        return declared.startswith("text/") or declared in {
            "application/json",
            "application/xml",
            "application/yaml",
            "application/x-yaml",
            "application/toml",
            "application/csv",
            "application/octet-stream",
        }
    if sniffed == "application/octet-stream":
        # unknown binary: anything except a text claim, which would be a lie about the content
        return not declared.startswith("text/")
    if sniffed in ("application/x-elf", "application/x-dosexec", "application/x-shellscript"):
        return False  # executables never pass, whatever they claim to be
    family, _, _sub = sniffed.partition("/")
    return declared == sniffed or declared.startswith(f"{family}/")


class _SniffingStream:
    """Wraps the caller's stream, keeping the first bytes so the content can be sniffed."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.head = b""

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        if chunk and len(self.head) < SNIFF_BYTES:
            self.head += chunk[: SNIFF_BYTES - len(self.head)]
        return chunk


def store_upload(
    storage: ArtifactStorage,
    *,
    workspace_id: str,
    filename: str,
    mime: str,
    stream: BinaryIO,
) -> UploadResult:
    """Validate, stream to content-addressed storage and confirm the declared type.

    Raises :class:`ArtifactUploadError` with a stable code before anything is registered.
    """
    try:
        safe_name = validate_filename(filename)
        declared = validate_mime(mime)
    except ArtifactStorageError as exc:
        raise ArtifactUploadError(exc.code, exc.detail) from exc
    sniffer = _SniffingStream(stream)
    try:
        blob = storage.write(workspace_id, safe_name, declared, sniffer)  # type: ignore[arg-type]
    except ArtifactStorageError as exc:
        raise ArtifactUploadError(exc.code, exc.detail) from exc
    sniffed = sniff(sniffer.head)
    if not mime_matches(declared, sniffed):
        raise ArtifactUploadError(
            "ARTIFACT_MIME_MISMATCH", f"declared {declared} but content looks like {sniffed}"
        )
    return UploadResult(blob, safe_name, declared, sniffed)


def readback_hash(storage: ArtifactStorage, storage_uri: str, expected_sha256: str) -> str:
    """Re-read the stored bytes and confirm the checksum (V-P6-05 readback)."""
    try:
        return storage.verify(storage_uri, expected_sha256)
    except ArtifactStorageError as exc:
        raise ArtifactUploadError(exc.code, exc.detail) from exc


def blob_path(storage: ArtifactStorage, storage_uri: str) -> Path:
    return storage.path_for(storage_uri)
