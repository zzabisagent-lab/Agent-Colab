"""Phase 6 (P6-03/P6-06): artifact quarantine/provenance, publishers and destinations (owned .

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0021_phase6_publish.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    pass  # tables created by this revision are dropped by the owning package's downgrade list
