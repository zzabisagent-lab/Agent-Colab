"""VerificationRun core (P1-06): states, immutable identity snapshots, chained revisions.

Authority tables: ``verification_runs`` (current status/revision), ``credential_identity_snapshots``
(immutable creation-time snapshot), ``verification_revisions`` (append-only hash chain, one row per
verdict), ``verification_evidence``, ``verification_findings``. Results are corrected only by new
revisions (spec §8.5, development plan §6.4, validation plan §5).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from server.events.canonical import canonical_json
from server.events.chain import VERIFICATION_CHAIN, chain_hash, hashed_row_fields, last_hash
from server.verification.independence import (
    Identity,
    VerificationIndependenceError,
    check_independence,
    effective_principal,
)

VERDICT_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "documents"
    / "verification-verdict.v1.schema.json"
)


class VerificationStatus(StrEnum):
    PLANNED = "PLANNED"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    FIX_SUBMITTED = "FIX_SUBMITTED"
    RECHECK_ASSIGNED = "RECHECK_ASSIGNED"


class VerificationOp(StrEnum):
    ASSIGN = "assign"
    START = "start"
    VERDICT = "verdict"
    FIX = "fix"
    RECHECK = "recheck"
    CANCEL = "cancel"


TERMINAL: frozenset[VerificationStatus] = frozenset(
    {VerificationStatus.PASSED, VerificationStatus.CANCELLED}
)

# (from, op) -> to ; VERDICT resolves to the submitted result
TRANSITIONS: dict[tuple[VerificationStatus, VerificationOp], VerificationStatus | None] = {
    (VerificationStatus.PLANNED, VerificationOp.ASSIGN): VerificationStatus.ASSIGNED,
    (VerificationStatus.ASSIGNED, VerificationOp.START): VerificationStatus.RUNNING,
    (VerificationStatus.RECHECK_ASSIGNED, VerificationOp.START): VerificationStatus.RUNNING,
    (VerificationStatus.RUNNING, VerificationOp.VERDICT): None,
    (VerificationStatus.FAILED, VerificationOp.FIX): VerificationStatus.FIX_SUBMITTED,
    (VerificationStatus.BLOCKED, VerificationOp.FIX): VerificationStatus.FIX_SUBMITTED,
    (VerificationStatus.FIX_SUBMITTED, VerificationOp.RECHECK): VerificationStatus.RECHECK_ASSIGNED,
    (VerificationStatus.BLOCKED, VerificationOp.RECHECK): VerificationStatus.RECHECK_ASSIGNED,
    (VerificationStatus.PLANNED, VerificationOp.CANCEL): VerificationStatus.CANCELLED,
    (VerificationStatus.ASSIGNED, VerificationOp.CANCEL): VerificationStatus.CANCELLED,
    (VerificationStatus.RUNNING, VerificationOp.CANCEL): VerificationStatus.CANCELLED,
    (VerificationStatus.FAILED, VerificationOp.CANCEL): VerificationStatus.CANCELLED,
    (VerificationStatus.BLOCKED, VerificationOp.CANCEL): VerificationStatus.CANCELLED,
    (VerificationStatus.FIX_SUBMITTED, VerificationOp.CANCEL): VerificationStatus.CANCELLED,
    (VerificationStatus.RECHECK_ASSIGNED, VerificationOp.CANCEL): VerificationStatus.CANCELLED,
}


class VerificationError(ValueError):
    def __init__(self, code: str, detail: str, status: int = 409) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


def next_status(
    current: VerificationStatus, op: VerificationOp, result: str | None = None
) -> VerificationStatus:
    """Pure transition; raises ``VERIFICATION_TRANSITION_INVALID`` / ``VERIFICATION_TERMINAL``."""
    if current in TERMINAL:
        raise VerificationError("VERIFICATION_TERMINAL", f"{current} is terminal")
    key = (current, op)
    if key not in TRANSITIONS:
        raise VerificationError(
            "VERIFICATION_TRANSITION_INVALID", f"{op} is not allowed in {current}"
        )
    target = TRANSITIONS[key]
    if target is None:
        if result not in ("PASSED", "FAILED", "BLOCKED"):
            raise VerificationError("VERIFICATION_RESULT_INVALID", str(result), status=400)
        return VerificationStatus(result)
    return target


@dataclass(frozen=True)
class VerificationRun:
    verification_id: str
    workspace_id: str
    target_type: str
    target_id: str
    phase: int | None
    task_id: str | None
    implementer_account_id: str  # uuid text
    verifier_account_id: str  # uuid text
    implementer_agent_id: str | None
    verifier_agent_id: str | None
    implementer_credential_fingerprint: str
    verifier_credential_fingerprint: str
    identity_graph_version: str
    effective_policy_hash: str
    criteria_version: str
    target_commit: str
    status: VerificationStatus
    current_revision: int
    result: str | None
    snapshot_hash: str


def load_run(session: Session, verification_id: str) -> VerificationRun:
    row = (
        session.execute(
            text(
                "SELECT verification_id, workspace_id, target_type, target_id, phase, task_id, "
                "implementer_account_id, verifier_account_id, implementer_agent_id, "
                "verifier_agent_id, implementer_credential_fingerprint, "
                "verifier_credential_fingerprint, identity_graph_version, effective_policy_hash, "
                "criteria_version, target_commit, status, current_revision, result, snapshot_hash "
                "FROM verification_runs WHERE verification_id = :v FOR UPDATE"
            ),
            {"v": verification_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise VerificationError("VERIFICATION_NOT_FOUND", verification_id, status=404)
    return VerificationRun(
        verification_id=row["verification_id"],
        workspace_id=str(row["workspace_id"]),
        target_type=row["target_type"],
        target_id=row["target_id"],
        phase=row["phase"],
        task_id=row["task_id"],
        implementer_account_id=str(row["implementer_account_id"]),
        verifier_account_id=str(row["verifier_account_id"]),
        implementer_agent_id=row["implementer_agent_id"],
        verifier_agent_id=row["verifier_agent_id"],
        implementer_credential_fingerprint=row["implementer_credential_fingerprint"],
        verifier_credential_fingerprint=row["verifier_credential_fingerprint"],
        identity_graph_version=row["identity_graph_version"],
        effective_policy_hash=row["effective_policy_hash"],
        criteria_version=row["criteria_version"],
        target_commit=row["target_commit"],
        status=VerificationStatus(row["status"]),
        current_revision=int(row["current_revision"]),
        result=row["result"],
        snapshot_hash=row["snapshot_hash"],
    )


# ---------------------------------------------------------------- alias graph and snapshots


def alias_graph(session: Session, workspace_uuid: str) -> dict[str, str]:
    """account uuid -> alias-of account uuid (canonical direction), for one workspace."""
    rows = session.execute(
        text(
            "SELECT al.account_id, al.alias_of_account_id FROM account_aliases al "
            "JOIN accounts a ON a.id = al.account_id WHERE a.workspace_id = :ws"
        ),
        {"ws": uuid.UUID(workspace_uuid)},
    ).all()
    return {str(r[0]): str(r[1]) for r in rows}


def relevant_alias_edges(graph: dict[str, str], *accounts: str) -> list[list[str]]:
    """Edges touching the given accounts or their alias chains (sorted, deterministic)."""
    keep: set[tuple[str, str]] = set()
    for acct in accounts:
        cur = acct
        seen: set[str] = set()
        while cur in graph and cur not in seen:
            seen.add(cur)
            keep.add((cur, graph[cur]))
            cur = graph[cur]
        for a, b in graph.items():
            if b == acct:
                keep.add((a, b))
    return [list(e) for e in sorted(keep)]


def build_snapshot(
    implementer: Identity,
    verifier: Identity,
    *,
    identity_graph_version: str,
    effective_policy_hash: str,
    criteria_version: str,
    target_commit: str,
    alias_edges: list[list[str]],
) -> dict[str, Any]:
    return {
        "implementer_account_id": implementer.account_id,
        "verifier_account_id": verifier.account_id,
        "implementer_agent_id": implementer.agent_id,
        "verifier_agent_id": verifier.agent_id,
        "implementer_credential_fingerprint": implementer.credential_fingerprint,
        "verifier_credential_fingerprint": verifier.credential_fingerprint,
        "identity_graph_version": identity_graph_version,
        "alias_edges": alias_edges,
        "effective_policy_hash": effective_policy_hash,
        "criteria_version": criteria_version,
        "target_commit": target_commit,
    }


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(snapshot)).hexdigest()


def independence_from_snapshot(snapshot: dict[str, Any]) -> None:
    """Re-evaluate independence purely from a stored snapshot (V-P1-24 reproducibility)."""
    graph = {a: b for a, b in snapshot.get("alias_edges", [])}
    check_independence(
        Identity(
            snapshot["implementer_account_id"],
            snapshot["implementer_credential_fingerprint"],
            snapshot.get("implementer_agent_id"),
        ),
        Identity(
            snapshot["verifier_account_id"],
            snapshot["verifier_credential_fingerprint"],
            snapshot.get("verifier_agent_id"),
        ),
        alias_graph=graph,
    )


def load_snapshot(session: Session, verification_id: str) -> tuple[dict[str, Any], str]:
    row = session.execute(
        text(
            "SELECT snapshot, snapshot_hash FROM credential_identity_snapshots "
            "WHERE verification_id = :v ORDER BY id ASC LIMIT 1"
        ),
        {"v": verification_id},
    ).first()
    if row is None:
        raise VerificationError("VERIFICATION_SNAPSHOT_MISSING", verification_id, status=404)
    snap = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return snap, str(row[1])


# ---------------------------------------------------------------- submitter independence


def check_submitter(
    run: VerificationRun,
    *,
    submitter_account_id: str,
    submitter_fingerprint: str,
    graph: dict[str, str],
) -> None:
    """Only the verifier identity may submit a verdict.

    The implementer, its aliases, and any credential sharing the implementer's fingerprint are
    ``SELF_VERIFICATION_FORBIDDEN``; any other account is ``VERIFIER_MISMATCH``.
    """
    impl_principal = effective_principal(run.implementer_account_id, graph)
    sub_principal = effective_principal(submitter_account_id, graph)
    if (
        submitter_account_id == run.implementer_account_id
        or sub_principal == impl_principal
        or submitter_fingerprint == run.implementer_credential_fingerprint
    ):
        raise VerificationError(
            "SELF_VERIFICATION_FORBIDDEN", "the implementer cannot verify its own scope"
        )
    if submitter_account_id != run.verifier_account_id:
        raise VerificationError("VERIFIER_MISMATCH", "only the assigned verifier may submit")
    if submitter_fingerprint == run.verifier_credential_fingerprint:
        return
    # a rotated verifier credential is fine as long as the account is the verifier; but the
    # fingerprint must not collide with the implementer (checked above)


# ---------------------------------------------------------------- verdict report


@lru_cache(maxsize=1)
def _verdict_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(VERDICT_SCHEMA.read_text(encoding="utf-8")))


def validate_verdict(report: dict[str, Any], result: str) -> None:
    errors = sorted(_verdict_validator().iter_errors(report), key=lambda e: list(e.path))
    if errors:
        path = "/".join(str(p) for p in errors[0].path) or "<root>"
        raise VerificationError(
            "VERDICT_REPORT_INVALID", f"{path}: {errors[0].message}", status=400
        )
    if report["result"] != result:
        raise VerificationError(
            "VERDICT_RESULT_MISMATCH", "report.result must equal the submitted result", status=400
        )
    if result == "PASSED":
        blocking = [
            f["id"] for f in report["findings"] if f["severity"] in ("Critical", "High", "Medium")
        ]
        if blocking or any(t["result"] not in ("PASS", "NOT_APPLICABLE") for t in report["tests"]):
            raise VerificationError(
                "VERDICT_PASS_NOT_JUSTIFIED",
                "PASSED requires every test PASS/NOT_APPLICABLE and no open Medium+ finding",
                status=400,
            )


def report_sha256(report: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(report)).hexdigest()


# ---------------------------------------------------------------- revisions (append-only chain)


@dataclass(frozen=True)
class Revision:
    revision_id: str
    verification_id: str
    revision: int
    result: str
    submitted_by_account_id: str
    submitter_credential_fingerprint: str
    report_sha256: str
    event_id: str
    previous_hash: str | None
    content_hash: str
    created_at: str


def append_revision(
    session: Session,
    run: VerificationRun,
    *,
    result: str,
    submitted_by_account_id: str,
    submitter_fingerprint: str,
    report: dict[str, Any],
    event_id: str,
    now: dt.datetime,
) -> Revision:
    """Insert the next chained revision row. The INSERT is guarded so that a row whose submitter
    equals the run's implementer can never be written, even if the application check is bypassed.
    """
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('verification_revisions_chain'))"))
    previous = last_hash(session, VERIFICATION_CHAIN)
    revision = run.current_revision + 1
    revision_id = f"vrr-{run.verification_id}-{revision:03d}"
    fields = {
        "revision_id": revision_id,
        "verification_id": run.verification_id,
        "revision": revision,
        "result": result,
        "submitted_by_account_id": uuid.UUID(submitted_by_account_id),
        "submitter_credential_fingerprint": submitter_fingerprint,
        "report_sha256": report_sha256(report),
        "event_id": event_id,
        "created_at": now,
    }
    content_hash = chain_hash(hashed_row_fields(VERIFICATION_CHAIN, fields), previous)
    inserted = session.execute(
        text(
            "INSERT INTO verification_revisions (revision_id, verification_id, revision, result, "
            "submitted_by_account_id, submitter_credential_fingerprint, report, report_sha256, "
            "event_id, previous_hash, content_hash, created_at) "
            "SELECT :revision_id, :verification_id, :revision, :result, :submitted_by_account_id, "
            ":submitter_credential_fingerprint, CAST(:report AS jsonb), :report_sha256, :event_id, "
            ":prev, :hash, :created_at "
            "WHERE NOT EXISTS (SELECT 1 FROM verification_runs r WHERE r.verification_id = "
            ":verification_id AND (r.implementer_account_id = :submitted_by_account_id OR "
            "r.implementer_credential_fingerprint = :submitter_credential_fingerprint))"
        ),
        {
            **fields,
            "report": json.dumps(report, ensure_ascii=False),
            "prev": previous,
            "hash": content_hash,
        },
    )
    if inserted.rowcount != 1:  # type: ignore[attr-defined]
        raise VerificationError("SELF_VERIFICATION_FORBIDDEN", "rejected by the revision guard")
    for f in report.get("findings", []):
        session.execute(
            text(
                "INSERT INTO verification_findings (finding_id, verification_id, revision, "
                "severity, summary, detail) VALUES (:fid, :v, :r, :sev, :sum, CAST(:d AS jsonb))"
            ),
            {
                "fid": f"{run.verification_id}:{revision}:{f['id']}",
                "v": run.verification_id,
                "r": revision,
                "sev": f["severity"],
                "sum": f["summary"],
                "d": json.dumps(f, ensure_ascii=False),
            },
        )
    session.execute(
        text(
            "UPDATE verification_runs SET current_revision = :r, result = :res, status = :st "
            "WHERE verification_id = :v"
        ),
        {"r": revision, "res": result, "st": result, "v": run.verification_id},
    )
    return Revision(
        revision_id=revision_id,
        verification_id=run.verification_id,
        revision=revision,
        result=result,
        submitted_by_account_id=submitted_by_account_id,
        submitter_credential_fingerprint=submitter_fingerprint,
        report_sha256=str(fields["report_sha256"]),
        event_id=event_id,
        previous_hash=previous,
        content_hash=content_hash,
        created_at=now.isoformat(),
    )


def list_revisions(session: Session, verification_id: str) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                "SELECT revision_id, revision, result, submitted_by_account_id, "
                "submitter_credential_fingerprint, report_sha256, event_id, previous_hash, "
                "content_hash, created_at FROM verification_revisions WHERE verification_id = :v "
                "ORDER BY revision"
            ),
            {"v": verification_id},
        )
        .mappings()
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["submitted_by_account_id"] = str(d["submitted_by_account_id"])
        d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    return out


def set_status(session: Session, verification_id: str, status: VerificationStatus) -> None:
    session.execute(
        text("UPDATE verification_runs SET status = :s WHERE verification_id = :v"),
        {"s": status.value, "v": verification_id},
    )


__all__ = [
    "TERMINAL",
    "TRANSITIONS",
    "Identity",
    "Revision",
    "VerificationError",
    "VerificationIndependenceError",
    "VerificationOp",
    "VerificationRun",
    "VerificationStatus",
    "alias_graph",
    "append_revision",
    "build_snapshot",
    "check_submitter",
    "independence_from_snapshot",
    "list_revisions",
    "load_run",
    "load_snapshot",
    "next_status",
    "relevant_alias_edges",
    "report_sha256",
    "set_status",
    "snapshot_hash",
    "validate_verdict",
]
