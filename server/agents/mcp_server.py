"""MCP tool surface on the same command handlers (development plan §7.4, §7B.3, §7.5).

Tools are registered from ``TOOL_MAP`` (tool name → command class); each call parses the input
into the command dataclass, resolves the principal from the Bearer service token (never from the
input), and executes through ``execute_command`` — the same path as REST. Errors carry the same
stable codes. Transport: Streamable HTTP mounted at ``/mcp``.
"""

from __future__ import annotations

import dataclasses
import inspect
import uuid
from collections.abc import Callable
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl

from server.api.dispatch import Runtime, execute_command
from server.api.errors import ApiError
from server.application import bus
from server.db.engine import session_scope
from server.identity.principals import Principal, resolve_service_token

SCHEMA_ID_BASE = "https://agent-colab.dev/schemas/api/mcp"

# tool name -> (command class, required top-level idempotency behaviour)
TOOL_MAP: dict[str, type[bus.Command]] = {}


def register_tool(name: str, command_type: type[bus.Command]) -> None:
    TOOL_MAP[name] = command_type


class ServiceTokenVerifier(TokenVerifier):
    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def verify_token(self, token: str) -> AccessToken | None:
        with session_scope(self._runtime.session_factory) as session:
            principal = resolve_service_token(session, token)
        if principal is None:
            return None
        return AccessToken(
            token=token,
            client_id=principal.account_id,
            scopes=["agent"],
            subject=principal.account_id,
            claims={
                "account_uuid": principal.account_uuid,
                "account_type": principal.account_type,
                "fingerprint": principal.credential_fingerprint,
            },
        )


def _principal_from_token() -> Principal:
    token = get_access_token()
    if token is None:
        raise ApiError(401, "AUTH_REQUIRED", "service token required")
    claims = token.claims or {}
    return Principal(
        account_id=str(token.subject or token.client_id),
        account_uuid=str(claims.get("account_uuid")),
        account_type=str(claims.get("account_type", "agent")),
        credential_fingerprint=str(claims.get("fingerprint", "")),
        credential_kind="service_token",
        mfa_verified=False,
        reauth_at=None,
    )


def _make_tool(runtime: Runtime, name: str, command_type: type[bus.Command]) -> Callable[..., Any]:
    fields = [f for f in dataclasses.fields(command_type)]  # type: ignore[arg-type]

    async def tool(**kwargs: Any) -> dict[str, Any]:
        idem = kwargs.pop("idempotency_key", None) or f"mcp-{uuid.uuid4().hex}"
        correlation = kwargs.pop("correlation_id", None) or f"corr-{uuid.uuid4().hex[:16]}"
        expected_seq = kwargs.pop("expected_seq", None)
        # actor claims in tool input are ignored (credential decides); audited by the bus later
        for spoof in ("actor_account_id", "on_behalf_of"):
            kwargs.pop(spoof, None)
        principal = _principal_from_token()
        try:
            command = command_type(**kwargs)
            result = execute_command(
                runtime,
                principal,
                command,
                idempotency_key=idem,
                correlation_id=correlation,
                expected_seq=expected_seq,
            )
        except ApiError as exc:
            return {
                "error": {"code": exc.code, "status": exc.status, "detail": exc.detail},
                "schema_id": f"{SCHEMA_ID_BASE}/error.v1",
            }
        except TypeError as exc:
            return {
                "error": {"code": "COMMAND_ARGS_INVALID", "status": 400, "detail": str(exc)},
                "schema_id": f"{SCHEMA_ID_BASE}/error.v1",
            }
        return {
            "schema_id": f"{SCHEMA_ID_BASE}/{name}.result.v1",
            "resource_id": result.resource_id,
            "event_id": result.event_id,
            "aggregate_type": result.aggregate_type,
            "aggregate_seq": result.aggregate_seq,
            "replayed": result.replayed,
            **result.data,
        }

    params = [
        inspect.Parameter(
            f.name,
            inspect.Parameter.KEYWORD_ONLY,
            default=(
                f.default if f.default is not dataclasses.MISSING else inspect.Parameter.empty
            ),
            annotation=f.type,
        )
        for f in fields
    ] + [
        inspect.Parameter(
            "idempotency_key", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None
        ),
        inspect.Parameter(
            "correlation_id", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None
        ),
        inspect.Parameter(
            "expected_seq", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=int | None
        ),
    ]
    tool.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    tool.__name__ = name
    tool.__doc__ = (command_type.__doc__ or name).strip()
    return tool


def build_mcp_server(runtime: Runtime, base_url: str) -> MCPServer:
    server = MCPServer(
        name="Agent-Colab",
        version="0.0.0",
        token_verifier=ServiceTokenVerifier(runtime),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(base_url), resource_server_url=AnyHttpUrl(base_url)
        ),
    )
    for name, command_type in TOOL_MAP.items():
        server.tool(name=name)(_make_tool(runtime, name, command_type))
    return server
