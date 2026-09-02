"""Phase 3 (P3-10/P3-11/P3-12): work delivery transports (owned by the delivery package).

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0009_phase3_delivery.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webhook_nonces")
