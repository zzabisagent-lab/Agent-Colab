"""Upgrade and rollback rehearsals (P7-06): V-P7-09 the previous release's schema upgraded to the
target with data, settings, secret references and Schedules preserved, and V-P7-10 an application
rollback plus an irreversible migration handled by forward fix, both inside the RTO.

The rehearsals run against their own temporary databases, created and dropped by the tool.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.conftest import TEST_URL
from tools import upgrade_rehearsal as ur

pytestmark = pytest.mark.db

PREVIOUS_RELEASE = "phase-5-passed"  # the last tag whose schema differs from the target


def _tag_exists(ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=ur.ROOT,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


@pytest.fixture(scope="module")
def base_url() -> str:
    assert TEST_URL
    if not _tag_exists(PREVIOUS_RELEASE):
        pytest.skip(f"{PREVIOUS_RELEASE} tag not available in this clone")
    return TEST_URL


def test_upgrade_from_the_previous_release_preserves_everything(base_url: str) -> None:
    """V-P7-09: the old release's migrations, real data, then the target's migrations."""
    report = ur.rehearse_upgrade(base_url, PREVIOUS_RELEASE)
    assert report["from_revision"] != report["to_revision"], "this must be a real schema upgrade"
    assert report["removed_tables"] == []
    for area in ("workspaces", "accounts", "settings", "secret_refs", "events"):
        assert report["data_preserved"][area] is True, area
    assert report["new_tables"], "the upgrade should add the later phases' tables"
    assert report["ok"] and report["within_rto"]
    print(
        f"upgrade {report['from_revision']}→{report['to_revision']} in "
        f"{report['elapsed_s']}s (RTO 4 h), {len(report['new_tables'])} new tables"
    )


def test_application_rollback_is_safe_against_the_upgraded_schema(base_url: str) -> None:
    """V-P7-10 (application failure): every column the previous release reads still exists, so
    rolling the application back needs no schema downgrade."""
    report = ur.rehearse_rollback(base_url, PREVIOUS_RELEASE)
    assert report["columns_lost_for_old_release"] == [], report["columns_lost_for_old_release"]
    assert all(report["data_preserved"].values())
    assert report["ok"] and report["within_rto"]
    print(f"rollback rehearsal in {report['elapsed_s']}s (RTO 4 h)")


def test_an_irreversible_migration_is_repaired_by_forward_fix(base_url: str) -> None:
    """V-P7-10 (irreversible migration): the dropped column cannot come back by downgrading, the
    forward fix restores service, and the ledger records both steps."""
    report = ur.rehearse_forward_fix(base_url)
    assert report["downgrade_possible"] is False  # the column really was gone
    assert report["serviceable_after_fix"] is True and report["tables_intact"] is True
    assert [step["step"] for step in report["ledger"]] == ["irreversible", "forward_fix"]
    assert report["ok"] and report["within_rto"]
    print(f"forward fix in {report['elapsed_s']}s (RTO 4 h)")


def test_the_rehearsal_cli_reports_every_scenario(base_url: str) -> None:
    """The tool an operator runs: all three scenarios, exit 0 only when each one passed."""
    report = ur.rehearse(base_url, PREVIOUS_RELEASE, ur.SCENARIOS)
    assert [r["scenario"] for r in report["results"]] == list(ur.SCENARIOS)
    assert report["ok"] is True
