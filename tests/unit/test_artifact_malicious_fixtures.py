"""V-P6-06 (unit half): every malicious fixture is refused before anything is stored, and the
scanner reports a signature name rather than the file's bytes (P6-03)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from server.artifacts.scan import EICAR, ScanReport, SignatureScanner, report_for
from server.artifacts.storage import ArtifactStorage
from server.artifacts.upload import ArtifactUploadError, mime_matches, sniff, store_upload

WS = str(uuid.uuid4())  # the storage URI encodes a workspace UUID
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
ELF = b"\x7fELF\x02\x01\x01\x00" + b"dropper" + b"\x00" * 16


def _storage(tmp_path: Path, max_bytes: int = 1024 * 1024) -> ArtifactStorage:
    return ArtifactStorage(root=tmp_path / "artifacts", max_bytes=max_bytes)


def _upload(storage: ArtifactStorage, name: str, mime: str, data: bytes) -> None:
    import io

    store_upload(storage, workspace_id=WS, filename=name, mime=mime, stream=io.BytesIO(data))


@pytest.mark.parametrize(
    ("name", "mime", "data", "code"),
    [
        ("../../etc/passwd", "text/plain", b"root:x:0:0", "ARTIFACT_PATH_INVALID"),
        ("/etc/shadow", "text/plain", b"root:!", "ARTIFACT_PATH_INVALID"),
        ("a/b.txt", "text/plain", b"nested", "ARTIFACT_PATH_INVALID"),
        ("bad\x00name.txt", "text/plain", b"nul", "ARTIFACT_PATH_INVALID"),
        ("payload.exe", "application/octet-stream", b"MZ\x90\x00", "ARTIFACT_MIME_DENIED"),
        ("run.sh", "text/plain", b"#!/bin/sh\n", "ARTIFACT_MIME_DENIED"),
        ("script.txt", "application/x-sh", b"#!/bin/sh\n", "ARTIFACT_MIME_DENIED"),
        ("dropper.bin", "text/plain", ELF, "ARTIFACT_MIME_MISMATCH"),
        ("photo.png", "image/png", b"not a png at all", "ARTIFACT_MIME_MISMATCH"),
    ],
)
def test_malicious_fixtures_are_refused(
    tmp_path: Path, name: str, mime: str, data: bytes, code: str
) -> None:
    with pytest.raises(ArtifactUploadError) as exc:
        _upload(_storage(tmp_path), name, mime, data)
    assert exc.value.code == code


def test_oversize_upload_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ArtifactUploadError) as exc:
        _upload(_storage(tmp_path, max_bytes=64), "big.txt", "text/plain", b"x" * 128)
    assert exc.value.code == "ARTIFACT_TOO_LARGE"


def test_valid_upload_records_the_sniffed_type(tmp_path: Path) -> None:
    import io

    storage = _storage(tmp_path)
    result = store_upload(
        storage,
        workspace_id=WS,
        filename="photo.png",
        mime="image/png",
        stream=io.BytesIO(PNG),
    )
    assert result.sniffed == "image/png" and result.blob.size == len(PNG)
    assert storage.verify(result.blob.storage_uri, result.blob.sha256) == result.blob.sha256


def test_sniffing_and_matching_rules() -> None:
    assert sniff(PNG) == "image/png"
    assert sniff(b"%PDF-1.7\n") == "application/pdf"
    assert sniff(b"plain words") == "text/plain"
    assert sniff(ELF) == "application/x-elf"
    assert mime_matches("text/csv", "text/plain") and mime_matches("application/json", "text/plain")
    assert not mime_matches("text/plain", "application/octet-stream")
    assert not mime_matches("application/octet-stream", "application/x-elf")
    assert mime_matches(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    )


def test_scanner_reports_signature_name_not_content(tmp_path: Path) -> None:
    infected = tmp_path / "sample.bin"
    infected.write_bytes(b"harmless prefix " + EICAR + b" harmless suffix")
    report = SignatureScanner().report(infected)
    assert report.verdict == "infected" and report.reason_code == "ARTIFACT_MALWARE"
    assert report.detail == "EICAR-Test-File"
    assert EICAR.decode() not in (report.detail or "")
    clean = tmp_path / "clean.txt"
    clean.write_bytes(b"nothing to see")
    assert SignatureScanner().report(clean).clean


def test_signature_spanning_two_chunks_is_still_found(tmp_path: Path) -> None:
    """A marker split across read boundaries must not slip through."""
    scanner = SignatureScanner({"Split-Marker": b"ABCDEFGHIJ"})
    path = tmp_path / "split.bin"
    from server.artifacts import scan as scan_module

    half = scan_module.CHUNK - 5
    path.write_bytes(b"\x00" * half + b"ABCDEFGHIJ" + b"\x00" * 16)
    assert scanner.report(path).verdict == "infected"


def test_report_for_wraps_a_plain_scanner(tmp_path: Path) -> None:
    class Plain:
        name = "plain"

        def scan(self, path: Path) -> object:
            from server.artifacts.storage import ScanResult

            return ScanResult(clean=False, reason_code="ARTIFACT_MALWARE")

    path = tmp_path / "x.bin"
    path.write_bytes(b"x")
    assert report_for(Plain(), path) == ScanReport("plain", "infected", "ARTIFACT_MALWARE")
