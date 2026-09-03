"""Settings REST (P4-04): validation before apply, redacted diff/audit, rollback, preflight."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.api.deps import correlation_id_of, current_principal
from server.api.errors import ApiError
from server.application.bus import CommandError
from server.identity.principals import Principal
from server.settings import preflight as pf
from server.settings.registry import REGISTRY, SettingsError, spec_for, validate
from server.settings.store import SettingsStore

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class PutBody(BaseModel):
    value: Any
    reason: str = Field(default="", max_length=500)


class PreflightBody(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)


def _require_admin(request: Request, principal: Principal, session: Any) -> uuid.UUID:
    from server.policy.authorization import AuthorizationDenied

    runtime = request.app.state.runtime
    try:
        runtime.authorizer.require(
            session,
            principal.account_id,
            "admin.settings",
            action="api:settings_apply",
            correlation_id=correlation_id_of(request),
        )
    except (AuthorizationDenied, CommandError) as exc:
        raise ApiError(404, "NOT_FOUND", "not found") from exc
    return uuid.UUID(runtime.resolve_workspace(session, principal.account_uuid))


def _store(request: Request) -> SettingsStore:
    runtime = request.app.state.runtime
    return SettingsStore(runtime.crypto, runtime.clock)


def _settings_error(exc: SettingsError) -> ApiError:
    return ApiError(400 if exc.code != "SETTING_UNKNOWN" else 404, exc.code, exc.detail)


@router.get("")
def list_settings(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with runtime.session_factory() as session:
        _require_admin(request, principal, session)
        return {"items": [v.__dict__ for v in _store(request).views(session)]}


@router.get("/{key}")
def get_setting(key: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with runtime.session_factory() as session:
        _require_admin(request, principal, session)
        try:
            view = _store(request).view(session, key)
        except SettingsError as exc:
            raise _settings_error(exc) from exc
        return {**view.__dict__, "history": _store(request).history(session, key)}


@router.put("/{key}")
def put_setting(
    key: str, body: PutBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with runtime.session_factory() as session, session.begin():
        ws = _require_admin(request, principal, session)
        store = _store(request)
        try:
            spec = spec_for(key)
            validate(spec, body.value)  # rejected before any write (V-P4-05)
            if spec.preflight:
                result = _probe(request, session, spec.preflight, {key: body.value})
                if not result.ok:
                    raise ApiError(
                        409, "SETTING_PREFLIGHT_FAILED", result.code, {"probe": pf.as_dict(result)}
                    )
            stored = store.set(
                session,
                key,
                body.value,
                workspace_id=ws,
                changed_by=uuid.UUID(principal.account_uuid),
                actor_label=principal.account_id,
                correlation_id=correlation_id_of(request),
                reason=body.reason,
                store=runtime.store_for(session),
            )
        except SettingsError as exc:
            raise _settings_error(exc) from exc
        return {**store.view(session, key).__dict__, "audit_id": stored.audit_id}


@router.post("/{key}/rollback/{version}")
def rollback(key: str, version: int, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with runtime.session_factory() as session, session.begin():
        ws = _require_admin(request, principal, session)
        store = _store(request)
        try:
            stored = store.rollback(
                session,
                key,
                version,
                workspace_id=ws,
                changed_by=uuid.UUID(principal.account_uuid),
                actor_label=principal.account_id,
                correlation_id=correlation_id_of(request),
                store=runtime.store_for(session),
            )
        except SettingsError as exc:
            raise _settings_error(exc) from exc
        return {
            **store.view(session, key).__dict__,
            "audit_id": stored.audit_id,
            "rolled_back_to": version,
        }


def _probe(request: Request, session: Any, group: str, changes: dict[str, Any]) -> pf.ProbeResult:
    store = _store(request)

    def value(key: str) -> Any:
        return changes[key] if key in changes else store.value(session, key)

    if group == "mattermost":
        return pf.probe_mattermost(
            str(value("mattermost.url") or ""),
            str(value("mattermost.bot_token") or ""),
            str(value("mattermost.team") or ""),
            probe=getattr(request.app.state, "mattermost_probe", None),
        )
    if group == "storage":
        return pf.probe_storage(
            {
                "artifact_root": str(value("storage.artifact_root")),
                "document_root": str(value("storage.document_root")),
            }
        )
    return pf.probe_secret_provider(
        str(value("secrets.provider")), {"master_key_path": str(value("secrets.master_key_path"))}
    )


@router.post("/preflight")
def preflight(body: PreflightBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with runtime.session_factory() as session:
        _require_admin(request, principal, session)
        for key, value in body.changes.items():
            try:
                validate(spec_for(key), value)
            except SettingsError as exc:
                raise _settings_error(exc) from exc
        wanted: set[str] = set()
        for key in body.changes:
            group = REGISTRY[key].preflight
            if group is not None:
                wanted.add(group)
        groups: list[str] = sorted(wanted) or ["mattermost", "storage", "secrets"]
        results = [_probe(request, session, g, body.changes) for g in groups]
        return {"ok": all(r.ok for r in results), "steps": [pf.as_dict(r) for r in results]}
