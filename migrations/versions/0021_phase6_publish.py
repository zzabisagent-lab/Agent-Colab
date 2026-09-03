"""Phase 6 (P6-03/P6-06/P6-07): artifact quarantine and provenance, publishers, publish reviews.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0021_phase6_publish.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for table in (
        "publish_attempts",
        "published_documents",
        "publish_reviews",
        "publish_destinations",
        "artifact_quarantine",
        "artifact_scan_results",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
