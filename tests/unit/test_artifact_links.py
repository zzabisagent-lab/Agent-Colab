"""Unit tests: artifact path/MIME/size validation and the subject-handler registry (P1-09)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from server.artifacts.links import (
    SUBJECT_TYPES,
    ArtifactLinkError,
    InactiveSubjectHandler,
    SubjectRegistry,
    default_registry,
)
from server.artifacts.storage import (
    ArtifactStorage,
    ArtifactStorageError,
    parse_storage_uri,
    validate_filename,
    validate_mime,
)


@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "/abs.txt",
        "dir/file.txt",
        "a\\b.txt",
        "bad\x00name",
        "",
        "C:evil.txt",
        ".hidden",
    ],
)
def test_filename_traversal_and_control_chars_rejected(name: str) -> None:
    with pytest.raises(ArtifactStorageError) as exc:
        validate_filename(name)
    assert exc.value.code == "ARTIFACT_PATH_INVALID"


@pytest.mark.parametrize("name", ["report.md", "Data set 1.csv", "photo-2026.png"])
def test_safe_filenames_accepted(name: str) -> None:
    assert validate_filename(name) == name


@pytest.mark.parametrize("name", ["run.exe", "script.sh", "tool.ps1", "lib.dll", "x.jar"])
def test_denied_extensions(name: str) -> None:
    with pytest.raises(ArtifactStorageError) as exc:
        validate_filename(name)
    assert exc.value.code == "ARTIFACT_MIME_DENIED"


@pytest.mark.parametrize(
    "mime",
    ["application/x-msdownload", "text/x-shellscript", "application/javascript", "not a mime"],
)
def test_denied_or_malformed_mime(mime: str) -> None:
    with pytest.raises(ArtifactStorageError) as exc:
        validate_mime(mime)
    assert exc.value.code == "ARTIFACT_MIME_DENIED"


def test_storage_writes_content_addressed_and_enforces_size(tmp_path: Path) -> None:
    storage = ArtifactStorage(tmp_path, max_bytes=16)
    ws = "0f1e2d3c-4b5a-4a6b-8c7d-9e8f7a6b5c4d"
    blob = storage.write(ws, "a.txt", "text/plain", io.BytesIO(b"hello"))
    assert blob.sha256 == hashlib.sha256(b"hello").hexdigest() and blob.size == 5
    assert parse_storage_uri(blob.storage_uri) == (ws, blob.sha256)
    assert blob.path == tmp_path / ws / blob.sha256[:2] / blob.sha256
    assert storage.read(blob.storage_uri, blob.sha256) == b"hello"
    with pytest.raises(ArtifactStorageError) as exc:
        storage.write(ws, "big.txt", "text/plain", io.BytesIO(b"x" * 17))
    assert exc.value.code == "ARTIFACT_TOO_LARGE"
    assert not list((tmp_path / ws).glob(".upload-*")), "temp files are cleaned up"


def test_storage_detects_tampering(tmp_path: Path) -> None:
    storage = ArtifactStorage(tmp_path)
    ws = "0f1e2d3c-4b5a-4a6b-8c7d-9e8f7a6b5c4d"
    blob = storage.write(ws, "a.txt", "text/plain", io.BytesIO(b"hello"))
    blob.path.chmod(0o640)
    blob.path.write_bytes(b"HELLO")
    with pytest.raises(ArtifactStorageError) as exc:
        storage.read(blob.storage_uri, blob.sha256)
    assert exc.value.code == "ARTIFACT_CHECKSUM_MISMATCH"
    with pytest.raises(ArtifactStorageError) as exc2:
        storage.verify(f"colab-fs://{ws}/{'0' * 64}", "0" * 64)
    assert exc2.value.code == "ARTIFACT_MISSING"


def test_registry_activation_states() -> None:
    registry = default_registry()
    assert set(registry.status()) == set(SUBJECT_TYPES)
    assert registry.status()["task"] == {"active": True, "activating_phase": 1}
    assert registry.status()["schedule_run"] == {"active": True, "activating_phase": 5}
    assert registry.status()["brainstorm"] == {"active": True, "activating_phase": 6}
    assert registry.status()["decision"] == {"active": True, "activating_phase": 6}
    registry.require_active("schedule_run")  # activated with Phase 5 (V-P5-36)
    for subject in ("brainstorm", "decision"):  # activated with Phase 6 (V-P6-25)
        registry.require_active(subject)
    with pytest.raises(ArtifactLinkError) as unknown:
        registry.get("workspace")
    assert unknown.value.code == "SUBJECT_TYPE_UNKNOWN"
    with pytest.raises(ArtifactLinkError) as bad:
        SubjectRegistry().register(InactiveSubjectHandler("channel", 2))
    assert bad.value.code == "SUBJECT_TYPE_UNKNOWN"
