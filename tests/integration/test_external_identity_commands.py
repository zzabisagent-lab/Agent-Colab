"""V-P2-20/21/22 (external identity command principals per provider instance) and V-P2-27 (the
Mattermost link challenge through the Router with the fake client)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.api.errors import ApiError
from server.application import channels as ch
from server.application import tasks as t
from server.application.authz import BusAuthorizer
from server.channels.mattermost import provider as prov
from server.channels.mattermost.client import FakeMattermostClient
from server.channels.router import LINK_HANDLERS, Router, SlashRequest
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.identity import external_commands as ext
from server.identity import mattermost_link
from server.identity.external_links import sql_service
from server.identity.principals import IdentityError, Principal
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ADMIN, ACTIVE_ACC, OTHER_ACC, SYSTEM = (uuid.uuid4() for _ in range(4))
CLOCK = FixedClock(dt.datetime(2026, 8, 2, tzinfo=dt.UTC))
FAKE = FakeMattermostClient(users={"mm-u-alice": {"username": "alice"}})
CRITERIA = ({"statement": "done", "check_type": "evidence", "required": True},)
TG_PI = "tg:1234"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    prov.set_client_factory(lambda inst: FAKE)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ext', 'ext')"),
            {"i": WS},
        )
        repo = PostgresPolicyRepository()
        for acc, name, typ, subj, perms in (
            (
                ADMIN,
                "acct-ext-admin",
                "human",
                None,
                ["channel.manage", "admin.accounts", "task.*"],
            ),
            (ACTIVE_ACC, "acct-alice", "human", "mattermost:alice", ["task.create", "task.read"]),
            (OTHER_ACC, "acct-bob", "human", None, ["task.read"]),
            (SYSTEM, "acct-system", "service", None, []),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name, auth_subject) VALUES (:i, :a, :w, :t, :a, :s)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ, "s": subj},
            )
            if perms:
                repo.create_role(s, WS, f"ext-{name}", name)
                repo.commit_role_version(s, f"ext-{name}", perms, [], {}, ADMIN)
                repo.assign_role(s, acc, f"ext-{name}", ADMIN, CLOCK.now())
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "team_or_bot_ref) VALUES (:i, :p, :w, 'telegram', 'bot-1234')"
            ),
            {"i": uuid.uuid4(), "p": TG_PI, "w": WS},
        )
    mattermost_link.register()
    yield eng
    mattermost_link.unregister()
    prov.set_client_factory(None)
    eng.dispose()


ADMIN_P = Principal("acct-ext-admin", str(ADMIN), "human", "sha256:acct-ext-admin")


def _rt(engine: Engine) -> Runtime:
    return Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK, str(WS))


def _mm_instance(rt: Runtime) -> str:
    """The module's Mattermost instance (idempotent replay returns the same id)."""
    return str(
        execute_command(
            rt,
            ADMIN_P,
            ch.RegisterProviderInstance("http://mm.ext", "ext-team", "team-ext", "bot"),
            idempotency_key="ext-inst",
            correlation_id="ext",
        ).resource_id
    )


def _events(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": WS}
            ).scalar_one()
        )


def _link(engine: Engine, pi: str, external: str, account: uuid.UUID, status: str) -> str:
    link_id = f"link-{pi}-{external}-{status}".replace(":", "-")
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                "external_user_id, account_id, verification_method, status, verified_at) "
                "SELECT :i, :l, p.id, :e, :a, 'admin_approval', :st, now() "
                "FROM provider_instances p "
                "WHERE p.provider_instance_id = :pi AND NOT EXISTS "
                "(SELECT 1 FROM external_identity_links x WHERE x.link_id = :l)"
            ),
            {"i": uuid.uuid4(), "l": link_id, "e": external, "a": account, "st": status, "pi": pi},
        )
    return link_id


@pytest.fixture(scope="module")
def seeded(engine: Engine) -> str:
    """Instance, channel, links and memberships shared by the tests (idempotent)."""
    rt = _rt(engine)
    mm_inst = _mm_instance(rt)
    chan = execute_command(
        rt,
        ADMIN_P,
        ch.ImportChannel(mm_inst, "mm-ext-a", "work", display_name="A"),
        idempotency_key="ext-import",
        correlation_id="ext",
    ).resource_id
    with Session(engine) as s:
        chan_uuid = str(
            s.execute(
                text("SELECT id FROM channels WHERE channel_id = :c"), {"c": chan}
            ).scalar_one()
        )
    _link(engine, TG_PI, "tg-active", ACTIVE_ACC, "active")
    _link(engine, TG_PI, "tg-suspended", OTHER_ACC, "suspended")
    with Session(engine) as s, s.begin():  # channel membership for the linked Accounts
        for acc in (ACTIVE_ACC, OTHER_ACC):
            s.execute(
                text(
                    "INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"c": uuid.UUID(chan_uuid), "a": acc},
            )
    return chan_uuid


def test_only_active_links_execute_commands(engine: Engine, seeded: str) -> None:  # V-P2-20
    rt = _rt(engine)
    chan_uuid = seeded
    baseline = _events(engine)
    # the same Task command by three Telegram users: only the active link executes
    with Session(engine) as s:
        principal = ext.resolve_external_principal(s, TG_PI, "tg-active", clock=CLOCK)
        assert principal.account_id == "acct-alice" and principal.credential_kind == "external_link"
        for user, expected in (("tg-unlinked", "no active link"), ("tg-suspended", "suspended")):
            with pytest.raises(IdentityError) as exc:
                ext.resolve_external_principal(s, TG_PI, user, clock=CLOCK)
            assert exc.value.code == "EXTERNAL_IDENTITY_NOT_ACTIVE" and expected in exc.value.detail
            assert ext.try_resolve_external_principal(s, TG_PI, user, clock=CLOCK) is None
    assert _events(engine) == baseline  # resolution has zero side effects
    created = execute_command(
        rt,
        principal,
        t.CreateTask("From Telegram", chan_uuid, "research", "LOW", criteria=CRITERIA),
        idempotency_key="ext-task-1",
        correlation_id="ext",
    )
    assert created.resource_id.startswith("task-") and _events(engine) == baseline + 1
    # the active user's Account permissions apply (no task.delegate)
    with pytest.raises(ApiError) as denied:
        execute_command(
            rt,
            principal,
            t.DelegateTask(created.resource_id, "acct-bob"),
            idempotency_key="ext-task-2",
            correlation_id="ext",
        )
    assert denied.value.status == 404 and _events(engine) == baseline + 1


def test_second_link_for_the_same_provider_user_is_rejected_and_audited(
    engine: Engine, seeded: str
) -> None:  # V-P2-21
    with Session(engine) as s, s.begin():
        svc = sql_service(s, PostgresEventStore(s, clock=CLOCK), CLOCK)
        with pytest.raises(IdentityError) as exc:
            svc.start_challenge(TG_PI, "tg-active", actor_account_uuid=SYSTEM, correlation_id="dup")
        assert exc.value.code == "EXTERNAL_IDENTITY_DUPLICATE"
    with Session(engine) as s:
        audit = s.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE action = 'identity.link_challenge' "
                "AND result = 'DENY' AND error_code = 'EXTERNAL_IDENTITY_DUPLICATE'"
            )
        ).scalar_one()
        links = s.execute(
            text(
                "SELECT a.account_id, l.status FROM external_identity_links l "
                "JOIN provider_instances p ON p.id = l.provider_instance_id "
                "JOIN accounts a ON a.id = l.account_id WHERE p.provider_instance_id = :pi "
                "AND l.external_user_id = 'tg-active'"
            ),
            {"pi": TG_PI},
        ).all()
        # the DB itself refuses a second row for the same (instance, user)
        with pytest.raises(Exception, match=r"unique|duplicate"), s.begin_nested():
            s.execute(
                text(
                    "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                    "external_user_id, account_id, verification_method, status) SELECT :i, "
                    "'link-dup', p.id, 'tg-active', :a, 'admin_approval', 'active' "
                    "FROM provider_instances p WHERE p.provider_instance_id = :pi"
                ),
                {"i": uuid.uuid4(), "a": OTHER_ACC, "pi": TG_PI},
            )
    assert audit >= 1
    assert [tuple(r) for r in links] == [("acct-alice", "active")]  # existing link unchanged


def test_links_are_per_provider_instance(engine: Engine, seeded: str) -> None:  # V-P2-22
    rt = _rt(engine)
    other_pi = "tg:5678"
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "team_or_bot_ref) VALUES (:i, :p, :w, 'telegram', 'bot-5678') "
                "ON CONFLICT DO NOTHING"
            ),
            {"i": uuid.uuid4(), "p": other_pi, "w": WS},
        )
    # the same external user id is linked to a different Account on the other instance
    _link(engine, other_pi, "tg-active", OTHER_ACC, "active")
    with Session(engine) as s:
        first = ext.resolve_external_principal(s, TG_PI, "tg-active", clock=CLOCK)
        second = ext.resolve_external_principal(s, other_pi, "tg-active", clock=CLOCK)
        assert (first.account_id, second.account_id) == ("acct-alice", "acct-bob")
        assert first.credential_fingerprint != second.credential_fingerprint
        with pytest.raises(IdentityError):  # a user linked on instance 1 only is unknown on 2
            ext.resolve_external_principal(s, other_pi, "tg-suspended", clock=CLOCK)
    # acct-bob (instance 2) has no task.create: zero cross-Account permission use
    chan_uuid = seeded
    before = _events(engine)
    with pytest.raises(ApiError) as exc:
        execute_command(
            rt,
            second,
            t.CreateTask("Cross", chan_uuid, "research", "LOW", criteria=CRITERIA),
            idempotency_key="ext-cross",
            correlation_id="ext",
        )
    assert exc.value.status == 404 and _events(engine) == before


def test_link_challenge_through_the_router(engine: Engine, seeded: str) -> None:  # V-P2-27
    rt = _rt(engine)
    assert set(LINK_HANDLERS) >= {"start", "confirm"}
    pi = _mm_instance(rt)
    router = Router(rt, CLOCK)

    def req(text_in: str, n: int) -> SlashRequest:
        return SlashRequest(
            pi,
            "team-ext",
            "mm-ext-a",
            "mm-u-alice",
            "alice",
            "/colab",
            text_in,
            trigger_id=f"trig-{n}",
        )

    # unlinked user: only link/help are allowed
    denied = router.route(req('task create "x" --criteria "c"', 1))
    assert denied.response_type == "ephemeral" and denied.code == "COMMAND_UNLINKED_RESTRICTED"
    started = router.route(req("link start", 2))
    assert started.code == "OK" and started.response_type == "ephemeral"
    assert FAKE.dms and FAKE.dms[-1][0] == "mm-u-alice"  # the code went by DM, not to the channel
    code = next(tok for tok in FAKE.dms[-1][1].split() if tok.isdigit() and len(tok) == 8)
    assert not any(
        code in p.message for p in FAKE.posts.values() if not p.channel_id.startswith("dm-")
    )
    wrong = router.route(
        req("link confirm 00000000" if code != "00000000" else "link confirm 11111111", 3)
    )
    assert wrong.code == "EXTERNAL_IDENTITY_CHALLENGE_INVALID"
    confirmed = router.route(req(f"link confirm {code}", 4))
    assert confirmed.code == "OK" and "pending" in confirmed.text.lower()
    reused = router.route(req(f"link confirm {code}", 5))
    assert reused.code in ("EXTERNAL_IDENTITY_CHALLENGE_USED", "EXTERNAL_IDENTITY_DUPLICATE")
    with Session(engine) as s:
        status = s.execute(
            text(
                "SELECT status, verification_method FROM external_identity_links WHERE "
                "external_user_id = 'mm-u-alice'"
            )
        ).one()
        link_id = s.execute(
            text(
                "SELECT link_id FROM external_identity_links WHERE external_user_id = 'mm-u-alice'"
            )
        ).scalar_one()
    assert tuple(status) == ("pending_admin", "admin_approval")
    still = router.route(req('task create "x" --criteria "c"', 6))
    assert still.code == "COMMAND_UNLINKED_RESTRICTED"  # pending_admin executes nothing
    # administrator approval activates the link; the command now executes with Account permissions
    with Session(engine) as s, s.begin():
        ext.admin_transition(
            s,
            PostgresEventStore(s, clock=CLOCK),
            CLOCK,
            kind="approve",
            link_id=str(link_id),
            admin_account_uuid=ADMIN,
            correlation_id="approve",
        )
    ok = router.route(req('task create "Linked task" --criteria "c"', 7))
    assert ok.code == "OK" and ok.resource_id and ok.resource_id.startswith("task-")
    # lockout: 5 wrong codes on a fresh challenge for another user block the 6th attempt
    FAKE.users["mm-u-carol"] = {"username": "carol"}
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name, "
                "auth_subject) VALUES (:i, 'acct-carol', :w, 'human', 'carol', 'mattermost:carol')"
            ),
            {"i": uuid.uuid4(), "w": WS},
        )

    def creq(text_in: str, n: int) -> SlashRequest:
        return SlashRequest(
            pi,
            "team-ext",
            "mm-ext-a",
            "mm-u-carol",
            "carol",
            "/colab",
            text_in,
            trigger_id=f"ctrig-{n}",
        )

    assert router.route(creq("link start", 1)).code == "OK"
    good = next(tok for tok in FAKE.dms[-1][1].split() if tok.isdigit() and len(tok) == 8)
    bad = "00000000" if good != "00000000" else "11111111"
    codes = [router.route(creq(f"link confirm {bad}", 2 + i)).code for i in range(6)]
    assert codes[:5] == ["EXTERNAL_IDENTITY_CHALLENGE_INVALID"] * 5
    assert codes[5] == "EXTERNAL_IDENTITY_LOCKED"  # lockout from the sixth failure
    assert router.route(creq(f"link confirm {good}", 9)).code == "EXTERNAL_IDENTITY_LOCKED"
    CLOCK.advance(dt.timedelta(minutes=15, seconds=1))
    assert router.route(creq("link start", 10)).code == "OK"
