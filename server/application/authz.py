"""Bridge between the command bus and the Policy Engine (P1-03).

``BusAuthorizer`` implements ``AuthorizerLike`` by building an ``AuthorizationRequest`` and
translating ``AuthorizationDenied`` into a transport-neutral ``CommandError`` (403 for policy
denials; the information-disclosure normalization to 404 happens at the API layer per §7.5).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from server.application.bus import CommandError
from server.policy.authorization import (
    Authorization,
    AuthorizationDenied,
    AuthorizationRequest,
    Authorizer,
)


class BusAuthorizer:
    def __init__(self, authorizer: Authorizer | None = None) -> None:
        self._authorizer = authorizer or Authorizer()

    @property
    def authorizer(self) -> Authorizer:
        return self._authorizer

    def require(
        self,
        session: Session,
        principal_account_id: str,
        permission: str,
        *,
        action: str | None = None,
        domain: str | None = None,
        channel_id: str | None = None,
        resource: str | None = None,
        side_effect: bool = False,
        capability: str | None = None,
        correlation_id: str = "",
        target_type: str = "action",
        target_id: str = "-",
    ) -> Authorization:
        request = AuthorizationRequest(
            permission=permission,
            action=action,
            domain=domain,
            channel_id=channel_id,
            resource=resource,
            side_effect=side_effect,
            required_capability=capability,
            correlation_id=correlation_id or "-",
            target_type=target_type,
            target_id=target_id,
        )
        try:
            return self._authorizer.require(session, principal_account_id, request)
        except AuthorizationDenied as exc:
            raise CommandError(
                exc.code, f"{permission} denied", status=403, extra={"permission": permission}
            ) from exc


class AllowAllAuthorizer:
    """Test double: allows everything. Never used in production wiring."""

    def require(
        self, session: Session, principal_account_id: str, permission: str, **scope: Any
    ) -> None:
        return None
