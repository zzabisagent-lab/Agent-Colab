"""Setup Wizard HTTP surface (development plan §8.1-§8.3; P4-03), mounted at ``/setup``.

Every request passes the transport boundary first (loopback by default; remote only with a
TLS-terminating proxy, client mTLS, allowlist and a valid setup token). After LOCKED the bootstrap
endpoints answer 404 (information-disclosure policy); reconfiguration is a separate,
authenticated, time-boxed flow.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from server.api.deps import current_principal
from server.api.errors import ApiError
from server.domain.clock import SystemClock
from server.settings.preflight import as_dict
from server.setup.errors import SetupError
from server.setup.order import ApplyStep
from server.setup.state import SetupState
from server.setup.wizard import ProcessKilledError, SetupService

router = APIRouter(prefix="/setup", tags=["setup"])
TOKEN_HEADER = "X-Setup-Token"  # noqa: S105 - header name, not a value  # nosec B105
PROXY_PROTO_HEADER = "X-Forwarded-Proto"
PROXY_MTLS_HEADER = "X-Client-Cert-Verified"

_STATUS = {
    "SETUP_TOKEN_INVALID": 403,  # nosec B105 - error code, not a value
    "SETUP_TOKEN_USED": 403,  # nosec B105 - error code, not a value
    "SETUP_TOKEN_EXPIRED": 403,  # nosec B105 - error code, not a value
    "SETUP_TOKEN_MISSING": 403,  # nosec B105 - error code, not a value
    "SETUP_TOKEN_BLOCKED": 429,  # nosec B105 - error code, not a value
    "SETUP_LOCKED": 404,
    "SETUP_SESSION_EXPIRED": 403,
    "SETUP_REAUTH_REQUIRED": 403,
    "SETUP_TRANSITION_INVALID": 409,
    "SETUP_PREFLIGHT_REQUIRED": 409,
    "SETUP_INPUT_MISSING": 400,
    "SETUP_HANDLE_EXPIRED": 400,
    "SETUP_SECTION_UNKNOWN": 400,
    "SETUP_OWNER_EXISTS": 409,
}


def build_service(app: Any) -> SetupService:
    """Create (once) the SetupService for this app from its settings and environment."""
    existing = getattr(app.state, "setup", None)
    if isinstance(existing, SetupService):
        return existing
    settings = app.state.settings
    service = SetupService(
        clock=getattr(app.state, "clock", None) or SystemClock(),
        store_path=Path(settings.bootstrap_state_path),
        bind_host=settings.bind_host,
        master_key_id=settings.master_key_id,
        trust_proxy=os.environ.get("AGENT_COLAB_SETUP_TRUST_PROXY", "0") == "1",
        allowlist=tuple(filter(None, os.environ.get("AGENT_COLAB_SETUP_ALLOWLIST", "").split(","))),
        session_factory=getattr(app.state, "session_factory", None),
        crypto=getattr(getattr(app.state, "runtime", None), "crypto", None),
    )
    fail_after = os.environ.get("AGENT_COLAB_SETUP_FAIL_AFTER")
    if fail_after:
        service.fail_after = ApplyStep[fail_after]

    def on_configured(session_factory: Any, crypto: Any) -> None:
        if getattr(app.state, "session_factory", None) is None:
            from server.api.dispatch import default_runtime

            app.state.session_factory = session_factory
            app.state.runtime = default_runtime(session_factory, settings)
            app.state.runtime.crypto = crypto
        elif app.state.runtime is not None and app.state.runtime.crypto is None:
            app.state.runtime.crypto = crypto

    service.on_configured = on_configured
    try:
        service.load()
    except SetupError as exc:  # a corrupt or foreign store must stop setup, never be ignored
        service.machine.state = SetupState.UNINITIALIZED
        app.state.setup_load_error = exc.code
    app.state.setup = service
    return service


def _service(request: Request) -> SetupService:
    return build_service(request.app)


def _client_ip(request: Request) -> str:
    override = request.scope.get("client")
    return str(override[0]) if override else "0.0.0.0"  # noqa: S104 - unknown client is denied  # nosec B104


def _guard_transport(request: Request, service: SetupService) -> None:
    decision = service.transport(
        _client_ip(request),
        forwarded_proto=request.headers.get(PROXY_PROTO_HEADER),
        client_cert_verified=request.headers.get(PROXY_MTLS_HEADER, "").upper() == "SUCCESS",
        presented_token=request.headers.get(TOKEN_HEADER),
    )
    if not decision.allowed:
        raise ApiError(
            403,
            decision.code,
            "setup is not reachable from this transport",
            {"origin": decision.origin},
        )


def _raise(exc: SetupError) -> ApiError:
    return ApiError(_STATUS.get(exc.code, 400), exc.code, exc.detail)


class ConfigureBody(BaseModel):
    section: str = Field(pattern="^(db|keys|owner|integrations)$")
    values: dict[str, Any] = Field(default_factory=dict)


class BootstrapBody(BaseModel):
    token: str = Field(min_length=16, max_length=256)


class ReconfigureOpenBody(BaseModel):
    recovery_code: str = Field(min_length=16, max_length=32)


class ReconfigureApplyBody(BaseModel):
    changes: dict[str, Any]


@router.get("/state")
def state(request: Request) -> dict[str, Any]:
    service = _service(request)
    _guard_transport(request, service)
    view = service.state_view()
    view["load_error"] = getattr(request.app.state, "setup_load_error", None)
    return view


@router.post("/token", status_code=201)
def issue_token(request: Request) -> dict[str, Any]:
    service = _service(request)
    _guard_transport(request, service)
    try:
        value = service.issue_token()
    except SetupError as exc:
        raise _raise(exc) from exc
    return {"token": value, "expires_in_s": 30 * 60, "single_use": True, "shown_once": True}


@router.post("/configure")
def configure(body: ConfigureBody, request: Request) -> dict[str, Any]:
    service = _service(request)
    _guard_transport(request, service)
    try:
        return {"section": body.section, "accepted": service.configure(body.section, body.values)}
    except SetupError as exc:
        raise _raise(exc) from exc
    except ValueError as exc:  # settings validation
        raise ApiError(
            400, getattr(exc, "code", "SETUP_INPUT_INVALID"), getattr(exc, "detail", str(exc))
        ) from exc


@router.post("/preflight")
def preflight(request: Request) -> dict[str, Any]:
    service = _service(request)
    _guard_transport(request, service)
    try:
        results = service.preflight()
    except SetupError as exc:
        raise _raise(exc) from exc
    return {
        "ok": all(r.ok for r in results),
        "state": service.machine.state.value,
        "steps": [as_dict(r) for r in results],
    }


@router.get("/diff")
def diff(request: Request) -> dict[str, Any]:
    service = _service(request)
    _guard_transport(request, service)
    if service.machine.state not in (
        SetupState.UNINITIALIZED,
        SetupState.PREFLIGHT_PASSED,
        SetupState.BOOTSTRAP_FAILED,
    ):
        raise ApiError(404, "NOT_FOUND", "not found")
    return service.diff()


@router.post("/bootstrap")
def bootstrap(body: BootstrapBody, request: Request) -> dict[str, Any]:
    service = _service(request)
    _guard_transport(request, service)
    try:
        service.machine.require_bootstrap_open()
    except SetupError:
        raise ApiError(404, "NOT_FOUND", "not found") from None
    try:
        return service.bootstrap(body.token, _client_ip(request))
    except ProcessKilledError:
        raise  # test hook: the process is gone, no response is produced
    except SetupError as exc:
        raise _raise(exc) from exc


# ---------------------------------------------------------------- reconfiguration (LOCKED only)
def _owner(request: Request) -> tuple[str, str]:
    principal = current_principal(request)
    runtime = request.app.state.runtime
    from server.application.bus import CommandError
    from server.policy.authorization import AuthorizationDenied

    with runtime.session_factory() as session:
        try:
            runtime.authorizer.require(
                session,
                principal.account_id,
                "admin.settings",
                action="api:setup_reconfigure",
                correlation_id="setup:reconfigure",
            )
        except (AuthorizationDenied, CommandError) as exc:
            raise ApiError(403, "SETUP_REAUTH_REQUIRED", "System Owner required") from exc
    return principal.account_uuid, principal.account_id


@router.post("/reconfigure", status_code=201)
def reconfigure_open(body: ReconfigureOpenBody, request: Request) -> dict[str, Any]:
    service = _service(request)
    _guard_transport(request, service)
    if service.machine.state not in (SetupState.LOCKED, SetupState.RECONFIGURING):
        raise ApiError(404, "NOT_FOUND", "not found")
    owner_uuid, owner_id = _owner(request)
    from server.maintenance.mode import is_active

    with request.app.state.runtime.session_factory() as session:
        maintenance = is_active(session)
    try:
        opened, next_code = service.open_reconfiguration(
            owner_account_uuid=owner_uuid,
            owner_account_id=owner_id,
            recovery_code=body.recovery_code,
            maintenance_active=maintenance,
        )
    except SetupError as exc:
        raise _raise(exc) from exc
    return {
        "session_id": opened.session_id,
        "expires_at": opened.expires_at.isoformat(),
        "state": service.machine.state.value,
        "recovery_code_next": next_code,  # the used code is rotated; shown once
        "shown_once": True,
    }


@router.put("/reconfigure/{session_id}/settings")
def reconfigure_apply(
    session_id: str, body: ReconfigureApplyBody, request: Request
) -> dict[str, Any]:
    service = _service(request)
    _guard_transport(request, service)
    owner_uuid, owner_id = _owner(request)
    try:
        applied = service.apply_reconfiguration(
            session_id,
            owner_uuid,
            owner_id,
            body.changes,
            store=request.app.state.runtime.store_for,
        )
    except SetupError as exc:
        raise _raise(exc) from exc
    except ValueError as exc:
        raise ApiError(
            400, getattr(exc, "code", "SETTING_INVALID"), getattr(exc, "detail", str(exc))
        ) from exc
    return {"applied": applied, "state": service.machine.state.value}


@router.post("/reconfigure/{session_id}/close")
def reconfigure_close(session_id: str, request: Request) -> dict[str, Any]:
    service = _service(request)
    _guard_transport(request, service)
    _owner_uuid, owner_id = _owner(request)
    try:
        service.close_reconfiguration(session_id, owner_id)
    except SetupError as exc:
        raise _raise(exc) from exc
    return {"state": service.machine.state.value}
