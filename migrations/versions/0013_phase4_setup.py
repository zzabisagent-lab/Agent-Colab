"""Phase 4 (P4-03/P4-04/P4-13): setup state, settings versions, maintenance mode (owned by th.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0013_phase4_setup.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    pass  # tables created by this revision are dropped by the owning package's downgrade list
