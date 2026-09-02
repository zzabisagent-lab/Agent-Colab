"""Verification REST routes on the common command dispatch (P1-06; development plan §7.2)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.dispatch import Runtime, command_error_to_api, dispatch, to_bus_principal
from server.application import bus
from server.application.verification import (
    AssignVerifier,
    CancelVerification,
    CreateVerificationRun,
    RequestRecheck,
    StartVerification,
    SubmitEvidence,
    SubmitFix,
    SubmitVerdict,
    get_run,
)
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/verification-runs", tags=["verification"])

PrincipalDep = Annotated[Principal, Depends(current_principal)]


class VerificationRunRequest(BaseModel):
    target_type: str = Field(pattern="^(phase|task)$")
    target_id: str
    implementer_account_id: str
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
    workspace_id: str | None = None  # accepted for compatibility; the credential decides


class EvidenceRequest(BaseModel):
    evidence_refs: list[str]
    sha256: str | None = None


class VerdictRequest(BaseModel):
    result: str = Field(pattern="^(PASSED|FAILED|BLOCKED)$")
    report: dict[str, Any] = Field(default_factory=dict)


class FixRequest(BaseModel):
    fix_commit: str
    note: str = ""


class CancelRequest(BaseModel):
    reason_code: str = "SUPERSEDED"


@router.post("", status_code=201)
def create_run(
    body: VerificationRunRequest, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    data = body.model_dump(exclude={"workspace_id"})
    out = dispatch(request, principal, CreateVerificationRun(**data))
    return {"verification_id": out["resource_id"], **out}


@router.post("/{verification_id}/assign")
def assign(verification_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, AssignVerifier(verification_id))


@router.post("/{verification_id}/start")
def start(verification_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, StartVerification(verification_id))


@router.post("/{verification_id}/evidence")
def evidence(
    verification_id: str, body: EvidenceRequest, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, SubmitEvidence(verification_id, tuple(body.evidence_refs), body.sha256)
    )


@router.post("/{verification_id}/verdict")
def verdict(
    verification_id: str, body: VerdictRequest, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, SubmitVerdict(verification_id, body.result, body.report))


@router.post("/{verification_id}/fix")
def fix(
    verification_id: str, body: FixRequest, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, SubmitFix(verification_id, body.fix_commit, body.note))


@router.post("/{verification_id}/recheck")
def recheck(verification_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, RequestRecheck(verification_id))


@router.post("/{verification_id}/cancel")
def cancel(
    verification_id: str, body: CancelRequest, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, CancelVerification(verification_id, body.reason_code))


@router.get("/{verification_id}")
def read_run(verification_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        ctx = bus.CommandContext(
            session=session,
            store=runtime.store_for(session),
            authorizer=runtime.authorizer,
            clock=runtime.clock,
            principal=to_bus_principal(principal),
            workspace_id=runtime.resolve_workspace(session),
            correlation_id=request.headers.get("X-Correlation-ID") or "-",
            idempotency_key="-",
        )
        try:
            return get_run(ctx, verification_id)
        except bus.CommandError as exc:
            raise command_error_to_api(exc) from exc
