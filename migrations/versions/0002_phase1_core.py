"""Phase 1 core schema: append-only Event/Audit/Verification authority, projections, roles.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0002_phase1_core.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("Phase 1 core schema is not downgradable; restore from backup instead")
