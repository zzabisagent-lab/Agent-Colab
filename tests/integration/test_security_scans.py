"""V-P7-11: the release security scans run and report zero High or Critical findings.

Each scan writes its own report, and the dynamic scan carries a negative control: pointed at an
application without the security middleware it must raise findings, so a clean result on the real
application means the checks ran rather than that they cannot detect anything.

The container scan is asserted here too. It was not, which is how this suite reported four clean
scans while a released image carried 36 High findings: the scan that would have found them was
never part of the gate.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools import security_scan

pytestmark = pytest.mark.db
BLOCKING = ("HIGH", "CRITICAL")
ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "release" / "manifest.json"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0:
        return True
    if shutil.which("sg") is None:
        return False
    return (
        subprocess.run(
            ["sg", "docker", "-c", "docker info"], capture_output=True, check=False
        ).returncode
        == 0
    )


def _blocking(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in report["findings"] if str(f.get("severity", "")).upper() in BLOCKING]


def test_sast_reports_no_high_or_critical(tmp_path: Path) -> None:
    report = security_scan.scan_sast()
    assert report["ran"], report.get("reason")
    assert _blocking(report) == []


def test_dependency_audit_reports_no_high_or_critical() -> None:
    report = security_scan.scan_dependency()
    assert report["ran"], report.get("reason")
    assert report["tools"], "no dependency auditor produced output"
    assert _blocking(report) == []


def test_dynamic_scan_is_clean_on_the_real_application() -> None:
    report = security_scan.scan_dynamic()
    assert report["ran"]
    assert _blocking(report) == [], report["findings"]


def test_dynamic_scan_detects_an_unprotected_application() -> None:
    """Negative control: without the middleware and auth the same checks must fire."""
    leaky = FastAPI()

    @leaky.get("/healthz")
    def healthz() -> dict[str, str]:  # no security headers at all
        return {"status": "ok"}

    @leaky.get("/api/v1/accounts")
    def accounts() -> dict[str, str]:  # unauthenticated admin data
        return {"database": "postgresql://user:password=hunter2@db/x"}

    @leaky.post("/api/v1/auth/sessions")
    def sessions() -> dict[str, str]:  # accepts anything
        return {"session": "granted"}

    for route in security_scan.ADMIN_ROUTES:
        if route != "/api/v1/accounts":
            leaky.get(route)(lambda: {"ok": True})

    report = security_scan.scan_dynamic(client=TestClient(leaky, raise_server_exceptions=False))
    ids = {f["id"] for f in report["findings"]}
    assert "UNAUTHENTICATED_ADMIN_ROUTE" in ids
    assert "AUTH_ACCEPTS_UNKNOWN_TOKEN" in ids
    assert any(i.startswith("HEADER_MISSING") for i in ids)
    assert _blocking(report), "an unprotected application must produce blocking findings"


def test_scan_reports_are_written_for_the_evidence_archive(tmp_path: Path) -> None:
    code = security_scan.main(["--scan", "sast", "--require", "sast", "--out-dir", str(tmp_path)])
    written = json.loads((tmp_path / "sast.json").read_text())
    assert code == 0 and written["scan"] == "sast" and written["ran"]


def _write_manifest(path: Path, images: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"schema_id": "colab.release-manifest.v1", "images": images}))
    return path


def test_container_scan_blocks_when_an_image_is_not_covered(tmp_path: Path) -> None:
    """A gate that reports "unavailable" for an image it never scanned can be passed by hiding it.

    This is the hole that let the container scan report zero while a release image carried High
    findings (V-P7-11): nothing asserted that both images were actually looked at.
    """
    manifest = _write_manifest(
        tmp_path / "manifest.json",
        [{"name": "server", "tag": "agent-colab/server:x", "built": True}],
    )
    report = security_scan.scan_container(manifest)
    assert report["reason_code"] == "IMAGE_NOT_SCANNED"
    assert report["blocking"] >= 1, "an unscanned release image must block the gate"
    assert "web-admin" in report["reason"]


def test_container_scan_blocks_when_there_is_no_manifest(tmp_path: Path) -> None:
    report = security_scan.scan_container(tmp_path / "absent.json")
    assert report["reason_code"] == "RELEASE_MANIFEST_MISSING"
    assert report["blocking"] >= 1


def test_container_scan_covers_every_release_image_by_default() -> None:
    assert set(security_scan.REQUIRED_IMAGES) == {"server", "web-admin"}


@pytest.mark.skipif(not _docker_available(), reason="docker is not available")
@pytest.mark.skipif(not MANIFEST.exists(), reason="no release manifest; run tools.release_build")
def test_container_scan_finds_no_high_or_critical_in_either_image() -> None:
    """The real gate: Trivy against both released images, nothing High or Critical in either."""
    report = security_scan.scan_container(MANIFEST)
    assert report["ran"], report.get("reason")
    assert report.get("reason_code") is None, report.get("reason")
    scanned = {i["name"] for i in report["images"]}
    assert scanned == set(security_scan.REQUIRED_IMAGES), scanned
    assert _blocking(report) == [], report["findings"]
    assert report["blocking"] == 0


@pytest.mark.skipif(not _docker_available(), reason="docker is not available")
@pytest.mark.skipif(not MANIFEST.exists(), reason="no release manifest; run tools.release_build")
def test_container_scan_blocks_when_a_tag_drifted_from_the_manifest(tmp_path: Path) -> None:
    """Scanning a tag that no longer holds the released image is how a clean report goes stale."""
    real = json.loads(MANIFEST.read_text())
    drifted = [
        {**image, "image_id": "sha256:" + "0" * 64} for image in real["images"] if image["built"]
    ]
    report = security_scan.scan_container(_write_manifest(tmp_path / "drift.json", drifted))
    assert report["reason_code"] == "IMAGE_DIGEST_DRIFT"
    assert report["blocking"] >= 1
