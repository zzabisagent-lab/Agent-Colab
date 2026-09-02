"""Tests for the threat model checklist linter (P0-06 / V-P0-08)."""

from __future__ import annotations

import subprocess

from tools import threat_model_lint
from tools.baseline import ROOT
from tools.threat_model_lint import lint

KNOWN = {"V-P0-16", "V-P2-09", "V-P4-02", "V-P4-10", "V-P4-08"}

GOOD = """# T

| Boundary | Included | Controls | Tests |
|---|---|---|---|
| Mattermost | yes | signature | V-P0-16, V-P2-09 |
| Telegram | yes | dedupe | V-P2-09 |
| Setup | yes | loopback | V-P4-02 |
| Secret | yes | lease | V-P4-10 |
| Admin | yes | rbac | V-P4-08 |
"""


def test_real_threat_model_passes() -> None:
    assert threat_model_lint.main() == 0


def test_synthetic_checklist_passes() -> None:
    assert lint(GOOD, KNOWN) == []


def test_missing_boundary_fails() -> None:
    doc = GOOD.replace("| Telegram | yes | dedupe | V-P2-09 |\n", "")
    assert any("Telegram" in p for p in lint(doc, KNOWN))


def test_not_included_or_unknown_test_fails() -> None:
    doc = GOOD.replace("| Setup | yes |", "| Setup | no |").replace("V-P4-08", "V-P9-99")
    problems = lint(doc, KNOWN)
    assert any("Setup: Included" in p for p in problems)
    assert any("unknown Test ID V-P9-99" in p for p in problems)


def test_no_table_fails() -> None:
    assert lint("# nothing here\n", KNOWN) == [
        "no checklist table with header | Boundary | Included | Controls | Tests |"
    ]


def test_env_file_is_git_ignored() -> None:
    proc = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=ROOT, check=False, capture_output=True
    )
    assert proc.returncode == 0, ".env must be git-ignored"
