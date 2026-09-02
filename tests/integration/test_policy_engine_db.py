"""V-P1-07 on the real database: deny + redacted audit, committed role versions, deny precedence."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.policy.authorization import AuthorizationDenied, AuthorizationRequest, Authorizer
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

NOW = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)
WS = uuid.uuid4()
HUMAN = uuid.uuid4()
AGENT_ACCT = uuid.uuid4()
CHAN = uuid.uuid4()


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    repo = PostgresPolicyRepository()
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-pol', 'pol')"),
            {"i": WS},
        )
        for acc, name, typ in (
            (HUMAN, "acct-pol-human", "human"),
            (AGENT_ACCT, "acct-pol-agent", "agent"),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, "
                    "account_type, display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
        s.execute(
            text(
                "INSERT INTO agents (id, agent_id, workspace_id, account_id, "
                "adapter_type, status, display_name) VALUES (:i, 'agent-pol', :w, "
                ":a, 'mcp', 'active', 'Pol')"
            ),
            {"i": uuid.uuid4(), "w": WS, "a": AGENT_ACCT},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, "
                "display_name) VALUES (:i, 'chan-pol', :w, 'work', 'pol')"
            ),
            {"i": CHAN, "w": WS},
        )
        s.execute(
            text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
            {"c": CHAN, "a": AGENT_ACCT},
        )
        s.execute(
            text(
                "INSERT INTO capabilities (id, capability_id, tool) VALUES (:i, "
                "'cap-progress', 'task_progress')"
            ),
            {"i": uuid.uuid4()},
        )
        repo.create_role(s, WS, "role-worker", "Worker")
        repo.commit_role_version(
            s,
            "role-worker",
            ["task.create", "task.read", "task.progress"],
            [],
            {"max_risk": "MEDIUM"},
            HUMAN,
        )
        repo.create_role(s, WS, "role-denier", "Denier")
        repo.commit_role_version(s, "role-denier", [], ["task.progress"], {}, HUMAN)
        repo.assign_role(s, AGENT_ACCT, "role-worker", HUMAN, NOW - dt.timedelta(days=1))
    yield eng
    eng.dispose()


def _authorizer() -> Authorizer:
    return Authorizer(PostgresPolicyRepository(), clock=FixedClock(NOW))


def test_deny_without_scope_or_capability_is_audited_redacted(engine: Engine) -> None:
    az = _authorizer()
    with Session(engine) as s, s.begin():
        # no permission at all
        r1 = az.authorize(
            s,
            "acct-pol-agent",
            AuthorizationRequest(
                "secret.grant",
                "tool:secret_grant_create",
                correlation_id="corr-1",
                target_id="sec-1",
            ),
        )
        assert not r1.allowed and r1.code == "DEFAULT_DENY" and r1.audit_id
        # permission but capability missing
        r2 = az.authorize(
            s,
            "acct-pol-agent",
            AuthorizationRequest(
                "task.progress",
                "tool:task_progress",
                required_capability="cap-progress",
                correlation_id="corr-2",
            ),
        )
        assert not r2.allowed and r2.code == "CAPABILITY_MISSING"
        # permission but outside channel membership
        r3 = az.authorize(
            s,
            "acct-pol-agent",
            AuthorizationRequest(
                "task.create", "tool:task_create", channel_id="chan-other", correlation_id="corr-3"
            ),
        )
        assert not r3.allowed and r3.code == "CHANNEL_NOT_MEMBER"
        rows = s.execute(
            text(
                "SELECT error_code, redacted_metadata, actor_account_id, result FROM "
                "audit_events WHERE action = 'policy.deny' AND correlation_id IN "
                "('corr-1','corr-2','corr-3') ORDER BY id"
            )
        ).all()
    assert [r[0] for r in rows] == ["DEFAULT_DENY", "CAPABILITY_MISSING", "CHANNEL_NOT_MEMBER"]
    for r in rows:
        assert r[3] == "DENY" and uuid.UUID(str(r[2])) == AGENT_ACCT
        meta = r[1]
        assert set(meta) <= {
            "permission",
            "action",
            "reason",
            "roles",
            "channel_id",
            "domain",
            "required_capability",
        }
        assert "token" not in str(meta).lower()


def test_allow_when_capability_and_membership_present(engine: Engine) -> None:
    az = _authorizer()
    with Session(engine) as s, s.begin():
        PostgresPolicyRepository().grant_capability(s, "agent-pol", "cap-progress")
        r = az.authorize(
            s,
            "acct-pol-agent",
            AuthorizationRequest(
                "task.progress",
                "tool:task_progress",
                channel_id="chan-pol",
                required_capability="cap-progress",
            ),
        )
        assert r.allowed and r.risk == "LOW" and r.snapshot.role_versions == (("role-worker", 1),)
        assert r.snapshot.capability_ids == ("cap-progress",)
        before = s.execute(
            text("SELECT count(*) FROM audit_events WHERE action = 'policy.deny'")
        ).scalar()
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT count(*) FROM audit_events WHERE action = 'policy.deny'")
            ).scalar()
            == before
        )


def test_committed_role_version_wins_immediately_and_deny_precedence(engine: Engine) -> None:
    az = _authorizer()
    repo = PostgresPolicyRepository()
    with Session(engine) as s, s.begin():
        assert az.authorize(
            s, "acct-pol-agent", AuthorizationRequest("task.create", "tool:task_create")
        ).allowed
        version, digest = repo.commit_role_version(
            s, "role-worker", ["task.read", "task.progress"], [], {"max_risk": "MEDIUM"}, HUMAN
        )
        assert version == 2 and len(digest) == 64
        # first authorization after the commit already sees version 2: zero stale allows
        r = az.authorize(
            s, "acct-pol-agent", AuthorizationRequest("task.create", "tool:task_create")
        )
        assert (
            not r.allowed
            and r.code == "DEFAULT_DENY"
            and r.snapshot.role_versions == (("role-worker", 2),)
        )
        # allow + deny roles together: explicit deny wins
        repo.assign_role(s, AGENT_ACCT, "role-denier", HUMAN, NOW - dt.timedelta(hours=1))
        r2 = az.authorize(
            s,
            "acct-pol-agent",
            AuthorizationRequest("task.progress", "tool:task_progress", channel_id="chan-pol"),
        )
        assert (
            not r2.allowed and r2.code == "EXPLICIT_DENY" and r2.matched_roles == ("role-denier",)
        )
        # revoke the deny role: allow returns; revoke everything: default deny
        assert repo.revoke_role(s, AGENT_ACCT, "role-denier", NOW) == 1
        assert az.authorize(
            s,
            "acct-pol-agent",
            AuthorizationRequest("task.progress", "tool:task_progress", channel_id="chan-pol"),
        ).allowed
        assert repo.revoke_role(s, AGENT_ACCT, "role-worker", NOW) == 1
        with pytest.raises(AuthorizationDenied) as exc:
            az.require(s, "acct-pol-agent", AuthorizationRequest("task.read", "tool:task_get"))
        assert exc.value.code == "DEFAULT_DENY"
        versions = s.execute(
            text(
                "SELECT version, policy_hash FROM role_versions WHERE role_id = "
                "'role-worker' ORDER BY version"
            )
        ).all()
    assert [v[0] for v in versions] == [1, 2] and versions[0][1] != versions[1][1]


def test_role_versions_are_immutable(engine: Engine) -> None:
    with pytest.raises(Exception, match="IMMUTABLE_ROW"), engine.begin() as c:
        c.execute(
            text(
                "UPDATE role_versions SET permissions = '[]'::jsonb WHERE role_id = "
                "'role-worker' AND version = 1"
            )
        )
