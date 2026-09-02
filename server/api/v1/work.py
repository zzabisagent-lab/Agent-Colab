"""Work item REST surface for push Agents (development plan §7B.2; P3-11).

Service-token routes act as the Agent bound to the credential's Account (never a claimed
identity). ``/api/v1/agents/{agent_id}/webhook/callbacks`` accepts HMAC-signed callbacks from
webhook Agents without a service token: signature, 5-minute timestamp window, one-time nonce
(``webhook_nonces``, 24 h) and body hash are verified before any command runs (§7.5).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from server.agents import webhook_signing as ws
from server.agents.adapters.contract import AdapterError
from server.agents.push_common import load_agent
from server.agents.signing_keys import default_resolver
from server.api.deps import current_principal
from server.api.dispatch import Runtime, dispatch, execute_command
from server.api.errors import ApiError
from server.application import work as wk
from server.db.engine import session_scope
from server.domain import defaults
from server.domain.clock import Clock
from server.identity.principals import Principal
from server.work import inbox, receipts
from server.work.state import WorkItemError

router = APIRouter(prefix="/api/v1", tags=["work"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class RejectBody(BaseModel):
    reason_code: str = Field(pattern="^(CAPABILITY_UNSUPPORTED|CAPACITY|POLICY|OTHER)$")


class CallbackBody(BaseModel):
    op: str = Field(pattern="^(result|ack|reject)$")
    work_item_id: str
    result: dict[str, Any] | None = None
    reason_code: str | None = None


def _agent_of(session: Session, principal: Principal) -> str:
    row = session.execute(
        text("SELECT agent_id FROM agents WHERE account_id = :a"),
        {"a": uuid.UUID(principal.account_uuid)},
    ).first()
    if row is None:
        raise ApiError(404, "WORK_ITEM_NOT_FOUND", "not found")
    return str(row[0])


@router.get("/work/{work_item_id}")
def get_work_item(work_item_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        agent_id = _agent_of(session, principal)
        try:
            item = inbox.load(session, work_item_id)
        except WorkItemError as exc:
            raise ApiError(404, "WORK_ITEM_NOT_FOUND", "not found") from exc
        if item.agent_id != agent_id:  # normalized 404 (information-disclosure policy)
            raise ApiError(404, "WORK_ITEM_NOT_FOUND", "not found")
        envelope = item.to_delivery()
        return {
            **envelope,
            "status": item.status.value,
            "payload": item.payload,
            "receipts": [
                {"kind": r.receipt_kind, "delivery_no": r.delivery_no, "result_ref": r.result_ref}
                for r in receipts.receipts_of(session, work_item_id)
            ],
        }


@router.post("/work/{work_item_id}/ack")
def ack(work_item_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, wk.WorkAck(work_item_id=work_item_id))


@router.post("/work/{work_item_id}/reject")
def reject(
    work_item_id: str, body: RejectBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, wk.WorkReject(work_item_id=work_item_id, reason_code=body.reason_code)
    )


@router.post("/work/{work_item_id}/result")
def result(
    work_item_id: str, body: dict[str, Any], request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, wk.WorkResult(work_item_id=work_item_id, result=body))


# ------------------------------------------------------------- signed callbacks (no token)


class DbNonceStore:
    """One-time nonces kept 24 h in ``webhook_nonces`` (development plan §7B.2)."""

    def __init__(self, session: Session, agent_id: str) -> None:
        self._session = session
        self._agent_id = agent_id

    def remember(self, nonce: str, now: dt.datetime) -> bool:
        cutoff = now - dt.timedelta(hours=defaults.WEBHOOK_NONCE_RETENTION_H)
        self._session.execute(text("DELETE FROM webhook_nonces WHERE seen_at < :c"), {"c": cutoff})
        row = self._session.execute(
            text(
                "INSERT INTO webhook_nonces (nonce, agent_id, seen_at) VALUES (:n, :a, :t) "
                "ON CONFLICT (nonce) DO NOTHING RETURNING nonce"
            ),
            {"n": nonce, "a": self._agent_id, "t": now},
        ).first()
        return row is not None


def verify_callback(
    session: Session, agent_id: str, headers: dict[str, str], body: bytes, clock: Clock
) -> Any:
    """Resolve the Agent, its signing key and verify the callback; returns the AgentRecord."""
    agent = load_agent(session, agent_id)
    if agent is None or agent.adapter_type != "webhook" or not agent.credential_ref:
        raise ApiError(404, "AGENT_NOT_FOUND", "not found")
    if agent.status in ("revoked", "suspended"):
        raise ApiError(403, "AGENT_INACTIVE", f"agent is {agent.status}")
    key_ref = headers.get(ws.HEADER_KEY_REF, agent.credential_ref)
    if key_ref != agent.credential_ref:
        raise ApiError(401, "WEBHOOK_SIGNATURE_INVALID", "key reference mismatch")
    try:
        key = default_resolver().resolve(agent.credential_ref)
    except AdapterError as exc:
        raise ApiError(401, "WEBHOOK_SIGNATURE_INVALID", "signing key unavailable") from exc
    claim = headers.get("X-Colab-Body-Sha256")
    try:
        ws.verify(
            key,
            headers,
            body,
            clock,
            DbNonceStore(session, agent_id),
            body_sha256_claim=claim,
        )
    except ws.WebhookError as exc:
        raise ApiError(401, exc.code, exc.detail or exc.code) from exc
    return agent


def _principal_for(agent: Any) -> Principal:
    return Principal(
        account_id=agent.account_id,
        account_uuid=agent.account_uuid,
        account_type="agent",
        credential_fingerprint=f"webhook-hmac:{agent.credential_ref}",
        credential_kind="service_token",
    )


@router.post("/agents/{agent_id}/webhook/callbacks", status_code=200)
async def webhook_callback(agent_id: str, request: Request) -> dict[str, Any]:
    runtime: Runtime = request.app.state.runtime
    raw = await request.body()
    headers = {k: v for k, v in request.headers.items()}
    canonical = {
        ws.HEADER_TIMESTAMP: headers.get(ws.HEADER_TIMESTAMP.lower(), ""),
        ws.HEADER_NONCE: headers.get(ws.HEADER_NONCE.lower(), ""),
        ws.HEADER_SIGNATURE: headers.get(ws.HEADER_SIGNATURE.lower(), ""),
    }
    if key_ref := headers.get(ws.HEADER_KEY_REF.lower()):
        canonical[ws.HEADER_KEY_REF] = key_ref
    if claim := headers.get("x-colab-body-sha256"):
        canonical["X-Colab-Body-Sha256"] = claim
    for name in (ws.HEADER_TIMESTAMP, ws.HEADER_NONCE, ws.HEADER_SIGNATURE):
        if not canonical[name]:
            raise ApiError(401, "WEBHOOK_HEADER_MISSING", name)
    with session_scope(runtime.session_factory) as session:
        agent = verify_callback(session, agent_id, canonical, raw, runtime.clock)
    try:
        body = CallbackBody.model_validate(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise ApiError(422, "WEBHOOK_BODY_INVALID", "callback body is not valid") from exc
    command: Any
    if body.op == "result":
        if body.result is None:
            raise ApiError(422, "WEBHOOK_BODY_INVALID", "result required")
        command = wk.WorkResult(work_item_id=body.work_item_id, result=body.result)
    elif body.op == "ack":
        command = wk.WorkAck(work_item_id=body.work_item_id)
    else:
        if body.reason_code is None:
            raise ApiError(422, "WEBHOOK_BODY_INVALID", "reason_code required")
        command = wk.WorkReject(work_item_id=body.work_item_id, reason_code=body.reason_code)
    res = execute_command(
        runtime,
        _principal_for(agent),
        command,
        idempotency_key=f"webhook-cb:{canonical[ws.HEADER_NONCE]}",
        correlation_id=headers.get("x-colab-correlation-id") or f"webhook-cb:{agent_id}",
    )
    return {
        "resource_id": res.resource_id,
        "event_id": res.event_id,
        "aggregate_type": res.aggregate_type,
        "replayed": res.replayed,
        **(res.data or {}),
    }
