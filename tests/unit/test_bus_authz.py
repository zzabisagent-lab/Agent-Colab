from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from server.application import bus
from server.application.authz import AllowAllAuthorizer, BusAuthorizer
from server.domain.clock import FixedClock
from server.events.store import InMemoryEventStore
from server.policy.authorization import Authorization, AuthorizationDenied, Authorizer
from server.policy.repository import PolicySnapshot

_DENIED = Authorization(
    allowed=False,
    code="DEFAULT_DENY",
    risk="LOW",
    action_class="read_query",
    approval="none",
    approval_required=False,
    human_only=False,
    matched_roles=(),
    snapshot=PolicySnapshot("acct-x", (), (), "0" * 64, "2026-01-01T00:00:00Z"),
)


class _DenyingAuthorizer(Authorizer):
    def require(self, session: Any, principal_account_id: str, request: Any) -> Authorization:
        raise AuthorizationDenied(_DENIED)


def _ctx(authorizer: Any) -> bus.CommandContext:
    return bus.CommandContext(
        session=None,  # type: ignore[arg-type]
        store=InMemoryEventStore(),
        authorizer=authorizer,
        clock=FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)),
        principal=bus.Principal("acct-x", "00000000-0000-4000-8000-000000000000", "human", "fp"),
        workspace_id="ws",
        correlation_id="corr",
        idempotency_key="k",
    )


def test_denial_becomes_command_error_403() -> None:
    with pytest.raises(bus.CommandError) as exc:
        bus.require_permission(_ctx(BusAuthorizer(_DenyingAuthorizer())), "task.create")
    assert exc.value.code == "DEFAULT_DENY" and exc.value.status == 403


def test_missing_authorizer_denies_by_default() -> None:
    with pytest.raises(bus.CommandError) as exc:
        bus.require_permission(_ctx(None), "task.create")
    assert exc.value.code == "POLICY_DENIED"


def test_allow_all_double_passes() -> None:
    bus.require_permission(_ctx(AllowAllAuthorizer()), "task.create", channel_id="c")


def test_unknown_command_and_missing_idempotency_key() -> None:
    class Nope(bus.Command):
        pass

    with pytest.raises(bus.CommandError) as exc:
        bus.execute(Nope(), _ctx(AllowAllAuthorizer()))
    assert exc.value.code == "COMMAND_UNKNOWN"
