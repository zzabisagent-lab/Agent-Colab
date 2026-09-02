"""VerificationRun creation command (Phase 0 harness; extended by P1-06)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import column, select, table, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.events.canonical import canonical_json
from server.verification.independence import (
    Identity,
    VerificationIndependenceError,
    check_independence,
)


@dataclass(frozen=True)
class CreateVerificationRun:
    workspace_id: str
    target_type: str
    target_id: str
    implementer_account_id: str
    verifier_account_id: str
    implementer_credential_fingerprint: str
    verifier_credential_fingerprint: str
    criteria_version: str
    target_commit: str
    identity_graph_version: str
    effective_policy_hash: str
    created_by_account_id: str
    implementer_agent_id: str | None = None
    verifier_agent_id: str | None = None
    phase: int | None = None
    task_id: str | None = None


def _alias_graph(session: Session, workspace_uuid: uuid.UUID) -> dict[str, str]:
    rows = session.execute(
        text(
            "SELECT a.account_id, b.account_id FROM account_aliases al "
            "JOIN accounts a ON a.id = al.account_id JOIN accounts b ON b.id = al.alias_of_account_id "
            "WHERE a.workspace_id = :ws"
        ),
        {"ws": workspace_uuid},
    ).all()
    return {str(r[0]): str(r[1]) for r in rows}


def _account_uuid(session: Session, workspace_uuid: uuid.UUID, account_id: str) -> uuid.UUID:

    accounts = table("accounts", column("id"), column("account_id"), column("workspace_id"))
    row = session.execute(
        select(accounts.c.id).where(
            accounts.c.account_id == account_id, accounts.c.workspace_id == workspace_uuid
        )
    ).first()
    if row is None:
        raise VerificationIndependenceError("ACCOUNT_NOT_FOUND", account_id)
    return uuid.UUID(str(row[0]))


def create_verification_run(session: Session, cmd: CreateVerificationRun) -> str:
    """Return the new verification_id; raise VerificationIndependenceError on any violation."""
    ws_row = session.execute(
        text("SELECT id FROM workspaces WHERE workspace_id = :w"), {"w": cmd.workspace_id}
    ).first()
    if ws_row is None:
        raise VerificationIndependenceError("WORKSPACE_NOT_FOUND", cmd.workspace_id)
    ws = uuid.UUID(str(ws_row[0]))
    check_independence(
        Identity(
            cmd.implementer_account_id,
            cmd.implementer_credential_fingerprint,
            cmd.implementer_agent_id,
        ),
        Identity(
            cmd.verifier_account_id, cmd.verifier_credential_fingerprint, cmd.verifier_agent_id
        ),
        alias_graph=_alias_graph(session, ws),
    )
    impl = _account_uuid(session, ws, cmd.implementer_account_id)
    ver = _account_uuid(session, ws, cmd.verifier_account_id)
    creator = _account_uuid(session, ws, cmd.created_by_account_id)
    snapshot = {
        "implementer_account_id": cmd.implementer_account_id,
        "verifier_account_id": cmd.verifier_account_id,
        "implementer_agent_id": cmd.implementer_agent_id,
        "verifier_agent_id": cmd.verifier_agent_id,
        "implementer_credential_fingerprint": cmd.implementer_credential_fingerprint,
        "verifier_credential_fingerprint": cmd.verifier_credential_fingerprint,
        "identity_graph_version": cmd.identity_graph_version,
        "effective_policy_hash": cmd.effective_policy_hash,
        "criteria_version": cmd.criteria_version,
        "target_commit": cmd.target_commit,
    }
    snapshot_hash = hashlib.sha256(canonical_json(snapshot)).hexdigest()
    verification_id = "vr-" + uuid.uuid4().hex[:16]
    params = {
        "id": uuid.uuid4(),
        "vid": verification_id,
        "ws": ws,
        "tt": cmd.target_type,
        "tid": cmd.target_id,
        "phase": cmd.phase,
        "task": cmd.task_id,
        "impl": impl,
        "ver": ver,
        "impl_agent": cmd.implementer_agent_id,
        "ver_agent": cmd.verifier_agent_id,
        "impl_fp": cmd.implementer_credential_fingerprint,
        "ver_fp": cmd.verifier_credential_fingerprint,
        "igv": cmd.identity_graph_version,
        "eph": cmd.effective_policy_hash,
        "cv": cmd.criteria_version,
        "commit": cmd.target_commit,
        "snap": snapshot_hash,
        "creator": creator,
    }
    insert = text(
        "INSERT INTO verification_runs (id, verification_id, workspace_id, target_type, "
        "target_id, phase, task_id, implementer_account_id, verifier_account_id, "
        "implementer_agent_id, verifier_agent_id, implementer_credential_fingerprint, "
        "verifier_credential_fingerprint, identity_graph_version, effective_policy_hash, "
        "criteria_version, target_commit, status, snapshot_hash, created_by_account_id) "
        "VALUES (:id, :vid, :ws, :tt, :tid, :phase, :task, :impl, :ver, :impl_agent, "
        ":ver_agent, :impl_fp, :ver_fp, :igv, :eph, :cv, :commit, 'PLANNED', :snap, :creator)"
    )
    try:
        session.execute(insert, params)
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise VerificationIndependenceError("VERIFIER_NOT_INDEPENDENT_DB", str(exc.orig)) from exc
    return verification_id
