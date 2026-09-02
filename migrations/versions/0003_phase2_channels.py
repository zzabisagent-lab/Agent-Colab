"""Phase 2 channels: provider instance display/config, channel templates, thread bindings,
command token hashes, provider nonces.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0003_phase2_channels.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("Phase 2 channel schema is not downgradable; restore from backup instead")
