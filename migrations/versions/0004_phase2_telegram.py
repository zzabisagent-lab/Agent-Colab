"""Phase 2 (P2-04): Telegram update receipts for webhook replay protection.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0004_phase2_telegram.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS telegram_update_receipts")
