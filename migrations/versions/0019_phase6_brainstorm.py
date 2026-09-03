"""Phase 6 (P6-02/P6-09): brainstorm sessions, turns, summaries, decisions, taskify links (ow.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0019_phase6_brainstorm.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    pass  # tables created by this revision are dropped by the owning package's downgrade list
