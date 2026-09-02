"""Transport-neutral command dispatch (development plan §7.5).

REST routes, MCP tools, and Mattermost commands build a ``CommandContext`` here and execute the
same handler through the command bus inside one database transaction. Every write carries an
Idempotency-Key, an optional ``If-Match``/expected aggregate sequence, and a correlation ID; the
response returns the resource ID, Event ID, and aggregate sequence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from server.api.errors import ApiError
from server.application import bus
from server.application.authz import BusAuthorizer
from server.db.engine import session_scope
from server.domain.clock import Clock, SystemClock
from server.events.postgres_store import PostgresEventStore
from server.events.store import EventStore, EventStoreError
from server.identity.principals import Principal as CredentialPrincipal
from server.secrets.envelope import EnvelopeCrypto


@dataclass
class Runtime:
    """Application-wide collaborators stored on ``app.state.runtime``."""

    session_factory: Any
    authorizer: bus.AuthorizerLike | None
    crypto: EnvelopeCrypto | None
    clock: Clock
    workspace_id: str | None = None  # single-Workspace instance (development plan §7H)

    def store_for(self, session: Session) -> EventStore:
        return PostgresEventStore(session, crypto=self.crypto, clock=self.clock)

    def resolve_workspace(self, session: Session, account_uuid: str | None = None) -> str:
        """The principal's Workspace (single-Workspace instance: every Account belongs to it)."""
        from sqlalchemy import text

        if account_uuid is not None:
            row = session.execute(
                text("SELECT workspace_id FROM accounts WHERE id = :a"), {"a": account_uuid}
            ).first()
            if row is not None:
                return str(row[0])
        if self.workspace_id is None:
            row = session.execute(
                text("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")
            ).first()
            if row is None:
                raise ApiError(503, "WORKSPACE_NOT_CONFIGURED", "run Setup first")
            self.workspace_id = str(row[0])
        return self.workspace_id


def to_bus_principal(p: CredentialPrincipal) -> bus.Principal:
    return bus.Principal(
        account_id=p.account_id,
        account_uuid=p.account_uuid,
        account_type=p.account_type,
        credential_fingerprint=p.credential_fingerprint,
        agent_id=None,
        mfa_verified=p.mfa_verified,
    )


def parse_if_match(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    v = value.strip().strip('"')
    if v.startswith("seq:"):
        v = v[4:]
    try:
        return int(v)
    except ValueError as exc:
        raise ApiError(
            428, "IF_MATCH_INVALID", "If-Match must be the expected aggregate seq"
        ) from exc


def command_error_to_api(exc: bus.CommandError) -> ApiError:
    status = exc.status
    # information-disclosure policy (§7.5): forbidden and not-found look the same to the caller
    if status == 403 or exc.code in {"NOT_FOUND", "TASK_NOT_FOUND"}:
        status = 404
    return ApiError(status, exc.code, exc.detail, exc.extra)


def execute_command(
    runtime: Runtime,
    principal: CredentialPrincipal,
    command: bus.Command,
    *,
    idempotency_key: str,
    correlation_id: str,
    expected_seq: int | None = None,
    extras: dict[str, Any] | None = None,
) -> bus.CommandResult:
    with session_scope(runtime.session_factory) as session:
        ctx = bus.CommandContext(
            session=session,
            store=runtime.store_for(session),
            authorizer=runtime.authorizer,
            clock=runtime.clock,
            principal=to_bus_principal(principal),
            workspace_id=runtime.resolve_workspace(session, principal.account_uuid),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            expected_seq=expected_seq,
            extras=extras or {},
        )
        try:
            result = bus.execute(command, ctx)
        except bus.CommandError as exc:
            raise command_error_to_api(exc) from exc
        except EventStoreError as exc:
            raise ApiError(409, exc.code, exc.detail) from exc
        if not result.replayed and result.event_id:
            # Renderer (P2-11): card patch + thread reply enqueued in the same transaction
            from server.channels.task_cards import after_command

            after_command(
                session,
                workspace_id=ctx.workspace_id,
                actor_uuid=ctx.principal.account_uuid,
                event_id=result.event_id,
                now=runtime.clock.now(),
            )
        return result


def dispatch(
    request: Request, principal: CredentialPrincipal, command: bus.Command, **extras: Any
) -> dict[str, Any]:
    """REST helper: headers → context → execute → response body."""
    runtime: Runtime = request.app.state.runtime
    idem = request.headers.get("Idempotency-Key")
    if not idem:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header required")
    result = execute_command(
        runtime,
        principal,
        command,
        idempotency_key=idem,
        correlation_id=request.headers.get("X-Correlation-ID") or f"corr-{uuid.uuid4().hex[:16]}",
        expected_seq=parse_if_match(request.headers.get("If-Match")),
        extras=extras,
    )
    return {
        "resource_id": result.resource_id,
        "event_id": result.event_id,
        "aggregate_type": result.aggregate_type,
        "aggregate_seq": result.aggregate_seq,
        "replayed": result.replayed,
        **result.data,
    }


def default_runtime(
    session_factory: Any, settings: Any, authorizer: bus.AuthorizerLike | None = None
) -> Runtime:
    from server.secrets.envelope import MasterKey

    crypto = None
    if getattr(settings, "master_key_b64", None):
        crypto = EnvelopeCrypto(MasterKey.from_b64(settings.master_key_id, settings.master_key_b64))
    return Runtime(
        session_factory=session_factory,
        authorizer=authorizer if authorizer is not None else BusAuthorizer(),
        crypto=crypto,
        clock=SystemClock(),
    )
