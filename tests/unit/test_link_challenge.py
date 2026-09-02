"""P2-13 link challenge lifecycle (V-P2-27) on the in-memory identity repository with a fixed clock:
valid, wrong, expired, reused codes; 5 failures lock for 15 minutes (the 6th attempt is blocked);
the command path lands in pending_admin until an Administrator approves it."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from server.domain.clock import FixedClock
from server.events.store import InMemoryEventStore
from server.identity.external_links import ExternalLinkService
from server.identity.principals import IdentityError
from server.identity.repository import InMemoryIdentityRepository

WS = uuid.uuid4()
PI = "mm:test:link"
USER = "mm-user-1"


def _service(clock: FixedClock) -> tuple[ExternalLinkService, uuid.UUID, uuid.UUID]:
    repo = InMemoryIdentityRepository()
    repo.add_instance(PI, WS)
    system = repo.add_account("acct-system")
    account = repo.add_account("acct-alice")
    return ExternalLinkService(repo, InMemoryEventStore(clock), None, clock), system, account


def _start(svc: ExternalLinkService, system: uuid.UUID) -> str:
    return svc.start_challenge(PI, USER, actor_account_uuid=system, correlation_id="c").code


def _confirm(svc: ExternalLinkService, system: uuid.UUID, code: str, path: str = "command"):  # type: ignore[no-untyped-def]
    return svc.confirm_challenge(
        PI, USER, code, "acct-alice", path=path, actor_account_uuid=system, correlation_id="c"
    )


def test_valid_code_command_path_is_pending_admin_then_active_after_approval() -> None:
    clock = FixedClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))
    svc, system, _ = _service(clock)
    code = _start(svc, system)
    assert len(code) == 8 and code.isdigit()
    link = _confirm(svc, system, code)
    assert link.status == "pending_admin" and link.verification_method == "admin_approval"
    with pytest.raises(IdentityError) as blocked:
        svc.resolve_command_principal(PI, USER)
    assert blocked.value.code == "EXTERNAL_IDENTITY_NOT_ACTIVE"
    approved = svc.approve_pending_link(link.link_id, admin_account_uuid=system, correlation_id="c")
    assert approved.status == "active"
    assert svc.resolve_command_principal(PI, USER).account_id == "acct-alice"
    events = [e["type"] for e in svc.store.events]  # type: ignore[attr-defined]
    assert events == ["IDENTITY_LINK_CHALLENGED", "IDENTITY_LINK_VERIFIED"]


def test_web_path_is_active_with_signed_challenge() -> None:
    clock = FixedClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))
    svc, system, _ = _service(clock)
    link = _confirm(svc, system, _start(svc, system), path="web")
    assert link.status == "active" and link.verification_method == "signed_challenge"


def test_wrong_expired_and_reused_codes_are_rejected() -> None:
    clock = FixedClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))
    svc, system, _ = _service(clock)
    code = _start(svc, system)
    with pytest.raises(IdentityError) as wrong:
        _confirm(svc, system, "00000000" if code != "00000000" else "11111111")
    assert wrong.value.code == "EXTERNAL_IDENTITY_CHALLENGE_INVALID"
    clock.advance(dt.timedelta(minutes=10, seconds=1))
    with pytest.raises(IdentityError) as expired:
        _confirm(svc, system, code)
    assert expired.value.code == "EXTERNAL_IDENTITY_CHALLENGE_EXPIRED"
    fresh = _start(svc, system)
    assert _confirm(svc, system, fresh).status == "pending_admin"
    with pytest.raises(IdentityError) as reused:
        _confirm(svc, system, fresh)
    assert reused.value.code in ("EXTERNAL_IDENTITY_CHALLENGE_USED", "EXTERNAL_IDENTITY_DUPLICATE")


def test_five_failures_stay_invalid_and_the_sixth_locks_for_fifteen_minutes() -> None:
    clock = FixedClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))
    svc, system, _ = _service(clock)
    code = _start(svc, system)
    bad = "00000000" if code != "00000000" else "11111111"
    codes: list[str] = []
    for _ in range(6):
        with pytest.raises(IdentityError) as exc:
            _confirm(svc, system, bad)
        codes.append(exc.value.code)
    assert codes[:5] == ["EXTERNAL_IDENTITY_CHALLENGE_INVALID"] * 5  # five failed confirmations
    assert codes[5] == "EXTERNAL_IDENTITY_LOCKED"  # lockout starts with the sixth failure
    with pytest.raises(IdentityError) as sixth:  # even the right code is blocked while locked
        _confirm(svc, system, code)
    assert sixth.value.code == "EXTERNAL_IDENTITY_LOCKED"
    with pytest.raises(IdentityError) as restart:
        _start(svc, system)
    assert restart.value.code == "EXTERNAL_IDENTITY_LOCKED"
    clock.advance(dt.timedelta(minutes=15, seconds=1))
    new_code = _start(svc, system)  # lockout over: a new challenge can be issued
    assert _confirm(svc, system, new_code).status == "pending_admin"
