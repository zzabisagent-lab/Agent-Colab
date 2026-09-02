"""POST /api/v1/verification-runs (Phase 0 harness, V-P0-07)."""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field

from server.api.errors import ApiError
from server.application.verification import CreateVerificationRun, create_verification_run
from server.db.engine import session_scope
from server.identity.service_tokens import resolve_service_token
from server.verification.independence import VerificationIndependenceError

router = APIRouter(prefix="/api/v1", tags=["verification"])


class VerificationRunRequest(BaseModel):
    workspace_id: str
    target_type: str = Field(pattern="^(phase|task)$")
    target_id: str
    implementer_account_id: str
    verifier_account_id: str
    implementer_credential_fingerprint: str
    verifier_credential_fingerprint: str
    criteria_version: str = "v8.0"
    target_commit: str
    identity_graph_version: str
    effective_policy_hash: str
    implementer_agent_id: str | None = None
    verifier_agent_id: str | None = None
    phase: int | None = None
    task_id: str | None = None


class VerificationRunResponse(BaseModel):
    verification_id: str
    status: str = "PLANNED"


@router.post("/verification-runs", status_code=201, response_model=VerificationRunResponse)
async def post_verification_run(
    body: VerificationRunRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> VerificationRunResponse:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "AUTH_REQUIRED", "service token required")
    if not idempotency_key:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header required")
    factory = request.app.state.session_factory
    if factory is None:
        raise ApiError(503, "DATABASE_UNAVAILABLE", "database not configured")
    with session_scope(factory) as session:
        principal = resolve_service_token(session, authorization.removeprefix("Bearer "))
        if principal is None:
            raise ApiError(401, "AUTH_INVALID", "unknown or revoked credential")
        cmd = CreateVerificationRun(**body.model_dump(), created_by_account_id=principal.account_id)
        try:
            verification_id = create_verification_run(session, cmd)
        except VerificationIndependenceError as exc:
            status = 404 if exc.code.endswith("NOT_FOUND") else 409
            raise ApiError(status, exc.code, exc.detail) from exc
    return VerificationRunResponse(verification_id=verification_id)
