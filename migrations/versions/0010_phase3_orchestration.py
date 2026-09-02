"""Phase 3 (P3-06/P3-09/P3-13/P3-14): routing, task graph, verifier assignment.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0010_phase3_orchestration.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for table in ("task_join_state", "verifier_assignments", "routing_decisions"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
