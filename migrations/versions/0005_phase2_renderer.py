"""Phase 2 renderer delivery records (channel_posts).

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0005_phase2_renderer.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS channel_posts")
