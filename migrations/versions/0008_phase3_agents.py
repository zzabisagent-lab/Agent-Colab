"""Phase 3 (P3-01/P3-06/P3-08): Agent registry runtime state, heartbeats, rate windows.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0008_phase3_agents.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for table in ("agent_rate_windows", "agent_heartbeats"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
