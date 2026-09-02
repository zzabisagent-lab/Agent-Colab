"""Phase 3 (P3-01/P3-02/P3-08 extras): registry, roles preview, limits.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0011_phase3_registry_extras.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    pass  # tables created by this revision are dropped by the owning package's downgrade list
