"""P2-10 Command Router over the P0-10 grammar (V-P2-24). Runs against the real PostgreSQL with
a fake Mattermost client; every case counts Events to prove zero side effects on rejections."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application import channels as ch
from server.application.authz import BusAuthorizer
from server.channels.mattermost import provider as prov
from server.channels.mattermost.client import FakeMattermostClient
from server.channels.router import Router, SlashRequest
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import Principal
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

CASES = yaml.safe_load(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "mattermost" / "router-cases.yaml"
    ).read_text()
)["cases"]
WS = uuid.uuid4()
ACCOUNTS = {
    "linked": ("acct-rt-creator", uuid.uuid4(), "human", "mm-u-creator"),
    "agent": ("acct-rt-agent", uuid.uuid4(), "agent", "mm-u-agent"),
    "verifier": ("acct-rt-verifier", uuid.uuid4(), "human", "mm-u-verifier"),
    "approver": ("acct-rt-approver", uuid.uuid4(), "human", "mm-u-approver"),
}
ROLES: dict[str, tuple[list[str], list[str], dict[str, Any]]] = {
    "linked": (
        [
            "task.create",
            "task.read",
            "task.delegate",
            "task.complete",
            "task.cancel",
            "task.list",
            "approval.request",
            "approval.read",
            "artifact.read",
            "document.read",
            "verification.assign",
            "verification.read",
            "channel.manage",
            "notification.self",
        ],
        [],
        {},
    ),
    "agent": (
        [
            "task.read",
            "task.accept",
            "task.progress",
            "task.submit",
            "artifact.write",
            "artifact.read",
            "work.poll",
            "verification.read",
        ],
        [],
        {},
    ),
    "verifier": (
        [
            "task.read",
            "verification.assign",
            "verification.submit",
            "verification.read",
            "artifact.read",
            "document.read",
        ],
        [],
        {},
    ),
    "approver": (["task.read", "approval.decide", "approval.read"], [], {"max_risk": "HIGH"}),
}
EXT_CHANNEL = "mm-chan-a"
CLOCK = FixedClock(dt.datetime(2026, 4, 1, tzinfo=dt.UTC))
FAKE = FakeMattermostClient(
    users={
        "mm-u-agent": {"username": "agentuser"},
        "mm-u-verifier": {"username": "verifieruser"},
        "mm-u-creator": {"username": "creator"},
        "mm-u-approver": {"username": "approveruser"},
    },
)


@pytest.fixture(autouse=True, scope="module")
def _no_link_handlers() -> Iterator[None]:
    """The grammar cases assume no P2-13 link handler is mounted (LINK_PENDING guidance);
    create_app() in other modules registers them globally, so isolate the registry here."""
    from server.channels.router import LINK_HANDLERS

    saved = dict(LINK_HANDLERS)
    LINK_HANDLERS.clear()
    yield
    LINK_HANDLERS.clear()
    LINK_HANDLERS.update(saved)


@pytest.fixture(scope="module")
def runtime(database_url: str) -> Iterator[Runtime]:
    engine = make_engine(database_url)
    prov.set_client_factory(lambda inst: FAKE)
    rt = Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK)
    with Session(engine) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-rt', 'rt')"),
            {"i": WS},
        )
        repo = PostgresPolicyRepository()
        now = CLOCK.now()
        for key, (acct, acc_uuid, typ, _ext) in ACCOUNTS.items():
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc_uuid, "a": acct, "w": WS, "t": typ},
            )
            perms, deny, constraints = ROLES[key]
            repo.create_role(s, WS, f"rt-{key}", key)
            repo.commit_role_version(s, f"rt-{key}", perms, deny, constraints, acc_uuid)
            repo.assign_role(s, acc_uuid, f"rt-{key}", acc_uuid, now)
    creator = _principal("linked")
    inst_res = execute_command(
        rt,
        creator,
        ch.RegisterProviderInstance("http://mm.test", "colab-test", "team-1", "bot-user"),
        idempotency_key="rt-inst",
        correlation_id="rt",
    )
    execute_command(
        rt,
        creator,
        ch.ImportChannel(inst_res.resource_id, EXT_CHANNEL, "work", display_name="work-a"),
        idempotency_key="rt-import",
        correlation_id="rt",
    )
    with Session(engine) as s, s.begin():
        inst = prov.load_instance(s, inst_res.resource_id)
        assert inst is not None
        ch_row = prov.internal_channel(s, inst.id, EXT_CHANNEL)
        for key, (_acct, acc_uuid, _typ, ext) in ACCOUNTS.items():
            s.execute(
                text(
                    "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                    "external_user_id, "
                    "account_id, verification_method, status, verified_at) VALUES (:i, :l, :p, :e, "
                    ":a, "
                    "'admin_approval', 'active', now())"
                ),
                {"i": uuid.uuid4(), "l": f"link-rt-{key}", "p": inst.id, "e": ext, "a": acc_uuid},
            )
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": ch_row["id"], "a": acc_uuid},
            )
    rt.extras = {"provider_instance_id": inst_res.resource_id}  # type: ignore[attr-defined]
    yield rt
    prov.set_client_factory(None)
    engine.dispose()


def _principal(key: str) -> Principal:
    acct, acc_uuid, typ, _ = ACCOUNTS[key]
    return Principal(acct, str(acc_uuid), typ, f"sha256:{acct}")


def _events(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": WS}
            ).scalar_one()
        )


def test_router_cases(runtime: Runtime) -> None:
    engine = runtime.session_factory.kw["bind"]
    pid: str = runtime.extras["provider_instance_id"]  # type: ignore[attr-defined]
    router = Router(runtime, CLOCK)
    captured: dict[str, str] = {}
    root_post: str | None = None
    last_post_id = 0
    failures: list[str] = []
    for case in CASES:
        who = case["user"]
        user_id = "mm-u-unlinked" if who == "unlinked" else ACCOUNTS[who][3]
        text_in = case["text"]
        for k, v in captured.items():
            text_in = text_in.replace("${" + k + "}", v)
        if not case.get("same_post"):
            last_post_id += 1
        req = SlashRequest(
            provider_instance_id=pid,
            team_id="team-1",
            channel_id=EXT_CHANNEL,
            user_id=user_id,
            user_name=who,
            command="/colab",
            text=text_in[len("/colab ") :] if text_in.startswith("/colab ") else text_in,
            trigger_id=f"trig-{last_post_id}",
            post_id=f"slash-{last_post_id}",
            root_id=root_post if case.get("in_thread") else None,
        )
        if not text_in.startswith("/colab"):
            req = SlashRequest(
                pid,
                "team-1",
                EXT_CHANNEL,
                user_id,
                who,
                "",
                text_in,
                f"trig-{last_post_id}",
                "",
                f"slash-{last_post_id}",
            )
        before = _events(engine)
        resp = router.route(req)
        delta = _events(engine) - before
        exp = case["expect"]
        problems = []
        if resp.response_type != exp["type"]:
            problems.append(f"type {resp.response_type} != {exp['type']}")
        if resp.code != exp["code"]:
            problems.append(f"code {resp.code} != {exp['code']}")
        if delta != exp["events"]:
            problems.append(f"events {delta} != {exp['events']}")
        contains = exp.get("contains")
        if contains:
            for k, v in captured.items():
                contains = contains.replace("${" + k + "}", v)
            if contains not in resp.text:
                problems.append(f"text lacks {contains!r}: {resp.text[:120]!r}")
        if problems:
            failures.append(f"{case['name']}: {'; '.join(problems)} :: {resp.text[:160]}")
        if case.get("capture") and resp.resource_id:
            captured[case["capture"]] = resp.resource_id
            if case["capture"] == "task" and resp.post_id:
                root_post = resp.post_id
    assert not failures, "\n".join(failures)
    assert root_post is not None and FAKE.posts[root_post].root_id == ""
    replies = [p for p in FAKE.posts.values() if p.root_id == root_post]
    assert len(replies) >= 6, "thread replies must land under the Task root post"


def test_every_resource_verb_has_a_router_handler_or_phase_gate() -> None:
    from server.channels.commands import VERBS
    from server.channels.router import DOC_LATER_VERBS, PHASE_LATER

    missing = []
    for spec in VERBS:
        if spec.resource in ("help", "link") or spec.resource in PHASE_LATER:
            continue
        if spec.resource == "doc" and spec.verb in DOC_LATER_VERBS:
            continue
        if not hasattr(Router, f"_cmd_{spec.resource}_{spec.verb.replace('-', '_')}"):
            missing.append(f"{spec.resource} {spec.verb}")
    assert not missing, missing
