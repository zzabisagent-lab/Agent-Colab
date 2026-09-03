"""V-P7-11: the release security scans run and report zero High or Critical findings.

Each scan writes its own report, and the dynamic scan carries a negative control: pointed at an
application without the security middleware it must raise findings, so a clean result on the real
application means the checks ran rather than that they cannot detect anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools import security_scan

pytestmark = pytest.mark.db
BLOCKING = ("HIGH", "CRITICAL")


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
