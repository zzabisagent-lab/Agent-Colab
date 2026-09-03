"""V-P7-16 (every Phase report retrievable, zero missing, zero secrets), V-P7-17 (every open
finding has an owner, a deadline and an acceptor, and no High or Critical finding is accepted) and
V-P7-18 (the final report is delivered and no deployment precedes the user's explicit decision)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools import deployment_decision, evidence_archive

ROOT = Path(__file__).resolve().parents[2]
HIGHEST = int(os.environ.get("COLAB_HIGHEST_PHASE", "7"))


def test_evidence_archive_is_complete_and_secret_free() -> None:
    index = evidence_archive.build(HIGHEST)
    out = ROOT / "release" / "evidence-index.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    for phase in index["phases"]:
        assert phase["reports"], f"phase {phase['phase']} has no retrievable Verifier report"
        assert phase["self_evidence_count"] > 0, f"phase {phase['phase']} has no SELF evidence"
        if phase["phase"] < evidence_archive.CURRENT_PHASE:
            assert phase["passed_report"], f"phase {phase['phase']} has no PASSED Verifier report"
    assert index["secret_scan_clean"], "the shipped evidence archive must contain no secret"
    assert index["problems"] == [], index["problems"]


def test_the_phase_under_verification_is_archived_without_a_verdict() -> None:
    """Its reports must be retrievable and secret-free; only the verdict is still open."""
    index = evidence_archive.build(evidence_archive.CURRENT_PHASE)
    current = next(p for p in index["phases"] if p["phase"] == evidence_archive.CURRENT_PHASE)
    assert current["reports"], "the phase under verification must archive its Verifier reports"
    assert index["problems"] == [], index["problems"]


def test_open_findings_carry_an_owner_a_deadline_and_an_acceptor() -> None:
    rows, problems = evidence_archive.residual_risks()
    assert rows, "the residual-risk register must list what the release accepts"
    assert problems == [], problems
    for row in rows:
        assert row["severity"].upper() not in ("HIGH", "CRITICAL"), row
        assert row["owner"], row
        assert row["deadline"], row
        assert row["acceptor"], f"every open risk needs a recorded acceptor: {row}"


def test_the_final_report_exists_with_the_sections_the_plan_requires() -> None:
    summary, problems = deployment_decision.check()
    assert summary["report_present"], "REPORT.md is delivered before the deployment question"
    assert problems == [], problems


def test_no_deployment_precedes_an_explicit_decision() -> None:
    summary, problems = deployment_decision.check()
    assert problems == [], problems
    if summary["state"] not in deployment_decision.DEPLOYABLE:
        assert not summary["deployment_performed"], summary["ledger_entries"]


@pytest.mark.skipif(HIGHEST < 8, reason="Phase 7 is the phase under verification")
def test_phase7_is_in_the_archive_with_its_verdict() -> None:
    index = evidence_archive.build(7)
    phase7 = next(p for p in index["phases"] if p["phase"] == 7)
    assert phase7["passed_report"], "Phase 7 must carry its PASSED Verifier report"
