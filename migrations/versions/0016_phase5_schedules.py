"""Phase 5 (P5-01/P5-02/P5-03): schedules, versions, runs, attempts, occurrence keys, leases .

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0016_phase5_schedules.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for table in (
        "schedule_planner_notes",
        "schedule_run_attempts",
        "schedule_runs",
        "schedule_versions",
        "schedules",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for fn in (
        "agent_colab_forbid_schedule_version_change",
        "agent_colab_check_schedule_version_owner",
        "agent_colab_forbid_run_version_change",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
