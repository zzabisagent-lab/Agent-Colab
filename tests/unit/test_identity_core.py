"""P1-05 unit tests: spoof detection (V-P1-08) and external link lifecycle (V-P1-23), no DB."""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from server.domain.clock import FixedClock
from server.events.store import InMemoryEventStore
from server.identity.external_links import ExternalLinkService, link_id_for
from server.identity.principals import IdentityError, detect_actor_claims
from server.identity.repository import InMemoryIdentityRepository

VECTORS = yaml.safe_load(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "identity" / "spoof-vectors.yaml"
    ).read_text()
)["vectors"]
T0 = dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC)
WS = uuid.uuid4()
ACTOR = uuid.uuid4()


@pytest.mark.parametrize("vec", VECTORS, ids=[v["name"] for v in VECTORS])
def test_spoof_claims_are_detected_but_never_change_identity(vec: dict[str, Any]) -> None:
    assert detect_actor_claims(vec["body"], vec["headers"]) == vec["expect_claims"]


def _service() -> tuple[
    ExternalLinkService, InMemoryIdentityRepository, FixedClock, InMemoryEventStore
]:
    repo = InMemoryIdentityRepository()
    repo.add_instance("mm-team-a", WS)
    repo.add_instance("mm-team-b", WS)
    repo.add_instance("tg-bot-1", WS, provider="telegram")
    for a in ("acct-alice", "acct-bob"):
        repo.add_account(a)
    clock = FixedClock(T0)
    store = InMemoryEventStore(clock=clock)
    return ExternalLinkService(repo, store, None, clock), repo, clock, store


def test_challenge_web_confirm_creates_exactly_one_active_link() -> None:
    svc, repo, _clock, store = _service()
    issued = svc.start_challenge(
        "mm-team-a", "mmuser-1", actor_account_uuid=ACTOR, correlation_id="c1"
    )
    assert issued.link_id == link_id_for("mm-team-a", "mmuser-1") and len(issued.code) == 8
    link = svc.confirm_challenge(
        "mm-team-a",
        "mmuser-1",
        issued.code,
        "acct-alice",
        path="web",
        actor_account_uuid=ACTOR,
        correlation_id="c1",
    )
    assert link.status == "active" and link.verification_method == "signed_challenge"
    principal = svc.resolve_command_principal("mm-team-a", "mmuser-1")
    assert principal.account_id == "acct-alice" and principal.credential_kind == "external_link"
    types = [e["type"] for e in store.stream(str(WS), "external_identity_link", issued.link_id)]
    assert types == ["IDENTITY_LINK_CHALLENGED", "IDENTITY_LINK_VERIFIED"]
    # duplicate: a second challenge/confirm for the same (instance, user) is rejected
    with pytest.raises(IdentityError) as exc:
        svc.start_challenge("mm-team-a", "mmuser-1", actor_account_uuid=ACTOR, correlation_id="c2")
    assert exc.value.code == "EXTERNAL_IDENTITY_DUPLICATE"
    assert sum(1 for x in repo.links.values() if x.status == "active") == 1


def test_command_path_is_pending_admin_until_approved() -> None:
    svc, _, _, _ = _service()
    issued = svc.start_challenge(
        "mm-team-a", "mmuser-2", actor_account_uuid=ACTOR, correlation_id="c"
    )
    link = svc.confirm_challenge(
        "mm-team-a",
        "mmuser-2",
        issued.code,
        "acct-bob",
        path="command",
        actor_account_uuid=ACTOR,
        correlation_id="c",
    )
    assert link.status == "pending_admin" and link.verification_method == "admin_approval"
    with pytest.raises(IdentityError) as exc:
        svc.resolve_command_principal("mm-team-a", "mmuser-2")
    assert exc.value.code == "EXTERNAL_IDENTITY_NOT_ACTIVE"
    approved = svc.approve_pending_link(link.link_id, admin_account_uuid=ACTOR, correlation_id="c")
    assert approved.status == "active"
    assert svc.resolve_command_principal("mm-team-a", "mmuser-2").account_id == "acct-bob"


def test_wrong_expired_reused_codes_and_lockout() -> None:
    svc, _, clock, _ = _service()
    issued = svc.start_challenge(
        "mm-team-a", "mmuser-3", actor_account_uuid=ACTOR, correlation_id="c"
    )
    for i in range(1, 7):  # five failed confirmations, lockout from the sixth (V-P2-27)
        with pytest.raises(IdentityError) as exc:
            svc.confirm_challenge(
                "mm-team-a",
                "mmuser-3",
                "00000000",
                "acct-alice",
                path="web",
                actor_account_uuid=ACTOR,
                correlation_id="c",
            )
        expected = "EXTERNAL_IDENTITY_LOCKED" if i == 6 else "EXTERNAL_IDENTITY_CHALLENGE_INVALID"
        assert exc.value.code == expected, i
    # 6th attempt, even with the right code, is locked for 15 minutes
    with pytest.raises(IdentityError) as exc:
        svc.confirm_challenge(
            "mm-team-a",
            "mmuser-3",
            issued.code,
            "acct-alice",
            path="web",
            actor_account_uuid=ACTOR,
            correlation_id="c",
        )
    assert exc.value.code == "EXTERNAL_IDENTITY_LOCKED"
    with pytest.raises(IdentityError) as exc:
        svc.start_challenge("mm-team-a", "mmuser-3", actor_account_uuid=ACTOR, correlation_id="c")
    assert exc.value.code == "EXTERNAL_IDENTITY_LOCKED"
    clock.advance(dt.timedelta(minutes=15, seconds=1))
    # after the lockout a new challenge works; the old code is now expired (TTL 10 min)
    issued2 = svc.start_challenge(
        "mm-team-a", "mmuser-3", actor_account_uuid=ACTOR, correlation_id="c"
    )
    clock.advance(dt.timedelta(minutes=10, seconds=1))
    with pytest.raises(IdentityError) as exc:
        svc.confirm_challenge(
            "mm-team-a",
            "mmuser-3",
            issued2.code,
            "acct-alice",
            path="web",
            actor_account_uuid=ACTOR,
            correlation_id="c",
        )
    assert exc.value.code == "EXTERNAL_IDENTITY_CHALLENGE_EXPIRED"
    issued3 = svc.start_challenge(
        "mm-team-a", "mmuser-3", actor_account_uuid=ACTOR, correlation_id="c"
    )
    svc.confirm_challenge(
        "mm-team-a",
        "mmuser-3",
        issued3.code,
        "acct-alice",
        path="web",
        actor_account_uuid=ACTOR,
        correlation_id="c",
    )
    # reuse of a consumed code
    svc.revoke_link(issued3.link_id, "TEST", actor_account_uuid=ACTOR, correlation_id="c")
    with pytest.raises(IdentityError) as exc:
        svc.confirm_challenge(
            "mm-team-a",
            "mmuser-3",
            issued3.code,
            "acct-alice",
            path="web",
            actor_account_uuid=ACTOR,
            correlation_id="c",
        )
    assert exc.value.code == "EXTERNAL_IDENTITY_CHALLENGE_USED"


def test_suspend_blocks_commands_immediately_and_revoke_allows_relink() -> None:
    svc, _, _, store = _service()
    issued = svc.start_challenge(
        "mm-team-a", "mmuser-4", actor_account_uuid=ACTOR, correlation_id="c"
    )
    svc.confirm_challenge(
        "mm-team-a",
        "mmuser-4",
        issued.code,
        "acct-alice",
        path="web",
        actor_account_uuid=ACTOR,
        correlation_id="c",
    )
    svc.suspend_link(issued.link_id, "POLICY", actor_account_uuid=ACTOR, correlation_id="c")
    with pytest.raises(IdentityError) as exc:
        svc.resolve_command_principal("mm-team-a", "mmuser-4")
    assert exc.value.code == "EXTERNAL_IDENTITY_NOT_ACTIVE" and exc.value.detail == "suspended"
    with pytest.raises(IdentityError):  # suspended -> suspended is not a transition
        svc.suspend_link(issued.link_id, "POLICY", actor_account_uuid=ACTOR, correlation_id="c")
    svc.revoke_link(issued.link_id, "USER_LEFT", actor_account_uuid=ACTOR, correlation_id="c")
    with pytest.raises(IdentityError):
        svc.revoke_link(issued.link_id, "AGAIN", actor_account_uuid=ACTOR, correlation_id="c")
    issued2 = svc.start_challenge(
        "mm-team-a", "mmuser-4", actor_account_uuid=ACTOR, correlation_id="c"
    )
    link = svc.confirm_challenge(
        "mm-team-a",
        "mmuser-4",
        issued2.code,
        "acct-bob",
        path="web",
        actor_account_uuid=ACTOR,
        correlation_id="c",
    )
    assert link.status == "active" and link.account_id == "acct-bob"
    types = [e["type"] for e in store.stream(str(WS), "external_identity_link", issued.link_id)]
    assert types == [
        "IDENTITY_LINK_CHALLENGED",
        "IDENTITY_LINK_VERIFIED",
        "IDENTITY_LINK_SUSPENDED",
        "IDENTITY_LINK_REVOKED",
        "IDENTITY_LINK_CHALLENGED",
        "IDENTITY_LINK_VERIFIED",
    ]


def test_provider_instance_isolation_and_multi_provider_accounts() -> None:
    svc, _, _, _ = _service()
    for inst in ("mm-team-a", "mm-team-b", "tg-bot-1"):
        issued = svc.start_challenge(inst, "user-9", actor_account_uuid=ACTOR, correlation_id="c")
        svc.confirm_challenge(
            inst,
            "user-9",
            issued.code,
            "acct-alice",
            path="web",
            actor_account_uuid=ACTOR,
            correlation_id="c",
        )
    assert len({link_id_for(i, "user-9") for i in ("mm-team-a", "mm-team-b", "tg-bot-1")}) == 3
    svc.suspend_link(
        link_id_for("mm-team-b", "user-9"), "X", actor_account_uuid=ACTOR, correlation_id="c"
    )
    assert svc.resolve_command_principal("mm-team-a", "user-9").account_id == "acct-alice"
    assert svc.resolve_command_principal("tg-bot-1", "user-9").account_id == "acct-alice"
    with pytest.raises(IdentityError):
        svc.resolve_command_principal("mm-team-b", "user-9")
    with pytest.raises(IdentityError) as exc:
        svc.resolve_command_principal("unknown-instance", "user-9")
    assert exc.value.code == "EXTERNAL_IDENTITY_NOT_ACTIVE"
