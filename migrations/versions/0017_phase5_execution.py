"""Phase 5 (P5-04/P5-05/P5-06/P5-07/P5-10): execution policy checks, notifications, budget se.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0017_phase5_execution.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    pass  # tables created by this revision are dropped by the owning package's downgrade list
