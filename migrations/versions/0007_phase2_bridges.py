"""Phase 2 (P2-05/P2-06): Telegram Bridges, message mappings, dead letters.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0007_phase2_bridges.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for table in ("bridge_dead_letters", "message_mappings", "telegram_bridges"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
