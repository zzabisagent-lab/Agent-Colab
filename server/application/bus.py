"""Common command bus (development plan §7.5): REST, MCP, and Mattermost commands all execute the
same application command handlers, with the same Policy check, idempotency scope, and Event
append. No other path may change state.

A handler receives a ``CommandContext`` (transaction-bound session, Event store, authorizer,
clock, principal, correlation, idempotency key) and returns a ``CommandResult`` carrying the
created resource ID, Event ID, and aggregate sequence. Handlers are registered per command type
with ``@handles(CommandType)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.events.store import EventStore


class CommandError(Exception):
    """Stable, transport-independent error (mapped to Problem Details / MCP errors)."""

    def __init__(
        self, code: str, detail: str, status: int = 409, extra: dict[str, Any] | None = None
    ):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status
        self.extra = extra or {}


@dataclass(frozen=True)
class Principal:
    account_id: str  # public account_id (e.g. acct-...)
    account_uuid: str
    account_type: str  # human | agent | service
    credential_fingerprint: str
    agent_id: str | None = None
    mfa_verified: bool = False


class AuthorizerLike(Protocol):
    def require(
        self,
        session: Session,
        principal_account_uuid: str,
        permission: str,
        *,
        action: str | None = None,
        domain: str | None = None,
        channel_id: str | None = None,
        resource: str | None = None,
        side_effect: bool = False,
        capability: str | None = None,
        correlation_id: str = "",
    ) -> Any: ...


@dataclass
class CommandContext:
    session: Session
    store: EventStore
    authorizer: AuthorizerLike | None
    clock: Clock
    principal: Principal
    workspace_id: str
    correlation_id: str
    idempotency_key: str
    expected_seq: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    resource_id: str
    event_id: str
    aggregate_seq: int
    aggregate_type: str
    replayed: bool = False
    data: dict[str, Any] = field(default_factory=dict)


class Command:
    """Marker base for command dataclasses. ``idempotency_scope`` names the aggregate:operation."""

    idempotency_scope: str = ""


Handler = Callable[[Any, CommandContext], CommandResult]

_HANDLERS: dict[type[Command], Handler] = {}


def handles[C: Command](
    command_type: type[C],
) -> Callable[
    [Callable[[C, CommandContext], CommandResult]], Callable[[C, CommandContext], CommandResult]
]:
    def register(
        fn: Callable[[C, CommandContext], CommandResult],
    ) -> Callable[[C, CommandContext], CommandResult]:
        if command_type in _HANDLERS:
            raise RuntimeError(f"handler already registered for {command_type.__name__}")
        _HANDLERS[command_type] = fn
        return fn

    return register


def registered_commands() -> dict[str, type[Command]]:
    return {c.__name__: c for c in _HANDLERS}


def execute(command: Command, ctx: CommandContext) -> CommandResult:
    handler = _HANDLERS.get(type(command))
    if handler is None:
        raise CommandError("COMMAND_UNKNOWN", type(command).__name__, status=400)
    if not ctx.idempotency_key:
        raise CommandError(
            "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header required", status=400
        )
    return handler(command, ctx)


def require_permission(ctx: CommandContext, permission: str, **scope: Any) -> None:
    """Deny-by-default: without an authorizer nothing is allowed."""
    if ctx.authorizer is None:
        raise CommandError("POLICY_DENIED", "no authorizer configured", status=403)
    ctx.authorizer.require(
        ctx.session,
        ctx.principal.account_uuid,
        permission,
        correlation_id=ctx.correlation_id,
        **scope,
    )
