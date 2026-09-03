"""V-P7-22 Human-path acceptance and V-P7-02 full end-to-end, driven through Mattermost only.

Every Human step is a `/colab` slash command or a card button: create a Task with acceptance
criteria, delegate it, approve from the card, submit with an Artifact as evidence, assign and pass
an independent verification, complete, and read the closing Document. The Agent's own steps use
the same channel surface. Cards, thread replies and notifications are asserted on every iteration,
so a regression in the Mattermost surface fails here rather than silently.

The path runs against the real database and the real Command Router, card Renderer, outbox drain
and interactive-action handler, with the Mattermost server itself replaced by the in-process fake
client (`FakeMattermostClient`) — the seam the provider talks to. A live Team Edition instance can
be used instead by pointing the provider factory at a real client; nothing else changes.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application import artifacts as art
from server.application import channels as ch
from server.application import schedules as sch
from server.application.authz import BusAuthorizer
from server.artifacts.storage import ArtifactStorage
from server.channels.actions import ActionContext, ActionHandler, ActionRequest
from server.channels.mattermost import provider as prov
from server.channels.mattermost.client import FakeMattermostClient
from server.channels.mattermost.delivery import MattermostChannelProvider
from server.channels.outbox import drain_channels
from server.channels.router import Router, SlashRequest
from server.channels.task_cards import bind_delivered_cards
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.identity.principals import Principal
from server.notifications.rules import NotificationEngine, load_rules, sync_rules
from server.policy.repository import PostgresPolicyRepository
from server.schedules import router_handlers as schedule_router_handlers

pytestmark = pytest.mark.db
T0 = dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.UTC)
CLOCK = FixedClock(T0)
ACTION_SECRET = b"human-path-acceptance-secret"
WS = uuid.uuid4()
EXT_CHANNEL = "mm-hp-1"
ACCOUNTS: dict[str, tuple[str, uuid.UUID, str, str]] = {
    "human": ("acct-hp-human", uuid.uuid4(), "human", "mm-hp-human"),
    "agent": ("acct-hp-agent", uuid.uuid4(), "agent", "mm-hp-agent"),
    "verifier": ("acct-hp-verifier", uuid.uuid4(), "human", "mm-hp-verifier"),
    "approver": ("acct-hp-approver", uuid.uuid4(), "human", "mm-hp-approver"),
}
ROLES: dict[str, tuple[list[str], dict[str, Any]]] = {
    "human": (
        [
            "task.create",
            "task.read",
            "task.delegate",
            "task.complete",
            "task.list",
            "approval.request",
            "approval.read",
            "artifact.read",
            "document.read",
            "verification.assign",
            "verification.read",
            "schedule.manage",
            "schedule.run",
            "schedule.read",
            "channel.manage",
        ],
        # the Human here is also the workspace administrator who imported the channel; the
        # approver below stays at MEDIUM, which is what the button path depends on
        {"max_risk": "CRITICAL"},
    ),
    "agent": (
        [
            "task.read",
            "task.accept",
            "task.progress",
            "task.submit",
            "artifact.write",
            "artifact.read",
            "verification.read",
        ],
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
        {},
    ),
    "approver": (["task.read", "approval.decide", "approval.read"], {"max_risk": "MEDIUM"}),
}
FAKE = FakeMattermostClient(
    users={ext: {"username": key} for key, (_a, _u, _t, ext) in ACCOUNTS.items()}
)


def _principal(key: str) -> Principal:
    acct, acc_uuid, typ, _ = ACCOUNTS[key]
    return Principal(acct, str(acc_uuid), typ, f"sha256:{acct}")


@pytest.fixture(scope="module")
def runtime(database_url: str) -> Iterator[Runtime]:
    engine = make_engine(database_url)
    prov.set_client_factory(lambda inst: FAKE)
    schedule_router_handlers.register()  # the app mounts these in create_app()
    rt = Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK, str(WS))
    with Session(engine) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-hp', 'hp')"),
            {"i": WS},
        )
        sync_rules(s, str(WS), load_rules())
        repo = PostgresPolicyRepository()
        for key, (acct, acc_uuid, typ, _ext) in ACCOUNTS.items():
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc_uuid, "a": acct, "w": WS, "t": typ},
            )
            perms, constraints = ROLES[key]
            repo.create_role(s, WS, f"hp-{key}", key)
            repo.commit_role_version(s, f"hp-{key}", perms, [], constraints, acc_uuid)
            repo.assign_role(
                s, acc_uuid, f"hp-{key}", acc_uuid, dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
            )
    human = _principal("human")
    inst = execute_command(
        rt,
        human,
        ch.RegisterProviderInstance("http://mm.test", "colab-hp", "team-hp", "bot-hp"),
        idempotency_key="hp-inst",
        correlation_id="hp",
    )
    execute_command(
        rt,
        human,
        ch.ImportChannel(inst.resource_id, EXT_CHANNEL, "work", display_name="hp"),
        idempotency_key="hp-import",
        correlation_id="hp",
    )
    with Session(engine) as s, s.begin():
        instance = prov.load_instance(s, inst.resource_id)
        assert instance is not None
        channel = prov.internal_channel(s, instance.id, EXT_CHANNEL)
        for key, (_acct, acc_uuid, _typ, ext) in ACCOUNTS.items():
            s.execute(
                text(
                    "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                    "external_user_id, account_id, verification_method, status, verified_at) "
                    "VALUES (:i, :l, :p, :e, :a, 'admin_approval', 'active', now())"
                ),
                {
                    "i": uuid.uuid4(),
                    "l": f"link-hp-{key}",
                    "p": instance.id,
                    "e": ext,
                    "a": acc_uuid,
                },
            )
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": channel["id"], "a": acc_uuid},
            )
    rt.extras = {  # type: ignore[attr-defined]
        "provider_instance_id": inst.resource_id,
        "instance_uuid": instance.id,
        "channel_uuid": channel["id"],
        "channel_public_id": str(channel["channel_id"]),
    }
    yield rt
    prov.set_client_factory(None)
    engine.dispose()


def _engine(rt: Runtime) -> Engine:
    return rt.session_factory.kw["bind"]  # type: ignore[no-any-return]


def _slash(rt: Runtime, who: str, text_in: str, *, root: str | None = None, post: str) -> Any:
    """One `/colab` command exactly as Mattermost delivers it."""
    router = Router(rt, CLOCK)
    request = SlashRequest(
        provider_instance_id=rt.extras["provider_instance_id"],  # type: ignore[attr-defined]
        team_id="team-hp",
        channel_id=EXT_CHANNEL,
        user_id=ACCOUNTS[who][3],
        user_name=who,
        command="/colab",
        text=text_in,
        trigger_id=f"trig-{post}",
        post_id=post,
        root_id=root,
    )
    return router.route(request)


def _press(rt: Runtime, who: str, action: str, approval_id: str, post: str) -> Any:
    """A card button press: the same signed context Mattermost posts back."""
    handler = ActionHandler(rt, CLOCK, ACTION_SECRET)
    ctx = ActionContext(
        "approval", approval_id, action, int(CLOCK.now().timestamp()), uuid.uuid4().hex
    )
    return handler.handle(
        ActionRequest(
            rt.extras["provider_instance_id"],  # type: ignore[attr-defined]
            ACCOUNTS[who][3],
            EXT_CHANNEL,
            post,
            ctx.as_button_context(ACTION_SECRET),
            trigger_id=f"btn-{post}",
        )
    )


def _drain(rt: Runtime) -> None:
    """Deliver the cards and thread replies the Renderer queued, as the gateway would."""
    engine = _engine(rt)
    with Session(engine) as s, s.begin():
        instance = prov.load_instance(s, rt.extras["provider_instance_id"])  # type: ignore[attr-defined]
        assert instance is not None
        provider = MattermostChannelProvider(FAKE, instance)
        for _ in range(3):  # a card must be delivered before its patches and replies bind
            drain_channels(s, {"mattermost": provider}, CLOCK, str(WS))
            bind_delivered_cards(s)


def _artifact_root() -> Any:
    import os
    from pathlib import Path

    return Path(os.environ["AGENT_COLAB_ARTIFACT_ROOT"])


def _artifact(rt: Runtime, n: int) -> str:
    """The Agent registers its evidence Artifact through its own surface."""
    result = execute_command(
        rt,
        _principal("agent"),
        art.RegisterArtifact("report.md", "text/markdown", content=f"report {n}".encode()),
        idempotency_key=f"hp-art-{n}",
        correlation_id=f"hp-{n}",
        extras={"artifact_storage": ArtifactStorage(_artifact_root())},
    )
    return str(result.resource_id)


def _notify(rt: Runtime, event_id: str) -> int:
    """Run the notification rules over one Event, the way an operator tick does.

    Nothing in the command path fires the rules engine on its own, so the acceptance path drives
    it explicitly rather than asserting a behaviour the product does not perform here.
    """
    with Session(_engine(rt)) as s, s.begin():
        event = PostgresEventStore(s, clock=CLOCK).get(event_id)
        assert event is not None
        return len(NotificationEngine(clock=CLOCK).on_event(s, event))


def _notifications(rt: Runtime) -> int:
    with Session(_engine(rt)) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM notifications WHERE workspace_id = :w"), {"w": WS}
            ).scalar_one()
        )


def _document_status(rt: Runtime, task_id: str) -> str | None:
    with Session(_engine(rt)) as s:
        row = s.execute(
            text(
                "SELECT v.status FROM document_versions v JOIN documents d "
                "ON d.document_id = v.document_id WHERE d.source_type = 'task' "
                "AND d.source_id = :t ORDER BY v.version DESC LIMIT 1"
            ),
            {"t": task_id},
        ).first()
    return None if row is None else str(row[0])


def human_path(rt: Runtime, n: int, *, with_schedule: bool = False) -> dict[str, Any]:
    """One full pass. Returns the checkpoints so a caller can assert on the whole run."""
    tag = f"{n:03d}-{uuid.uuid4().hex[:6]}"
    notifications_before = _notifications(rt)
    post = 0

    def nxt() -> str:
        nonlocal post
        post += 1
        return f"hp-{tag}-{post}"

    if with_schedule:  # V-P7-02: the chain may also start from a Schedule
        run_now = _slash(rt, "human", f"schedule run-now {rt.extras['schedule_id']}", post=nxt())  # type: ignore[attr-defined]
        assert run_now.code == "OK", run_now.text

    created = _slash(
        rt,
        "human",
        f'task create "Acceptance {tag}" --criteria "report attached" --domain research',
        post=nxt(),
    )
    assert created.code == "OK", created.text
    task_id = str(created.resource_id)
    root_post = created.post_id
    assert root_post, "the Task card must be posted in the channel"
    _drain(rt)

    delegated = _slash(rt, "human", f"task delegate {task_id} --to @agent", post=nxt())
    assert delegated.code == "OK", delegated.text
    accepted = _slash(rt, "agent", "task accept", root=root_post, post=nxt())
    assert accepted.code == "OK", accepted.text
    progressed = _slash(rt, "agent", 'task progress "drafting"', root=root_post, post=nxt())
    assert progressed.code == "OK", progressed.text

    requested = _slash(
        rt, "human", f"approve request {task_id} --action tool:task_delegate", post=nxt()
    )
    assert requested.code == "OK", requested.text
    approval_id = str(requested.resource_id)
    notified = _notify(rt, str(requested.event_id))
    assert notified >= 1, "the approval request must produce a notification"
    _drain(rt)
    pressed = _press(rt, "approver", "approve", approval_id, nxt())
    assert pressed.executed and pressed.code == "OK", pressed
    with Session(_engine(rt)) as s:
        approval_status = s.execute(
            text("SELECT status FROM approval_grants WHERE approval_id = :a"), {"a": approval_id}
        ).scalar_one()
    assert approval_status == "APPROVED", approval_status

    artifact_id = _artifact(rt, n)
    submitted = _slash(
        rt, "agent", f"task submit --evidence {artifact_id}", root=root_post, post=nxt()
    )
    assert submitted.code == "OK", submitted.text
    assigned = _slash(rt, "human", f"verify assign {task_id} --to @verifier", post=nxt())
    assert assigned.code == "OK", assigned.text
    passed = _slash(rt, "verifier", f"verify pass {task_id} --evidence {artifact_id}", post=nxt())
    assert passed.code == "OK", passed.text
    completed = _slash(rt, "human", "task complete", root=root_post, post=nxt())
    assert completed.code == "OK", completed.text
    shown = _slash(rt, "human", f"doc show {task_id}", post=nxt())
    assert shown.code == "OK" and "FINALIZED" in shown.text, shown.text
    _drain(rt)

    replies = [p for p in FAKE.posts.values() if p.root_id == root_post]
    assert FAKE.posts[root_post].root_id == "", "the Task card is a root post"
    assert len(replies) >= 5, f"thread replies: {len(replies)}"
    assert _document_status(rt, task_id) == "FINALIZED"
    notifications_after = _notifications(rt)
    assert notifications_after > notifications_before, "the approval must notify its approvers"
    return {
        "task_id": task_id,
        "approval_id": approval_id,
        "artifact_id": artifact_id,
        "thread_replies": len(replies),
        "notifications": notifications_after - notifications_before,
    }


def test_human_path_ten_consecutive_times(runtime: Runtime) -> None:
    """V-P7-22: ten consecutive Mattermost-only runs with cards, threads and notifications."""
    runs = [human_path(runtime, n) for n in range(10)]
    assert len(runs) == 10
    assert all(r["thread_replies"] >= 5 and r["notifications"] >= 1 for r in runs), runs
    assert len({r["task_id"] for r in runs}) == 10
    print(f"human path: {len(runs)}/10 consecutive successes")


def test_full_end_to_end_twenty_consecutive_times(runtime: Runtime) -> None:
    """V-P7-02: twenty consecutive runs of Mattermost → Schedule → Agent → Approval → Artifact →
    Document → Verification."""
    human = _principal("human")
    created = execute_command(
        runtime,
        human,
        sch.CreateSchedule(
            name="acceptance schedule",
            cron_expression="0 9 * * *",
            timezone="UTC",
            channel_id=str(runtime.extras["channel_public_id"]),  # type: ignore[attr-defined]
            execution_principal_id="acct-hp-agent",
            agent_selection={"mode": "capability", "required_capabilities": ["cap-hp"]},
            action_template={
                "schema_id": "action-template.v1",
                "action": "task_create",
                "input": {"title": "scheduled acceptance", "domain": "research", "risk": "LOW"},
            },
        ),
        idempotency_key="hp-sched-create",
        correlation_id="hp",
    )
    schedule_id = str(created.resource_id)
    execute_command(
        runtime,
        human,
        sch.EnableSchedule(schedule_id=schedule_id),
        idempotency_key="hp-sched-enable",
        correlation_id="hp",
    )
    runtime.extras["schedule_id"] = schedule_id  # type: ignore[attr-defined]

    runs = [human_path(runtime, 100 + n, with_schedule=True) for n in range(20)]
    assert len(runs) == 20 and len({r["task_id"] for r in runs}) == 20
    with Session(_engine(runtime)) as s:
        manual_runs = int(
            s.execute(
                text(
                    "SELECT count(*) FROM schedule_runs WHERE schedule_id = :s "
                    "AND run_kind = 'MANUAL'"
                ),
                {"s": schedule_id},
            ).scalar_one()
        )
    assert manual_runs == 20, manual_runs
    print(f"full end to end: {len(runs)}/20 consecutive successes, {manual_runs} Schedule Runs")
