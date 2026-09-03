"""Phase 6 (P6-04/P6-05/P6-07/P6-08/P6-10): document freezes, versions, redaction reports, pu.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0020_phase6_documents.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    pass  # tables created by this revision are dropped by the owning package's downgrade list
