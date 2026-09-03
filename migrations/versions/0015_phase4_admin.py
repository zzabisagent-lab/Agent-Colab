"""Phase 4 (P4-01/P4-02/P4-11): account admin, dependency probes, hard-delete workflow (owned.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0015_phase4_admin.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for table in ("backups", "dependency_probes", "hard_delete_tombstones", "hard_delete_requests"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
