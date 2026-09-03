"""V-P7-16 (every Phase report retrievable, zero missing, zero secrets) and V-P7-17 (every open
finding has an owner and a deadline, and no High or Critical finding is accepted)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools import evidence_archive

ROOT = Path(__file__).resolve().parents[2]
HIGHEST = int(os.environ.get("COLAB_HIGHEST_PHASE", "6"))


def test_evidence_archive_is_complete_and_secret_free() -> None:
    index = evidence_archive.build(HIGHEST)
    out = ROOT / "release" / "evidence-index.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    for phase in index["phases"]:
        assert phase["passed_report"], f"phase {phase['phase']} has no PASSED Verifier report"
        assert phase["self_evidence_count"] > 0, f"phase {phase['phase']} has no SELF evidence"
    assert index["secret_scan_clean"], "the shipped evidence archive must contain no secret"
    assert index["problems"] == [], index["problems"]


def test_open_findings_carry_an_owner_and_a_deadline() -> None:
    rows, problems = evidence_archive.residual_risks()
    assert rows, "the residual-risk register must list what the release accepts"
    assert problems == [], problems
    for row in rows:
        assert row["severity"].upper() not in ("HIGH", "CRITICAL"), row
        assert row["owner"] and row["deadline"], row


@pytest.mark.skipif(HIGHEST < 7, reason="Phase 7 is not verified yet")
def test_phase7_is_in_the_archive() -> None:
    index = evidence_archive.build(7)
    phase7 = next(p for p in index["phases"] if p["phase"] == 7)
    assert phase7["passed_report"], "Phase 7 must carry its PASSED Verifier report"
