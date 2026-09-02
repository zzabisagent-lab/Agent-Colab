"""Phase 4 (P4-08/P4-09/P4-10/P4-14): MFA enrollments, re-auth proofs, break-glass sessions, .

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0014_phase4_security.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    pass  # tables created by this revision are dropped by the owning package's downgrade list
