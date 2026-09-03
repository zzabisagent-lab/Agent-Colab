"""Phase 5 (P5-09): scheduler metrics and alerts (owned by the metrics package).

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0018_phase5_metrics.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    pass  # tables created by this revision are dropped by the owning package's downgrade list
