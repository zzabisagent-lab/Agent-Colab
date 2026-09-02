"""Filesystem artifact storage (spec §9.1 Artifact, §15.5/§15.11; development plan §6.8).

Content-addressed layout ``<root>/<workspace_id>/<sha256[:2]>/<sha256>``. Every write streams a
SHA-256 while copying; every read re-verifies the checksum. Path, size, and MIME policy are
enforced before any byte is stored. Malware scanning is a ``Scanner`` hook (ClamAV in P6-03).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

DEFAULT_ROOT = "/var/lib/agent-colab/artifacts"
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
URI_SCHEME = "colab-fs"

DENIED_MIME_PREFIXES: tuple[str, ...] = (
    "application/x-msdownload",
    "application/x-executable",
    "application/x-sh",
    "application/x-shellscript",
    "application/x-elf",
    "application/x-dosexec",
    "application/vnd.microsoft.portable-executable",
    "application/x-msi",
    "application/java-archive",
    "text/x-shellscript",
    "text/x-python",
    "application/x-python-code",
    "application/javascript",
    "text/javascript",
    "application/x-bat",
    "application/hta",
)
DENIED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe",
        ".dll",
        ".sh",
        ".bat",
        ".cmd",
        ".ps1",
        ".msi",
        ".scr",
        ".com",
        ".jar",
        ".vbs",
        ".js",
        ".hta",
    }
)
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,254}$")
_MIME_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$", re.I)


class ArtifactStorageError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    reason_code: str | None = None


class Scanner(Protocol):
    def scan(self, path: Path) -> ScanResult: ...


class NoopScanner:
    """Phase 1 placeholder: every artifact is treated as clean (ClamAV arrives in P6-03)."""

    def scan(self, path: Path) -> ScanResult:
        return ScanResult(clean=True)


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    size: int
    storage_uri: str
    path: Path


def validate_filename(filename: str) -> str:
    """Reject traversal, absolute paths, separators, control characters, and denied extensions."""
    if not filename or "\x00" in filename or any(ord(c) < 0x20 for c in filename):
        raise ArtifactStorageError("ARTIFACT_PATH_INVALID", "control characters or empty name")
    if filename.startswith(("/", "\\")) or ".." in filename or "/" in filename or "\\" in filename:
        raise ArtifactStorageError("ARTIFACT_PATH_INVALID", "path components are not allowed")
    if re.match(r"^[A-Za-z]:", filename):
        raise ArtifactStorageError("ARTIFACT_PATH_INVALID", "drive-prefixed names are not allowed")
    if not _FILENAME_RE.match(filename):
        raise ArtifactStorageError("ARTIFACT_PATH_INVALID", "unsupported characters in file name")
    ext = os.path.splitext(filename)[1].lower()
    if ext in DENIED_EXTENSIONS:
        raise ArtifactStorageError("ARTIFACT_MIME_DENIED", f"extension {ext} is not allowed")
    return filename


def validate_mime(mime: str) -> str:
    value = mime.strip().lower()
    if not _MIME_RE.match(value):
        raise ArtifactStorageError("ARTIFACT_MIME_DENIED", "malformed MIME type")
    if any(value.startswith(p) for p in DENIED_MIME_PREFIXES):
        raise ArtifactStorageError("ARTIFACT_MIME_DENIED", f"{value} is denied by policy")
    return value


def storage_uri_for(workspace_id: str, sha256: str) -> str:
    return f"{URI_SCHEME}://{workspace_id}/{sha256}"


def parse_storage_uri(uri: str) -> tuple[str, str]:
    m = re.fullmatch(rf"{URI_SCHEME}://([0-9a-f-]{{36}})/([0-9a-f]{{64}})", uri)
    if not m:
        raise ArtifactStorageError("ARTIFACT_PATH_INVALID", "unsupported storage URI")
    return m.group(1), m.group(2)


class ArtifactStorage:
    def __init__(
        self,
        root: str | Path | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        scanner: Scanner | None = None,
    ) -> None:
        self.root = Path(
            root or os.environ.get("AGENT_COLAB_ARTIFACT_ROOT", DEFAULT_ROOT)
        ).resolve()
        self.max_bytes = max_bytes
        self.scanner: Scanner = scanner or NoopScanner()

    def _blob_path(self, workspace_id: str, sha256: str) -> Path:
        path = (self.root / workspace_id / sha256[:2] / sha256).resolve()
        if self.root not in path.parents:
            raise ArtifactStorageError("ARTIFACT_PATH_INVALID", "resolved outside the storage root")
        return path

    def write(self, workspace_id: str, filename: str, mime: str, stream: BinaryIO) -> StoredBlob:
        """Stream ``stream`` to a temp file computing SHA-256, enforce limits, then move it."""
        validate_filename(filename)
        validate_mime(mime)
        ws_dir = self.root / workspace_id
        ws_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(prefix=".upload-", dir=ws_dir)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ArtifactStorageError(
                            "ARTIFACT_TOO_LARGE", f"exceeds {self.max_bytes} bytes"
                        )
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha = digest.hexdigest()
            final = self._blob_path(workspace_id, sha)
            final.parent.mkdir(parents=True, exist_ok=True)
            if not final.exists():
                os.replace(tmp, final)
                final.chmod(0o440)
            return StoredBlob(sha, size, storage_uri_for(workspace_id, sha), final)
        finally:
            if tmp.exists():
                tmp.unlink()

    def write_bytes(self, workspace_id: str, filename: str, mime: str, data: bytes) -> StoredBlob:
        import io

        return self.write(workspace_id, filename, mime, io.BytesIO(data))

    def path_for(self, storage_uri: str) -> Path:
        workspace_id, sha = parse_storage_uri(storage_uri)
        return self._blob_path(workspace_id, sha)

    def verify(self, storage_uri: str, expected_sha256: str) -> str:
        """Recompute the checksum of the stored bytes; raise on mismatch or missing blob."""
        path = self.path_for(storage_uri)
        if not path.exists():
            raise ArtifactStorageError("ARTIFACT_MISSING", storage_uri)
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise ArtifactStorageError("ARTIFACT_CHECKSUM_MISMATCH", "stored bytes changed")
        return actual

    def read(self, storage_uri: str, expected_sha256: str) -> bytes:
        self.verify(storage_uri, expected_sha256)
        return self.path_for(storage_uri).read_bytes()

    def iter_read(self, storage_uri: str, expected_sha256: str) -> Iterator[bytes]:
        self.verify(storage_uri, expected_sha256)
        with self.path_for(storage_uri).open("rb") as fh:
            yield from iter(lambda: fh.read(1024 * 1024), b"")

    def scan(self, storage_uri: str) -> ScanResult:
        return self.scanner.scan(self.path_for(storage_uri))

    def remove_tree(self) -> None:  # tests only
        shutil.rmtree(self.root, ignore_errors=True)
