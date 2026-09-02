"""Phase 0 verification harness: workspaces, accounts, credentials, agents, verification_runs.

Revision ID: 0001
Revises: None

Implements development plan §6.4 (verification independence constraints) at the DB level so that
a VerificationRun with the same implementer and verifier Account, Agent, or credential fingerprint
is rejected regardless of the application path (V-P0-07). Later phases extend these tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('ACTIVE','SUSPENDED')", name="ck_workspaces_status"),
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Text(), nullable=False, unique=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("account_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("auth_subject", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("account_type IN ('human','agent','service')", name="ck_accounts_type"),
        sa.CheckConstraint("status IN ('ACTIVE','SUSPENDED','DELETED')", name="ck_accounts_status"),
    )
    op.create_table(
        "account_aliases",
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("alias_of_account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("account_id <> alias_of_account_id", name="ck_account_aliases_distinct"),
    )
    op.create_table(
        "service_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False, unique=True),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','revoked')", name="ck_service_credentials_status"),
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("agent_id", sa.Text(), nullable=False, unique=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column(
            "account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False, unique=True
        ),
        sa.Column("adapter_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "adapter_type IN ('mcp','webhook','mattermost_bot')", name="ck_agents_adapter"
        ),
        sa.CheckConstraint(
            "status IN ('pending','active','suspended','revoked','offline')",
            name="ck_agents_status",
        ),
    )
    op.create_table(
        "verification_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("verification_id", sa.Text(), nullable=False, unique=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("phase", sa.SmallInteger(), nullable=True),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column(
            "implementer_account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("verifier_account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("implementer_agent_id", sa.Text(), nullable=True),
        sa.Column("verifier_agent_id", sa.Text(), nullable=True),
        sa.Column("implementer_credential_fingerprint", sa.Text(), nullable=False),
        sa.Column("verifier_credential_fingerprint", sa.Text(), nullable=False),
        sa.Column("identity_graph_version", sa.Text(), nullable=False),
        sa.Column("effective_policy_hash", sa.Text(), nullable=False),
        sa.Column("criteria_version", sa.Text(), nullable=False),
        sa.Column("target_commit", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PLANNED"),
        sa.Column("snapshot_hash", sa.Text(), nullable=False),
        sa.Column("created_by_account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "implementer_account_id <> verifier_account_id", name="ck_vr_distinct_accounts"
        ),
        sa.CheckConstraint(
            "implementer_agent_id IS NULL OR verifier_agent_id IS NULL "
            "OR verifier_agent_id <> implementer_agent_id",
            name="ck_vr_distinct_agents",
        ),
        sa.CheckConstraint(
            "implementer_credential_fingerprint <> verifier_credential_fingerprint",
            name="ck_vr_distinct_credentials",
        ),
        sa.CheckConstraint(
            "status IN ('PLANNED','ASSIGNED','RUNNING','PASSED','FAILED','BLOCKED','CANCELLED',"
            "'FIX_SUBMITTED','RECHECK_ASSIGNED')",
            name="ck_vr_status",
        ),
        sa.CheckConstraint("target_type IN ('phase','task')", name="ck_vr_target_type"),
    )
    # verification snapshots are immutable: forbid UPDATE of identity columns via trigger
    op.execute(
        """
        CREATE OR REPLACE FUNCTION agent_colab_forbid_snapshot_update() RETURNS trigger AS $$
        BEGIN
          IF NEW.implementer_account_id <> OLD.implementer_account_id
             OR NEW.verifier_account_id <> OLD.verifier_account_id
             OR NEW.implementer_agent_id IS DISTINCT FROM OLD.implementer_agent_id
             OR NEW.verifier_agent_id IS DISTINCT FROM OLD.verifier_agent_id
             OR NEW.implementer_credential_fingerprint <> OLD.implementer_credential_fingerprint
             OR NEW.verifier_credential_fingerprint <> OLD.verifier_credential_fingerprint
             OR NEW.identity_graph_version <> OLD.identity_graph_version
             OR NEW.effective_policy_hash <> OLD.effective_policy_hash
             OR NEW.criteria_version <> OLD.criteria_version
             OR NEW.target_commit <> OLD.target_commit
             OR NEW.snapshot_hash <> OLD.snapshot_hash THEN
            RAISE EXCEPTION 'VERIFICATION_SNAPSHOT_IMMUTABLE'
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_verification_runs_snapshot_immutable
        BEFORE UPDATE ON verification_runs
        FOR EACH ROW EXECUTE FUNCTION agent_colab_forbid_snapshot_update();
        """
    )
    op.execute(
        "CREATE RULE verification_runs_no_delete AS ON DELETE TO verification_runs "
        "DO INSTEAD NOTHING;"
    )


def downgrade() -> None:
    op.execute("DROP RULE IF EXISTS verification_runs_no_delete ON verification_runs")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_verification_runs_snapshot_immutable ON verification_runs"
    )
    op.execute("DROP FUNCTION IF EXISTS agent_colab_forbid_snapshot_update()")
    for table in (
        "verification_runs",
        "agents",
        "service_credentials",
        "account_aliases",
        "accounts",
        "workspaces",
    ):
        op.drop_table(table)
