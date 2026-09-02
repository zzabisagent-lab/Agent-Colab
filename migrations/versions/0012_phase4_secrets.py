"""Phase 4 (P4-05/P4-06/P4-07): secret provider, grants, leases, handles, tombstone ledger (o.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0012_phase4_secrets.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    pass  # tables created by this revision are dropped by the owning package's downgrade list
