"""VerificationRun commands on the command bus (P1-06).

Every command runs the same handler for REST and MCP. The implementer can never submit a verdict
on its own scope (``SELF_VERIFICATION_FORBIDDEN``): the application check covers account,
alias graph, and credential fingerprint; the DB CHECK constraints reject a same-identity run at
creation and the guarded revision INSERT rejects a same-identity submitter (V-P1-12).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from server.application.bus import (
    Command,
    CommandContext,
    CommandError,
    CommandResult,
    handles,
    require_permission,
)
from server.events.store import AppendRequest, AppendResult, EventStoreError
from server.observability.audit import append_audit
from server.verification.independence import (
    Identity,
    VerificationIndependenceError,
    check_independence,
)
from server.verification.runs import (
    VerificationError,
    VerificationOp,
    VerificationRun,
    VerificationStatus,
    alias_graph,
    append_revision,
    build_snapshot,
    check_submitter,
    load_run,
    next_status,
    relevant_alias_edges,
    set_status,
    snapshot_hash,
    validate_verdict,
)

# ---------------------------------------------------------------- commands


@dataclass(frozen=True)
class CreateVerificationRun(Command):
    """Create a VerificationRun with independent implementer/verifier identities."""

    target_type: str  # phase | task
    target_id: str
    implementer_account_id: str  # public account_id
    verifier_account_id: str
    implementer_credential_fingerprint: str
    verifier_credential_fingerprint: str
    target_commit: str
    effective_policy_hash: str
    criteria_version: str = "v8.0"
    identity_graph_version: str = "identity-v8-001"
    implementer_agent_id: str | None = None
    verifier_agent_id: str | None = None
    phase: int | None = None
    task_id: str | None = None
    idempotency_scope: str = "verification_run:create"


@dataclass(frozen=True)
class AssignVerifier(Command):
    verification_id: str
    idempotency_scope: str = "verification_run:assign"


@dataclass(frozen=True)
class StartVerification(Command):
    verification_id: str
    idempotency_scope: str = "verification_run:start"


@dataclass(frozen=True)
class SubmitEvidence(Command):
    verification_id: str
    evidence_refs: tuple[str, ...]
    sha256: str | None = None
    idempotency_scope: str = "verification_run:evidence"


@dataclass(frozen=True)
class SubmitVerdict(Command):
    """Verifier-only. ``report`` follows schemas/documents/verification-verdict.v1.schema.json."""

    verification_id: str
    result: str  # PASSED | FAILED | BLOCKED
    report: dict[str, Any] = field(default_factory=dict)
    idempotency_scope: str = "verification_run:result"


@dataclass(frozen=True)
class SubmitFix(Command):
    verification_id: str
    fix_commit: str
    note: str = ""
    idempotency_scope: str = "verification_run:fix"


@dataclass(frozen=True)
class RequestRecheck(Command):
    verification_id: str
    idempotency_scope: str = "verification_run:recheck"


@dataclass(frozen=True)
class CancelVerification(Command):
    verification_id: str
    reason_code: str = "SUPERSEDED"
    idempotency_scope: str = "verification_run:cancel"


# ---------------------------------------------------------------- helpers


def _account(ctx: CommandContext, account_id: str) -> tuple[str, str]:
    row = ctx.session.execute(
        text("SELECT id, account_type FROM accounts WHERE account_id = :a AND workspace_id = :w"),
        {"a": account_id, "w": uuid.UUID(ctx.workspace_id)},
    ).first()
    if row is None:
        raise CommandError("ACCOUNT_NOT_FOUND", account_id, status=404)
    return str(row[0]), str(row[1])


def _stream(ctx: CommandContext, verification_id: str) -> list[dict[str, Any]]:
    return ctx.store.stream(ctx.workspace_id, "verification_run", verification_id)


def _replay(ctx: CommandContext, verification_id: str, scope: str) -> AppendResult | None:
    for ev in _stream(ctx, verification_id):
        if (
            ev.get("idempotency_scope") == scope
            and ev.get("idempotency_key") == ctx.idempotency_key
            and ev.get("actor_account_id") == ctx.principal.account_uuid
        ):
            return AppendResult(
                ev["event_id"],
                ev["aggregate_seq"],
                ev["content_hash"],
                int(ev.get("recorded_seq", 0)),
                True,
            )
    return None


def _append(ctx: CommandContext, req: AppendRequest) -> AppendResult:
    try:
        return ctx.store.append(req)
    except EventStoreError as exc:
        status = 409 if exc.code in ("IDEMPOTENCY_CONFLICT", "SEQUENCE_CONFLICT") else 422
        raise CommandError(exc.code, exc.detail, status=status) from exc


def _event(
    ctx: CommandContext,
    run: VerificationRun,
    event_type: str,
    payload: dict[str, Any],
    scope: str,
) -> AppendResult:
    stream = _stream(ctx, run.verification_id)
    return _append(
        ctx,
        AppendRequest(
            workspace_id=ctx.workspace_id,
            aggregate_type="verification_run",
            aggregate_id=run.verification_id,
            type=event_type,
            actor_account_id=ctx.principal.account_uuid,
            correlation_id=ctx.correlation_id,
            idempotency_scope=scope,
            idempotency_key=ctx.idempotency_key,
            payload=payload,
            task_id=run.task_id,
            caused_by=stream[-1]["event_id"] if stream else None,
            expected_seq=(stream[-1]["aggregate_seq"] + 1) if stream else 1,
        ),
    )


def _run_or_error(ctx: CommandContext, verification_id: str) -> VerificationRun:
    try:
        run = load_run(ctx.session, verification_id)
    except VerificationError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    if run.workspace_id != ctx.workspace_id:
        raise CommandError("VERIFICATION_NOT_FOUND", verification_id, status=404)
    return run


def _result(res: AppendResult, run_id: str, status: str, **data: Any) -> CommandResult:
    return CommandResult(
        resource_id=run_id,
        event_id=res.event_id,
        aggregate_seq=res.aggregate_seq,
        aggregate_type="verification_run",
        replayed=res.replayed,
        data={"status": status, **data},
    )


def _transition(
    ctx: CommandContext, run: VerificationRun, op: VerificationOp, result: str | None = None
) -> VerificationStatus:
    try:
        return next_status(run.status, op, result)
    except VerificationError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc


def _audit_autonomous(ctx: CommandContext, **kwargs: Any) -> None:
    """Rejection audits must survive the rollback of the failing command (V-P1-12)."""
    from sqlalchemy.orm import Session

    bind = ctx.session.get_bind()
    with Session(bind=bind) as audit_session, audit_session.begin():
        append_audit(
            audit_session,
            actor_label=ctx.principal.account_id,
            correlation_id=ctx.correlation_id,
            workspace_id=uuid.UUID(ctx.workspace_id),
            actor_account_id=uuid.UUID(ctx.principal.account_uuid),
            clock=ctx.clock,
            **kwargs,
        )


def _now(ctx: CommandContext) -> dt.datetime:
    return ctx.clock.now()


# ---------------------------------------------------------------- handlers


@handles(CreateVerificationRun)
def create_verification_run(cmd: CreateVerificationRun, ctx: CommandContext) -> CommandResult:
    require_permission(ctx, "verification.assign", action="tool:verification_assign")
    if cmd.target_type not in ("phase", "task"):
        raise CommandError("VERIFICATION_TARGET_INVALID", cmd.target_type, status=400)
    impl_uuid, _ = _account(ctx, cmd.implementer_account_id)
    ver_uuid, _ = _account(ctx, cmd.verifier_account_id)
    graph = alias_graph(ctx.session, ctx.workspace_id)
    implementer = Identity(
        impl_uuid, cmd.implementer_credential_fingerprint, cmd.implementer_agent_id
    )
    verifier = Identity(ver_uuid, cmd.verifier_credential_fingerprint, cmd.verifier_agent_id)
    try:
        check_independence(implementer, verifier, alias_graph=graph)
    except VerificationIndependenceError as exc:
        _audit_autonomous(
            ctx,
            action="verification.create_rejected",
            target_type=cmd.target_type,
            target_id=cmd.target_id,
            result="REJECTED",
            error_code=exc.code,
            metadata={
                "implementer": cmd.implementer_account_id,
                "verifier": cmd.verifier_account_id,
            },
        )
        raise CommandError(exc.code, exc.detail, status=409) from exc
    # deterministic id per command so an idempotent retry finds the same run
    verification_id = (
        "vr-"
        + uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{ctx.workspace_id}|{ctx.principal.account_uuid}|{ctx.idempotency_key}",
        ).hex[:20]
    )
    existing = ctx.session.execute(
        text("SELECT 1 FROM verification_runs WHERE verification_id = :v"), {"v": verification_id}
    ).first()
    replay = _replay(ctx, verification_id, cmd.idempotency_scope)
    if existing and replay:
        return _result(replay, verification_id, load_run(ctx.session, verification_id).status)
    snapshot = build_snapshot(
        implementer,
        verifier,
        identity_graph_version=cmd.identity_graph_version,
        effective_policy_hash=cmd.effective_policy_hash,
        criteria_version=cmd.criteria_version,
        target_commit=cmd.target_commit,
        alias_edges=relevant_alias_edges(graph, impl_uuid, ver_uuid),
    )
    snap_hash = snapshot_hash(snapshot)
    try:
        with ctx.session.begin_nested():
            ctx.session.execute(
                text(
                    "INSERT INTO verification_runs (id, verification_id, workspace_id, "
                    "target_type, target_id, phase, task_id, implementer_account_id, "
                    "verifier_account_id, implementer_agent_id, verifier_agent_id, "
                    "implementer_credential_fingerprint, verifier_credential_fingerprint, "
                    "identity_graph_version, effective_policy_hash, criteria_version, "
                    "target_commit, status, snapshot_hash, created_by_account_id) "
                    "VALUES (:id, :vid, :ws, :tt, :tid, :phase, :task, :impl, :ver, :ia, :va, "
                    ":ifp, :vfp, :igv, :eph, :cv, :commit, 'PLANNED', :snap, :creator)"
                ),
                {
                    "id": uuid.uuid4(),
                    "vid": verification_id,
                    "ws": uuid.UUID(ctx.workspace_id),
                    "tt": cmd.target_type,
                    "tid": cmd.target_id,
                    "phase": cmd.phase,
                    "task": cmd.task_id,
                    "impl": uuid.UUID(impl_uuid),
                    "ver": uuid.UUID(ver_uuid),
                    "ia": cmd.implementer_agent_id,
                    "va": cmd.verifier_agent_id,
                    "ifp": cmd.implementer_credential_fingerprint,
                    "vfp": cmd.verifier_credential_fingerprint,
                    "igv": cmd.identity_graph_version,
                    "eph": cmd.effective_policy_hash,
                    "cv": cmd.criteria_version,
                    "commit": cmd.target_commit,
                    "snap": snap_hash,
                    "creator": uuid.UUID(ctx.principal.account_uuid),
                },
            )
            ctx.session.execute(
                text(
                    "INSERT INTO credential_identity_snapshots (verification_id, snapshot, "
                    "snapshot_hash) VALUES (:v, CAST(:s AS jsonb), :h)"
                ),
                {
                    "v": verification_id,
                    "s": json.dumps(snapshot, ensure_ascii=False),
                    "h": snap_hash,
                },
            )
    except IntegrityError as exc:
        raise CommandError("VERIFIER_NOT_INDEPENDENT_DB", str(exc.orig), status=409) from exc
    run = load_run(ctx.session, verification_id)
    res = _event(
        ctx,
        run,
        "VERIFIER_ASSIGNED",
        {
            "verification_id": verification_id,
            "target_type": cmd.target_type,
            "target_id": cmd.target_id,
            "verifier_account_id": cmd.verifier_account_id,
            "implementer_account_id": cmd.implementer_account_id,
            "criteria_version": cmd.criteria_version,
            "snapshot_hash": snap_hash,
        },
        cmd.idempotency_scope,
    )
    return _result(res, verification_id, "PLANNED", snapshot_hash=snap_hash)


def _simple_move(
    cmd: Command,
    ctx: CommandContext,
    verification_id: str,
    op: VerificationOp,
    permission: str,
    *,
    verifier_only: bool = False,
    implementer_only: bool = False,
    payload: dict[str, Any] | None = None,
) -> CommandResult:
    run = _run_or_error(ctx, verification_id)
    if verifier_only and ctx.principal.account_uuid != run.verifier_account_id:
        raise CommandError(
            "VERIFIER_MISMATCH", "only the assigned verifier may do this", status=409
        )
    if implementer_only and ctx.principal.account_uuid != run.implementer_account_id:
        raise CommandError("IMPLEMENTER_MISMATCH", "only the implementer may do this", status=409)
    require_permission(ctx, permission)
    replay = _replay(ctx, verification_id, cmd.idempotency_scope)
    if replay:
        return _result(replay, verification_id, run.status)
    target = _transition(ctx, run, op)
    set_status(ctx.session, verification_id, target)
    # state moves without a dedicated spec Event type reuse VERIFIER_ASSIGNED-style audit only
    append_audit(
        ctx.session,
        action=f"verification.{op.value}",
        target_type="verification_run",
        target_id=verification_id,
        result=target.value,
        actor_label=ctx.principal.account_id,
        correlation_id=ctx.correlation_id,
        workspace_id=uuid.UUID(ctx.workspace_id),
        actor_account_id=uuid.UUID(ctx.principal.account_uuid),
        metadata=payload or {},
        clock=ctx.clock,
    )
    stream = _stream(ctx, verification_id)
    last = stream[-1] if stream else None
    res = AppendResult(
        last["event_id"] if last else "",
        last["aggregate_seq"] if last else 0,
        last["content_hash"] if last else "",
        0,
        False,
    )
    return _result(res, verification_id, target.value)


@handles(AssignVerifier)
def assign_verifier(cmd: AssignVerifier, ctx: CommandContext) -> CommandResult:
    return _simple_move(cmd, ctx, cmd.verification_id, VerificationOp.ASSIGN, "verification.assign")


@handles(StartVerification)
def start_verification(cmd: StartVerification, ctx: CommandContext) -> CommandResult:
    result = _simple_move(
        cmd,
        ctx,
        cmd.verification_id,
        VerificationOp.START,
        "verification.submit",
        verifier_only=True,
    )
    if not result.replayed:
        _enter_task_verifying(ctx, cmd.verification_id)
    return result


def _enter_task_verifying(ctx: CommandContext, verification_id: str) -> None:
    """A Task target enters VERIFYING when its verification run starts (spec §8.2)."""
    from server.application.tasks import StartVerification as TaskStartVerification
    from server.application.tasks import load_task
    from server.application.tasks import start_verification as task_start_verification
    from server.verification.runs import load_run

    run = load_run(ctx.session, verification_id)
    if run.target_type != "task" or not run.task_id:
        return
    state = load_task(ctx, str(run.task_id))
    if state.status.value == "IMPLEMENTED":
        task_start_verification(
            TaskStartVerification(task_id=str(run.task_id), verification_id=verification_id), ctx
        )


@handles(SubmitFix)
def submit_fix(cmd: SubmitFix, ctx: CommandContext) -> CommandResult:
    return _simple_move(
        cmd,
        ctx,
        cmd.verification_id,
        VerificationOp.FIX,
        "task.submit",
        implementer_only=True,
        payload={"fix_commit": cmd.fix_commit, "note": cmd.note},
    )


@handles(RequestRecheck)
def request_recheck(cmd: RequestRecheck, ctx: CommandContext) -> CommandResult:
    return _simple_move(
        cmd, ctx, cmd.verification_id, VerificationOp.RECHECK, "verification.assign"
    )


@handles(CancelVerification)
def cancel_verification(cmd: CancelVerification, ctx: CommandContext) -> CommandResult:
    return _simple_move(
        cmd,
        ctx,
        cmd.verification_id,
        VerificationOp.CANCEL,
        "verification.assign",
        payload={"reason_code": cmd.reason_code},
    )


@handles(SubmitEvidence)
def submit_evidence(cmd: SubmitEvidence, ctx: CommandContext) -> CommandResult:
    run = _run_or_error(ctx, cmd.verification_id)
    if ctx.principal.account_uuid not in (run.implementer_account_id, run.verifier_account_id):
        require_permission(ctx, "verification.submit")
    if run.status in ("PASSED", "CANCELLED"):
        raise CommandError("VERIFICATION_TERMINAL", f"{run.status} is terminal", status=409)
    for ref in cmd.evidence_refs:
        ctx.session.execute(
            text(
                "INSERT INTO verification_evidence (verification_id, revision, evidence_ref, "
                "sha256, submitted_by_account_id) VALUES (:v, :r, :e, :h, :a)"
            ),
            {
                "v": cmd.verification_id,
                "r": run.current_revision + 1,
                "e": ref,
                "h": cmd.sha256,
                "a": uuid.UUID(ctx.principal.account_uuid),
            },
        )
    stream = _stream(ctx, cmd.verification_id)
    last = stream[-1]
    return _result(
        AppendResult(last["event_id"], last["aggregate_seq"], last["content_hash"], 0, False),
        cmd.verification_id,
        run.status,
        evidence_count=len(cmd.evidence_refs),
    )


@handles(SubmitVerdict)
def submit_verdict(cmd: SubmitVerdict, ctx: CommandContext) -> CommandResult:
    run = _run_or_error(ctx, cmd.verification_id)
    graph = alias_graph(ctx.session, ctx.workspace_id)
    try:
        check_submitter(
            run,
            submitter_account_id=ctx.principal.account_uuid,
            submitter_fingerprint=ctx.principal.credential_fingerprint,
            graph=graph,
        )
    except VerificationError as exc:
        _audit_autonomous(
            ctx,
            action=(
                "verification.self_submit_rejected"
                if exc.code == "SELF_VERIFICATION_FORBIDDEN"
                else "verification.submit_rejected"
            ),
            target_type="verification_run",
            target_id=cmd.verification_id,
            result="REJECTED",
            error_code=exc.code,
            metadata={"attempted_result": cmd.result},
        )
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    require_permission(ctx, "verification.submit", action="tool:verification_submit")
    replay = _replay(ctx, cmd.verification_id, cmd.idempotency_scope)
    if replay:
        return _result(replay, cmd.verification_id, run.status, revision=run.current_revision)
    report = dict(cmd.report) or {
        "result": cmd.result,
        "criteria_version": run.criteria_version,
        "tests": [],
        "findings": [],
        "residual_risks": [],
    }
    try:
        validate_verdict(report, cmd.result)
    except VerificationError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    target = _transition(ctx, run, VerificationOp.VERDICT, cmd.result)
    revision_no = run.current_revision + 1
    payload: dict[str, Any] = {"verification_id": cmd.verification_id, "revision": revision_no}
    if cmd.result == "PASSED":
        payload["evidence_refs"] = [t.get("evidence_ref", "") for t in report["tests"]]
    elif cmd.result == "FAILED":
        payload["finding_ids"] = [f["id"] for f in report["findings"]]
    else:
        payload["reason_code"] = str(report.get("reason_code") or "EXTERNAL_CONDITION")
    if run.target_type == "task" and run.task_id:
        res = _task_verdict_event(cmd, ctx, run, payload)
    else:
        res = _event(ctx, run, f"VERIFICATION_{cmd.result}", payload, cmd.idempotency_scope)
    try:
        revision = append_revision(
            ctx.session,
            run,
            result=cmd.result,
            submitted_by_account_id=ctx.principal.account_uuid,
            submitter_fingerprint=ctx.principal.credential_fingerprint,
            report=report,
            event_id=res.event_id,
            now=_now(ctx),
        )
    except VerificationError as exc:
        raise CommandError(exc.code, exc.detail, status=exc.status) from exc
    if run.target_type == "task" and run.task_id and not res.replayed:
        # Documentation Service: attempt/final version for the terminal verdict (spec §14.1)
        from server.application import documents as documents_app

        documents_app.on_verification_terminal(ctx, str(run.task_id), run.verification_id)
    return _result(
        res,
        cmd.verification_id,
        target.value,
        revision=revision.revision,
        revision_id=revision.revision_id,
        content_hash=revision.content_hash,
        report_sha256=revision.report_sha256,
    )


def _task_verdict_event(
    cmd: SubmitVerdict, ctx: CommandContext, run: VerificationRun, payload: dict[str, Any]
) -> AppendResult:
    """Task targets go through the Task package so the projection moves in the same transaction."""
    from server.application.tasks import RecordVerificationResult, record_verification_result

    task_cmd = RecordVerificationResult(
        task_id=str(run.task_id),
        verification_id=run.verification_id,
        result=cmd.result,
        revision=int(payload["revision"]),
        evidence_refs=tuple(payload.get("evidence_refs", [])),
        finding_ids=tuple(payload.get("finding_ids", [])),
        reason_code=str(payload.get("reason_code", "EXTERNAL_CONDITION")),
        idempotency_scope=cmd.idempotency_scope,
    )
    result = record_verification_result(task_cmd, ctx)
    return AppendResult(result.event_id, result.aggregate_seq, "", 0, result.replayed)


def get_run(ctx: CommandContext, verification_id: str) -> dict[str, Any]:
    """Read model: run + revisions + creation snapshot (verification.read or a party)."""
    from server.verification.runs import list_revisions, load_snapshot

    run = _run_or_error(ctx, verification_id)
    if ctx.principal.account_uuid not in (run.implementer_account_id, run.verifier_account_id):
        require_permission(ctx, "verification.read")
    snapshot, snap_hash = load_snapshot(ctx.session, verification_id)
    return {
        "verification_id": run.verification_id,
        "target_type": run.target_type,
        "target_id": run.target_id,
        "status": run.status.value,
        "result": run.result,
        "current_revision": run.current_revision,
        "criteria_version": run.criteria_version,
        "target_commit": run.target_commit,
        "snapshot": snapshot,
        "snapshot_hash": snap_hash,
        "revisions": list_revisions(ctx.session, verification_id),
    }
