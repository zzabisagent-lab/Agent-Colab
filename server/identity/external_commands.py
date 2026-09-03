"""External command principals and administrative link operations (P2-02; spec §9.2, §10.2).

``resolve_external_principal`` is the single rule for every external channel (Mattermost slash
commands, Telegram commands): only an ``active`` ExternalIdentityLink of *that* provider instance
yields an Account principal; pending, pending_admin, suspended, revoked, or missing links yield
``EXTERNAL_IDENTITY_NOT_ACTIVE`` with zero side effects. The same external user id on another
provider instance is a different link.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock
from server.events.postgres_store import PostgresEventStore
from server.events.store import EventStore
from server.identity.external_links import ExternalLinkService, sql_service
from server.identity.principals import IdentityError, Principal


def resolve_external_principal(
    session: Session,
    provider_instance_id: str,
    external_user_id: str,
    *,
    clock: Clock | None = None,
) -> Principal:
    """Account principal for an active link; raises ``IdentityError`` otherwise (read-only)."""
    service = sql_service(session, PostgresEventStore(session), clock or SystemClock())
    return service.resolve_command_principal(provider_instance_id, external_user_id)


def try_resolve_external_principal(
    session: Session,
    provider_instance_id: str,
    external_user_id: str,
    *,
    clock: Clock | None = None,
) -> Principal | None:
    try:
        return resolve_external_principal(
            session, provider_instance_id, external_user_id, clock=clock
        )
    except IdentityError:
        return None


@dataclass(frozen=True)
class LinkView:
    link_id: str
    provider_instance_id: str
    provider: str
    external_user_id: str
    account_id: str
    verification_method: str
    status: str
    verified_at: str | None


def list_links(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    status: str | None = None,
    provider_instance_id: str | None = None,
    limit: int = 100,
) -> list[LinkView]:
    rows = session.execute(
        text(
            "SELECT l.link_id, p.provider_instance_id, p.provider, l.external_user_id, "
            "a.account_id, l.verification_method, l.status, l.verified_at "
            "FROM external_identity_links l "
            "JOIN provider_instances p ON p.id = l.provider_instance_id "
            "JOIN accounts a ON a.id = l.account_id WHERE p.workspace_id = :ws "
            "AND (CAST(:st AS text) IS NULL OR l.status = CAST(:st AS text)) "
            "AND (CAST(:pi AS text) IS NULL OR p.provider_instance_id = CAST(:pi AS text)) "
            "ORDER BY l.created_at, l.link_id LIMIT :lim"
        ),
        {
            "ws": workspace_id,
            "st": status,
            "pi": provider_instance_id,
            "lim": min(max(limit, 1), 100),
        },
    ).all()
    return [
        LinkView(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            str(r[4]),
            str(r[5]),
            str(r[6]),
            r[7].isoformat() if r[7] is not None else None,
        )
        for r in rows
    ]


def admin_service(session: Session, store: EventStore, clock: Clock | None) -> ExternalLinkService:
    return sql_service(session, store, clock or SystemClock())


def admin_transition(
    session: Session,
    store: EventStore,
    clock: Clock | None,
    *,
    kind: str,
    link_id: str,
    admin_account_uuid: uuid.UUID,
    correlation_id: str,
    reason_code: str = "ADMIN",
) -> dict[str, Any]:
    """approve | suspend | revoke by an Administrator; each transition is audited and evented."""
    service = admin_service(session, store, clock)
    if kind == "approve":
        link = service.approve_pending_link(
            link_id, admin_account_uuid=admin_account_uuid, correlation_id=correlation_id
        )
    elif kind == "suspend":
        link = service.suspend_link(
            link_id,
            reason_code,
            actor_account_uuid=admin_account_uuid,
            correlation_id=correlation_id,
        )
    elif kind == "revoke":
        link = service.revoke_link(
            link_id,
            reason_code,
            actor_account_uuid=admin_account_uuid,
            correlation_id=correlation_id,
        )
    else:
        raise IdentityError("EXTERNAL_IDENTITY_TRANSITION_INVALID", kind)
    return {"link_id": link.link_id, "status": link.status, "account_id": link.account_id}
