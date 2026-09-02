"""P2-02 / P2-09: per-channel membership and configuration independence (V-P2-19) and channel soft
delete after archive with references intact (V-P2-18)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application import channel_members as cm
from server.application import channels as ch
from server.application import tasks as t
from server.application.authz import BusAuthorizer
from server.channels import lifecycle
from server.channels.mattermost import provider as prov
from server.channels.mattermost.client import FakeMattermostClient
from server.config import Settings
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import Principal, token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ADMIN, MEMBER, AGENT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
TOK_ADMIN, TOK_MEMBER = "svc-cc-admin", "svc-cc-member"
CLOCK = FixedClock(dt.datetime(2026, 7, 1, tzinfo=dt.UTC))
FAKE = FakeMattermostClient(users={"mm-u-admin": {"username": "admin"}})
CRITERIA = ({"statement": "done", "check_type": "evidence", "required": True},)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    prov.set_client_factory(lambda inst: FAKE)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-cc', 'cc')"),
            {"i": WS},
        )
        repo = PostgresPolicyRepository()
        for acc, name, typ, tok, perms in (
            (ADMIN, "acct-cc-admin", "human", TOK_ADMIN, ["channel.manage", "task.*"]),
            (MEMBER, "acct-cc-member", "human", TOK_MEMBER, ["task.read"]),
            (AGENT, "acct-cc-agent", "agent", None, ["task.read"]),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
            if tok:
                s.execute(
                    text(
                        "INSERT INTO service_credentials (id, account_id, fingerprint, "
                        "token_hash) VALUES (:i, :a, :f, :h)"
                    ),
                    {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{name}", "h": token_hash(tok)},
                )
            repo.create_role(s, WS, f"cc-{name}", name)
            repo.commit_role_version(s, f"cc-{name}", perms, [], {}, ADMIN)
            repo.assign_role(s, acc, f"cc-{name}", ADMIN, CLOCK.now())
    yield eng
    prov.set_client_factory(None)
    eng.dispose()


def _rt(engine: Engine) -> Runtime:
    return Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK, str(WS))


ADMIN_P = Principal("acct-cc-admin", str(ADMIN), "human", "sha256:acct-cc-admin")
MEMBER_P = Principal("acct-cc-member", str(MEMBER), "human", "sha256:acct-cc-member")


def _setup_channels(engine: Engine) -> tuple[str, str, str]:
    rt = _rt(engine)
    inst = execute_command(
        rt,
        ADMIN_P,
        ch.RegisterProviderInstance("http://mm.cc", "cc-team", "team-cc", "bot-cc"),
        idempotency_key="cc-inst",
        correlation_id="cc",
    ).resource_id
    a = execute_command(
        rt,
        ADMIN_P,
        ch.ImportChannel(inst, "mm-cc-a", "work", display_name="A"),
        idempotency_key="cc-import-a",
        correlation_id="cc",
    ).resource_id
    b = execute_command(
        rt,
        ADMIN_P,
        ch.ImportChannel(inst, "mm-cc-b", "brainstorm", display_name="B"),
        idempotency_key="cc-import-b",
        correlation_id="cc",
    ).resource_id
    return inst, a, b


def _members(engine: Engine, channel_id: str) -> dict[str, list[str]]:
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT a.account_id, m.permissions FROM channel_members m "
                "JOIN accounts a ON a.id = m.account_id JOIN channels c ON c.id = m.channel_id "
                "WHERE c.channel_id = :c AND m.status = 'active' ORDER BY a.account_id"
            ),
            {"c": channel_id},
        ).all()
    return {str(r[0]): list(r[1]) for r in rows}


def _channel(engine: Engine, channel_id: str) -> dict[str, object]:
    with Session(engine) as s:
        row = (
            s.execute(
                text(
                    "SELECT status, documentation_template, policy, language, template_id "
                    "FROM channels WHERE channel_id = :c"
                ),
                {"c": channel_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def test_membership_and_settings_are_independent_per_channel(engine: Engine) -> None:  # V-P2-19
    rt = _rt(engine)
    _inst, a, b = _setup_channels(engine)

    def run(cmd: Any, key: str) -> Any:
        return execute_command(
            rt,
            ADMIN_P,
            cmd,
            idempotency_key=key,
            correlation_id="cc",
        )

    run(cm.AddChannelMember(a, "acct-cc-member", ("read", "write")), "m1")
    run(cm.AddChannelMember(a, "acct-cc-agent", ("read",)), "m2")  # Agents join via their Account
    run(cm.AddChannelMember(b, "acct-cc-member", ("read", "moderate")), "m3")
    run(cm.SetChannelDocumentTemplate(a, "work-report-v2"), "t1")
    run(ch.ConfigureChannel(a, language="ko", retention_days=30), "cfg1")
    assert _members(engine, a) == {"acct-cc-agent": ["read"], "acct-cc-member": ["read", "write"]}
    assert _members(engine, b) == {"acct-cc-member": ["read", "moderate"]}
    assert _channel(engine, a)["documentation_template"] == "work-report-v2"
    assert _channel(engine, b)["documentation_template"] != "work-report-v2"
    assert _channel(engine, a)["language"] == "ko" and _channel(engine, b)["language"] != "ko"
    with Session(engine) as s:
        days = s.execute(
            text("SELECT channel_id, retention_days FROM channels WHERE channel_id IN (:a, :b)"),
            {"a": a, "b": b},
        ).all()
    assert {r[0]: r[1] for r in days} == {a: 30, b: 365}
    # permission change and removal on A never touch B
    run(cm.SetMemberPermissions(a, "acct-cc-member", ("read",)), "p1")
    run(cm.RemoveChannelMember(a, "acct-cc-agent"), "r1")
    assert _members(engine, a) == {"acct-cc-member": ["read"]}
    assert _members(engine, b) == {"acct-cc-member": ["read", "moderate"]}
    # idempotent replay: same key -> same Event, no duplicate change
    first = run(cm.AddChannelMember(b, "acct-cc-agent", ("read",)), "m4")
    again = run(cm.AddChannelMember(b, "acct-cc-agent", ("read",)), "m4")
    assert again.replayed and again.event_id == first.event_id
    with Session(engine) as s:
        events = s.execute(
            text(
                "SELECT payload->'change'->>'kind' FROM events WHERE aggregate_type = 'channel' "
                "AND aggregate_id = :c AND type = 'CHANNEL_CONFIGURED' ORDER BY aggregate_seq"
            ),
            {"c": a},
        ).all()
    assert [e[0] for e in events if e[0]] == [
        "member_added",
        "member_added",
        "documentation_template",
        "member_permissions",
        "member_removed",
    ]
    # invalid permission vocabulary and unknown member: stable errors, zero side effects
    from server.api.errors import ApiError

    with pytest.raises(ApiError) as exc:
        run(cm.AddChannelMember(a, "acct-cc-member", ("admin",)), "bad1")
    assert exc.value.code == "MEMBER_PERMISSIONS_INVALID"
    with pytest.raises(ApiError) as exc2:
        run(cm.RemoveChannelMember(a, "acct-cc-agent"), "bad2")
    assert exc2.value.code == "MEMBER_NOT_FOUND"
    # a non-manager cannot change membership (normalized 404) and cannot read another channel
    with pytest.raises(ApiError) as exc3:
        execute_command(
            rt,
            MEMBER_P,
            cm.AddChannelMember(b, "acct-cc-agent"),
            idempotency_key="x",
            correlation_id="cc",
        )
    assert exc3.value.status == 404


def test_member_rest_routes(database_url: str, engine: Engine) -> None:
    app = create_app(Settings(database_url=database_url, base_url="http://test"))
    from server.api.v1.channel_members import router as members_router

    app.router.routes.insert(0, members_router.routes[0])
    for route in members_router.routes[1:]:
        app.router.routes.insert(0, route)
    _inst, a, _b = (
        _setup_channels(engine) if not _members(engine, "chan-none") else (None, None, None)
    )
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {TOK_ADMIN}", "Idempotency-Key": "rest-m1"}
        r = c.post(
            f"/api/v1/channels/{a}/members", json={"account_id": "acct-cc-member"}, headers=h
        )
        assert r.status_code == 201, r.text
        lst = c.get(f"/api/v1/channels/{a}/members", headers=h)
        assert lst.status_code == 200 and any(
            m["account_id"] == "acct-cc-member" for m in lst.json()["items"]
        )
        denied = c.post(
            f"/api/v1/channels/{a}/members",
            json={"account_id": "acct-cc-agent"},
            headers={"Authorization": f"Bearer {TOK_MEMBER}", "Idempotency-Key": "rest-m2"},
        )
        assert denied.status_code == 404


def test_soft_delete_after_archive_keeps_references(engine: Engine) -> None:  # V-P2-18
    rt = _rt(engine)
    inst, a, _b = _setup_channels(engine)

    def run(cmd: Any, key: str) -> Any:
        return execute_command(
            rt,
            ADMIN_P,
            cmd,
            idempotency_key=key,
            correlation_id="cc",
        )

    with Session(engine) as s:
        chan_uuid = uuid.UUID(
            str(
                s.execute(
                    text("SELECT id FROM channels WHERE channel_id = :c"), {"c": a}
                ).scalar_one()
            )
        )
    # a Task with an Artifact link and a document reference in the channel
    run(cm.AddChannelMember(a, "acct-cc-admin", ("read", "write", "moderate")), "admin-member")
    task_id = run(
        t.CreateTask("Ref task", str(chan_uuid), "research", "LOW", criteria=CRITERIA), "task1"
    ).resource_id
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO artifacts (id, artifact_id, workspace_id, creator_account_id, "
                "storage_uri, "
                "mime, size, sha256, source_event_id) SELECT :i, 'art-cc-1', :ws, :c, "
                "'colab-fs://x', "
                "'text/plain', 1, repeat('a', 64), event_id FROM events WHERE task_id = :t LIMIT 1"
            ),
            {"i": uuid.uuid4(), "ws": WS, "c": ADMIN, "t": task_id},
        )
        s.execute(
            text(
                "INSERT INTO artifact_links (artifact_id, subject_type, subject_id, relation, "
                "linked_by) VALUES ('art-cc-1', 'task', :t, 'attachment', :b)"
            ),
            {"t": task_id, "b": ADMIN},
        )
        s.execute(
            text(
                "INSERT INTO documents (id, document_id, workspace_id, doc_type, source_type, "
                "source_id, status) VALUES (:i, 'doc-cc-1', :ws, 'task', 'task', :t, "
                "'DRAFT_PRE_VERIFICATION')"
            ),
            {"i": uuid.uuid4(), "ws": WS, "t": task_id},
        )
        inst_row = prov.load_instance(s, inst)
        assert inst_row is not None
        s.execute(
            text(
                "INSERT INTO thread_bindings (provider_instance_id, root_post_id, "
                "external_channel_id, subject_type, subject_id) VALUES (:p, 'root-1', 'mm-cc-a', "
                "'task', :t)"
            ),
            {"p": inst_row.id, "t": task_id},
        )
    from server.api.errors import ApiError

    # delete before archive is rejected; an open Task blocks deletion after archive
    with pytest.raises(ApiError) as exc:
        run(lifecycle.DeleteChannel(a), "del-early")
    assert exc.value.code == "CHANNEL_NOT_ARCHIVED"
    run(ch.ArchiveChannel(a), "arch")
    with pytest.raises(ApiError) as exc2:
        run(lifecycle.DeleteChannel(a), "del-open")
    assert (
        exc2.value.code == "CHANNEL_DELETE_BLOCKED"
        and "CHANNEL_HAS_OPEN_TASKS" in exc2.value.detail
    )
    run(t.CancelTask(task_id, "CLEANUP"), "cancel")
    # an enabled Telegram Bridge blocks deletion when the table exists
    with Session(engine) as s, s.begin():
        if s.execute(text("SELECT to_regclass('telegram_bridges')")).scalar() is not None:
            s.execute(
                text(
                    "INSERT INTO telegram_bridges (id, bridge_id, workspace_id, channel_id, "
                    "provider_instance_id, telegram_chat_id, direction, status, created_by) "
                    "VALUES (:i, 'bridge-cc-1', :ws, :c, 'tg:1', '-100999', 'bidirectional', "
                    "'enabled', :b)"
                ),
                {"i": uuid.uuid4(), "ws": WS, "c": chan_uuid, "b": ADMIN},
            )
            bridged = True
        else:
            bridged = False
    if bridged:
        with pytest.raises(ApiError) as exc3:
            run(lifecycle.DeleteChannel(a), "del-bridge")
        assert "CHANNEL_HAS_ENABLED_BRIDGE" in exc3.value.detail
        with Session(engine) as s, s.begin():
            s.execute(
                text(
                    "UPDATE telegram_bridges SET status = 'disabled' WHERE bridge_id = "
                    "'bridge-cc-1'"
                )
            )
    with Session(engine) as s:
        before = lifecycle.references(s, chan_uuid)
    res = run(lifecycle.DeleteChannel(a), "del")
    assert res.data["references_kept"] == before and before["thread_bindings"] == 1
    assert before["artifact_links"] == 1 and before["documents"] == 1
    view = _channel(engine, a)
    assert view["status"] == "deleted"
    with Session(engine) as s:
        assert lifecycle.channel_view(s, WS, a)["deleted_at"] is not None  # type: ignore[index]
        after = lifecycle.references(s, chan_uuid)
        assert after == before  # every reference intact: soft delete only
        assert (
            s.execute(
                text("SELECT count(*) FROM channels WHERE channel_id = :c"), {"c": a}
            ).scalar_one()
            == 1
        )
        audit = s.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE action = 'channel.delete' AND target_id = "
                ":c"
            ),
            {"c": a},
        ).scalar_one()
    assert audit == 1
    # deleting again is an idempotent no-op; members cannot be changed on a deleted channel
    assert run(lifecycle.DeleteChannel(a), "del2").replayed
    with pytest.raises(ApiError) as exc4:
        run(cm.AddChannelMember(a, "acct-cc-member"), "after-del")
    assert exc4.value.code == "CHANNEL_DELETED"
