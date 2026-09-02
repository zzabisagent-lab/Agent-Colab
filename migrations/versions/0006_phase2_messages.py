"""Phase 2 message ingestion and retention (conversations, messages, policies, tombstones).

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0006_phase2_messages.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("message tombstones are append-only; restore from backup instead")
