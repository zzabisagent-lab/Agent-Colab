"""Secret Broker REST (P4-06/P4-07; docs/protocol/secret-sidecar-api.md).

Admin: register/rotate/list metadata, grants, revocation, exposure requests. Agent/sidecar:
resolve a one-time handle (value returned exactly once, base64, never logged), acknowledge
cleanup, and follow the revocation feed (long-poll or SSE). Every denial is a 403 with a stable
code and no value; forbidden and unknown look the same (§7.5).
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.api.deps import current_principal
from server.api.dispatch import Runtime, dispatch
from server.api.errors import ApiError
from server.application import secrets as sc
from server.db.engine import session_scope
from server.identity.principals import Principal
from server.secrets import broker
from server.secrets import leases as ls
from server.secrets import local_provider as lp

router = APIRouter(prefix="/api/v1/secrets", tags=["secrets"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]
REVOCATION_POLL_MAX_S = 5.0


def _runtime(request: Request) -> Runtime:
    rt: Runtime = request.app.state.runtime
    return rt


def _extras(request: Request) -> dict[str, Any]:
    rt = _runtime(request)
    master = getattr(request.app.state, "secret_master_key", None)
    extras: dict[str, Any] = {"crypto": rt.crypto}
    if master is not None:
        extras["master_key"] = master
    return extras


_ENVELOPE = ("resource_id", "event_id", "aggregate_type", "aggregate_seq", "replayed")


def _data(result: dict[str, Any]) -> dict[str, Any]:
    """The command's own data (dispatch merges it with the Event envelope fields)."""
    return {k: v for k, v in result.items() if k not in _ENVELOPE}


def _dispatch(request: Request, principal: Principal, command: Any) -> dict[str, Any]:
    return dispatch(request, principal, command, **_extras(request))


# ------------------------------------------------------------------ admin


class RegisterBody(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._/-]{0,119}$")
    value_b64: str = Field(min_length=1, repr=False)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RotateBody(BaseModel):
    value_b64: str = Field(min_length=1, repr=False)


class GrantBody(BaseModel):
    agent_id: str
    task_id: str | None = None
    action: str | None = None
    ttl_seconds: int = Field(default=300, ge=1, le=3600)
    single_use: bool = True
    valid_for_seconds: int | None = Field(default=None, ge=60, le=30 * 24 * 3600)


class RevokeBody(BaseModel):
    reason_code: str = Field(default="ADMIN_REVOKE", pattern="^[A-Z][A-Z0-9_]{1,63}$")
    kind: str = Field(default="grant", pattern="^(grant|lease|task|agent|secret)$")


class ExposureBody(BaseModel):
    grant_id: str
    task_id: str
    reason: str = "llm_context"


def _value(b64: str) -> bytes:
    try:
        return base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise ApiError(400, "SECRET_VALUE_INVALID", "value_b64 must be base64") from exc


@router.post("", status_code=201)
def register_secret(
    body: RegisterBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    result = _dispatch(
        request, principal, sc.RegisterSecret(body.name, _value(body.value_b64), body.metadata)
    )
    return {"secret_ref": result["resource_id"], "version": 1, "event_id": result.get("event_id")}


@router.get("")
def list_secrets(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    rt = _runtime(request)
    with session_scope(rt.session_factory) as s:
        _require(rt, s, principal, "secret.register", "api:secret_register")
        ws = uuid.UUID(rt.resolve_workspace(s, principal.account_uuid))
        return {"items": lp.list_secrets(s, ws)}  # metadata only: no values, ever


@router.get("/revocations")
async def revocations(
    request: Request,
    principal: PrincipalDep,
    since: int = Query(default=0, ge=0),
    max_wait_s: float = Query(default=0.0, ge=0.0, le=REVOCATION_POLL_MAX_S),
) -> dict[str, Any]:
    """Revocation feed after ``since`` (long-poll up to 5 s: the sidecar's poll interval)."""
    rt = _runtime(request)
    deadline = asyncio.get_event_loop().time() + max_wait_s
    while True:
        rows = await asyncio.to_thread(_read_revocations, rt, principal, since)
        if rows or asyncio.get_event_loop().time() >= deadline:
            return {"items": rows, "next_since": rows[-1]["seq"] if rows else since}
        await asyncio.sleep(0.25)


@router.get("/revocations/stream")
async def revocations_stream(
    request: Request,
    principal: PrincipalDep,
    since: int = Query(default=0, ge=0),
    max_events: int | None = Query(default=None, ge=1),
    poll_seconds: float = Query(default=1.0, ge=0.05, le=5.0),
) -> StreamingResponse:
    """SSE push of revocations (§9.4 revoke push); sidecars fall back to the 5-second poll."""
    rt = _runtime(request)
    cursor = since

    async def generate() -> AsyncIterator[bytes]:
        nonlocal cursor
        sent = 0
        yield b": connected\n\n"
        while True:
            if await request.is_disconnected():
                return
            batch = await asyncio.to_thread(_read_revocations, rt, principal, cursor)
            for rev in batch:
                cursor = int(rev["seq"])
                yield f"id: {cursor}\nevent: revocation\ndata: {json.dumps(rev)}\n\n".encode()
                sent += 1
                if max_events is not None and sent >= max_events:
                    return
            if not batch:
                await asyncio.sleep(poll_seconds)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/grants/{grant_id}")
def get_grant(grant_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    rt = _runtime(request)
    with session_scope(rt.session_factory) as s:
        _require(rt, s, principal, "secret.grant", "api:secret_grant")
        ws = uuid.UUID(rt.resolve_workspace(s, principal.account_uuid))
        grant = broker.load_grant(s, grant_id)
        if grant is None or grant.workspace_id != ws:
            raise ApiError(404, "NOT_FOUND", "not found")
        return broker.grant_view(grant)


@router.post("/grants/{grant_id}/revoke")
def revoke_grant(
    grant_id: str, body: RevokeBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    result = _dispatch(
        request, principal, sc.RevokeSecretGrant(grant_id, "grant", body.reason_code)
    )
    return {"grant_id": grant_id, "revoked_leases": result.get("revoked_leases", [])}


@router.post("/revoke")
def revoke_scope(
    body: RevokeBody, request: Request, principal: PrincipalDep, target_id: str = Query(...)
) -> dict[str, Any]:
    """Revoke every lease/grant of a Task or Agent (or a lease/secret) — kind in the body."""
    result = _dispatch(
        request, principal, sc.RevokeSecretGrant(target_id, body.kind, body.reason_code)
    )
    return {
        "target_id": target_id,
        "kind": body.kind,
        "revoked_leases": result.get("revoked_leases", []),
    }


@router.post("/{secret_ref}/grants", status_code=201)
def create_grant(
    secret_ref: str, body: GrantBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    result = _dispatch(
        request,
        principal,
        sc.CreateSecretGrant(
            secret_ref,
            body.agent_id,
            body.task_id,
            body.action,
            body.ttl_seconds,
            body.single_use,
            body.valid_for_seconds,
        ),
    )
    return _data(result)


@router.post("/{secret_ref}/rotate")
def rotate_secret(
    secret_ref: str, body: RotateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    result = _dispatch(request, principal, sc.RotateSecret(secret_ref, _value(body.value_b64)))
    return _data(result)


@router.post("/{secret_ref}/exposure-requests", status_code=201)
def request_exposure(
    secret_ref: str, body: ExposureBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    result = _dispatch(
        request, principal, sc.RequestSecretExposure(body.grant_id, body.task_id, body.reason)
    )
    return _data(result)


@router.get("/{secret_ref}")
def get_secret(secret_ref: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    rt = _runtime(request)
    with session_scope(rt.session_factory) as s:
        _require(rt, s, principal, "secret.register", "api:secret_register")
        ws = uuid.UUID(rt.resolve_workspace(s, principal.account_uuid))
        view = lp.secret_view(s, secret_ref)
        if view is None or not _in_workspace(s, secret_ref, ws):
            raise ApiError(404, "NOT_FOUND", "not found")
        return view


# ------------------------------------------------------------------ Agent / sidecar


class LeaseBody(BaseModel):
    task_id: str | None = None
    action: str | None = None
    work_item_id: str | None = None
    sidecar_instance_id: str | None = None
    ttl_seconds: int | None = Field(default=None, ge=1, le=3600)


class ResolveBody(BaseModel):
    handle: str = Field(pattern=r"^sh-[0-9a-f]{32}$", repr=False)
    sidecar_instance_id: str | None = None
    work_item_id: str | None = None
    task_id: str | None = None
    action: str | None = None
    purpose: str = Field(default="adapter", pattern="^(adapter|llm_context)$")


@router.post("/{secret_ref}/leases", status_code=201)
def issue_lease(
    secret_ref: str, body: LeaseBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    result = _dispatch(
        request,
        principal,
        sc.IssueSecretLease(
            secret_ref,
            body.task_id,
            body.action,
            body.work_item_id,
            body.sidecar_instance_id,
            body.ttl_seconds,
        ),
    )
    return _data(result)


@router.post("/resolve")
def resolve(body: ResolveBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    """One-time resolve. 403 + stable code on any denial; the value is returned exactly once."""
    try:
        result = _dispatch(
            request,
            principal,
            sc.ResolveSecret(
                body.handle,
                body.sidecar_instance_id,
                body.work_item_id,
                body.task_id,
                body.action,
                body.purpose,
            ),
        )
    except ApiError as exc:
        if exc.status in (403, 404) or exc.code.startswith("SECRET_"):
            raise ApiError(
                403,
                exc.code if exc.code.startswith("SECRET_") else "SECRET_SCOPE_DENIED",
                "resolve denied",
            ) from exc
        raise
    return {"lease_id": result["resource_id"], "secret_b64": result.get("secret_b64", "")}


@router.post("/leases/{lease_id}/ack-cleanup")
def ack_cleanup(lease_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    result = _dispatch(request, principal, sc.AckLeaseCleanup(lease_id))
    return {"lease_id": lease_id, "acknowledged": bool(result.get("acknowledged"))}


# ------------------------------------------------------------------ helpers


def _require(
    rt: Runtime, session: Session, principal: Principal, permission: str, action: str
) -> None:
    if rt.authorizer is None:
        raise ApiError(403, "POLICY_DENIED", "no authorizer")
    from server.application.bus import CommandError

    try:
        rt.authorizer.require(
            session, principal.account_id, permission, action=action, correlation_id="-"
        )
    except CommandError as exc:
        raise ApiError(404, "NOT_FOUND", "not found") from exc
    except Exception as exc:  # AuthorizationDenied from the policy engine
        code = getattr(exc, "code", "POLICY_DENIED")
        raise ApiError(404, "NOT_FOUND", "not found", {"code": code}) from exc


def _in_workspace(session: Session, secret_ref: str, ws: uuid.UUID) -> bool:
    from sqlalchemy import text

    return (
        session.execute(
            text("SELECT 1 FROM secrets WHERE secret_ref = :r AND workspace_id = :w"),
            {"r": secret_ref, "w": ws},
        ).first()
        is not None
    )


def _read_revocations(rt: Runtime, principal: Principal, since: int) -> list[dict[str, Any]]:
    with session_scope(rt.session_factory) as s:
        ws = uuid.UUID(rt.resolve_workspace(s, principal.account_uuid))
        rows = ls.revocations_since(s, since, workspace_id=ws)
        if principal.account_type == "agent":
            from server.agents.registry import agent_for_account

            row = agent_for_account(s, uuid.UUID(principal.account_uuid))
            agent_id = row.agent_id if row else None
            rows = [
                r
                for r in rows
                if (r.kind != "agent" and _touches_agent(s, r, agent_id)) or r.target_id == agent_id
            ]
        return [ls.revocation_dict(r) for r in rows]


def _touches_agent(session: Session, rev: ls.Revocation, agent_id: str | None) -> bool:
    """Sidecars see only revocations of their own Agent's leases (never other Agents')."""
    if agent_id is None or not rev.lease_ids:
        return False
    from sqlalchemy import text

    row = session.execute(
        text("SELECT count(*) FROM secret_leases WHERE agent_id = :a AND lease_id = ANY(:ids)"),
        {"a": agent_id, "ids": list(rev.lease_ids)},
    ).scalar_one()
    return int(row) > 0
