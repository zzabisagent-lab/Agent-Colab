"""V-P7-22 Human-path acceptance and V-P7-02 full end-to-end, driven through real Mattermost.

Every Human step is a `/colab` slash command or a card button, and every one of them travels the
whole way: the test asks Mattermost to execute the command (`POST /api/v4/commands/execute`) or to
press a card button (`POST /api/v4/posts/{post}/actions/{action}`), Mattermost calls this server's
provider callbacks, the Command Router runs the command, and the reply, the cards and the thread
replies are posts that Mattermost itself holds. Assertions read them back from Mattermost.

Nothing here is simulated: a Team Edition instance is started when it is not already running
(`scripts/dev/mattermost-local.sh`), a fresh team, channel and four member accounts are created
through the Mattermost API for each run, the slash command is registered with Mattermost by the
product's own `RegisterSlashCommand`, and the gateway posts with the bot token from the
environment. Credentials live only in `~/.local/opt/mattermost/.spike-credentials` and are never
printed. The path skips only when Mattermost genuinely cannot be reached, with the reason stated.

    export AGENT_COLAB_TEST_DATABASE_URL=postgresql://colab@127.0.0.1:54329/colab_test
    uv run pytest tests/e2e/test_human_path_acceptance.py -q -s
"""

from __future__ import annotations

import datetime as dt
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application import artifacts as art
from server.application import channels as ch
from server.application import schedules as sch
from server.application.authz import BusAuthorizer
from server.artifacts.storage import ArtifactStorage
from server.channels import actions as chan_actions
from server.channels import work_messages
from server.channels.mattermost import provider as prov
from server.channels.mattermost.client import Post
from server.channels.mattermost.delivery import MattermostChannelProvider
from server.channels.outbox import drain_channels
from server.channels.task_cards import bind_delivered_cards
from server.config import Settings
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import SystemClock
from server.events.postgres_store import PostgresEventStore
from server.identity.principals import Principal
from server.main import create_app
from server.notifications.rules import NotificationEngine, load_rules, sync_rules
from server.policy.repository import PostgresPolicyRepository
from server.schedules import router_handlers as schedule_router_handlers
from server.secrets.envelope import new_master_key

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = ROOT / "scripts" / "dev" / "mattermost-local.sh"
MM_BASE = os.environ.get("COLAB_MATTERMOST_URL", "http://127.0.0.1:8065").rstrip("/")
DEFAULT_CREDENTIALS = Path.home() / ".local/opt/mattermost/.spike-credentials"
CREDENTIALS = Path(os.environ.get("COLAB_MATTERMOST_CREDENTIALS", str(DEFAULT_CREDENTIALS)))
CLOCK = SystemClock()
ROLE_VALID_FROM = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
TRIGGER = "colab"
WS = uuid.uuid4()
ACCOUNTS: dict[str, tuple[str, uuid.UUID, str]] = {
    "human": ("acct-hp-human", uuid.uuid4(), "human"),
    "agent": ("acct-hp-agent", uuid.uuid4(), "agent"),
    "verifier": ("acct-hp-verifier", uuid.uuid4(), "human"),
    "approver": ("acct-hp-approver", uuid.uuid4(), "human"),
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


# --- the live Mattermost instance ---------------------------------------------------------------


def _ping(base: str) -> bool:
    try:
        return httpx.get(f"{base}/api/v4/system/ping", timeout=5.0).status_code == 200
    except httpx.HTTPError:
        return False


def _ensure_mattermost() -> tuple[bool, str]:
    """Start the local Team Edition when it is not already up; never print its credentials."""
    if _ping(MM_BASE):
        return True, f"already running at {MM_BASE}"
    if MM_BASE != "http://127.0.0.1:8065":
        return False, f"{MM_BASE} is not reachable and is not the local instance"
    if not START_SCRIPT.exists():
        return False, f"{START_SCRIPT.relative_to(ROOT)} is missing"
    try:
        done = subprocess.run(
            ["bash", str(START_SCRIPT), "start"], capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"mattermost-local.sh start failed: {type(exc).__name__}"
    if _ping(MM_BASE):
        return True, "started by scripts/dev/mattermost-local.sh"
    tail = (done.stdout + done.stderr).strip().splitlines()
    return False, f"mattermost did not come up: {tail[-1] if tail else 'no output'}"


def _credentials() -> tuple[dict[str, str], str]:
    if not CREDENTIALS.exists():
        return {}, f"{CREDENTIALS} is missing (run scripts/dev/mattermost-local.sh configure)"
    values: dict[str, str] = {}
    for line in CREDENTIALS.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    missing = [k for k in ("ADMIN_TOKEN", "BOT_TOKEN", "BOT_USER_ID") if not values.get(k)]
    if missing:
        return {}, f"{CREDENTIALS} has no {', '.join(missing)}"
    return values, "ok"


@dataclass
class Live:
    """The Mattermost objects this run owns. Tokens are never part of the representation."""

    team_id: str
    team_name: str
    channel_id: str
    users: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    admin_token: str = field(default="", repr=False)
    bot_token: str = field(default="", repr=False)
    bot_user_id: str = ""


def _api(token: str, method: str, path: str, **kwargs: Any) -> Any:
    response = httpx.request(
        method,
        f"{MM_BASE}/api/v4{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
        **kwargs,
    )
    if response.status_code >= 400:
        detail = f"{response.status_code} {response.text}"
        raise AssertionError(f"mattermost {method} {path} -> {detail}")
    return response.json() if response.content else None


def _provision(creds: dict[str, str]) -> Live:
    """A fresh team, channel and four member accounts, created through the Mattermost API."""
    admin = creds["ADMIN_TOKEN"]
    tag = uuid.uuid4().hex[:8]
    team = _api(
        admin, "POST", "/teams", json={"name": f"hp{tag}", "display_name": f"hp {tag}", "type": "O"}
    )
    channel = _api(
        admin,
        "POST",
        "/channels",
        json={
            "team_id": team["id"],
            "name": f"work{tag}",
            "display_name": "acceptance work",
            "type": "O",
        },
    )
    live = Live(
        team_id=str(team["id"]),
        team_name=str(team["name"]),
        channel_id=str(channel["id"]),
        admin_token=admin,
        bot_token=creds["BOT_TOKEN"],
        bot_user_id=creds["BOT_USER_ID"],
    )
    for role in ACCOUNTS:
        username = f"hp-{role}-{tag}"
        password = f"{uuid.uuid4().hex}Aa1!"  # a throwaway account password, never printed
        user = _api(
            admin,
            "POST",
            "/users",
            json={
                "username": username,
                "email": f"{username}@example.invalid",
                "password": password,
            },
        )
        _api(
            admin,
            "POST",
            f"/teams/{team['id']}/members",
            json={"team_id": team["id"], "user_id": user["id"]},
        )
        _api(admin, "POST", f"/channels/{channel['id']}/members", json={"user_id": user["id"]})
        login = httpx.post(
            f"{MM_BASE}/api/v4/users/login",
            json={"login_id": username, "password": password},
            timeout=60.0,
        )
        assert login.status_code == 200, f"login for {username} -> {login.status_code}"
        token = login.headers.get("Token", "")
        assert token, f"mattermost returned no session token for {username}"
        live.users[role] = {"id": str(user["id"]), "username": username, "token": token}
    # the gateway posts as the bot, so the bot must be a member of the team and the channel
    _api(
        admin,
        "POST",
        f"/teams/{team['id']}/members",
        json={"team_id": team["id"], "user_id": live.bot_user_id},
    )
    _api(admin, "POST", f"/channels/{channel['id']}/members", json={"user_id": live.bot_user_id})
    return live


@pytest.fixture(scope="module")
def live() -> Iterator[Live]:
    ok, detail = _ensure_mattermost()
    if not ok:
        pytest.skip(f"Mattermost is not available: {detail}")
    creds, why = _credentials()
    if not creds:
        pytest.skip(f"Mattermost credentials unusable: {why}")
    yield _provision(creds)


# --- this server, as Mattermost reaches it ------------------------------------------------------


@dataclass
class Ctx:
    """Everything one pass needs: the server under test, the live instance and the seeded ids."""

    rt: Runtime
    live: Live
    base: str
    provider_instance_id: str
    instance_uuid: uuid.UUID
    channel_uuid: uuid.UUID
    channel_public_id: str
    schedule_id: str = ""


@pytest.fixture(scope="module")
def server(live: Live, database_url: str) -> Iterator[str]:
    """The application, served on loopback so Mattermost can call its provider callbacks."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    base = f"http://127.0.0.1:{port}"
    previous = {
        key: os.environ.get(key)
        for key in (
            "AGENT_COLAB_BASE_URL",
            "AGENT_COLAB_GATEWAY_DRAIN",
            "AGENT_COLAB_MATTERMOST_ACTION_SECRET",
            "AGENT_COLAB_MATTERMOST_ADMIN_TOKEN",
            "AGENT_COLAB_MATTERMOST_BOT_TOKEN",
            "AGENT_COLAB_MATTERMOST_URL",
        )
    }
    # the same environment a deployment configures: the bot posts, the admin token registers the
    # slash command, and the button callback URL is absolute so Mattermost can reach this server
    os.environ["AGENT_COLAB_BASE_URL"] = base
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"  # the path drains deterministically instead
    os.environ["AGENT_COLAB_MATTERMOST_ACTION_SECRET"] = uuid.uuid4().hex
    os.environ["AGENT_COLAB_MATTERMOST_ADMIN_TOKEN"] = live.admin_token
    os.environ["AGENT_COLAB_MATTERMOST_BOT_TOKEN"] = live.bot_token
    os.environ["AGENT_COLAB_MATTERMOST_URL"] = MM_BASE
    hooks = list(work_messages.POST_HOOKS)  # create_app registers a runtime-bound intake hook
    app = create_app(
        Settings(database_url=database_url, base_url=base, master_key_b64=new_master_key())
    )
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.1)
    assert srv.started, "the application did not start"
    yield base
    srv.should_exit = True
    thread.join(timeout=10)
    work_messages.POST_HOOKS[:] = hooks  # this runtime's engine goes away with the module
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="module")
def ctx(live: Live, server: str, database_url: str) -> Iterator[Ctx]:
    engine = make_engine(database_url)
    schedule_router_handlers.register()  # the app mounts these in create_app()
    rt = Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK, str(WS))
    with Session(engine) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-hp', 'hp')"),
            {"i": WS},
        )
        sync_rules(s, str(WS), load_rules())
        repo = PostgresPolicyRepository()
        for key, (acct, acc_uuid, typ) in ACCOUNTS.items():
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
            repo.assign_role(s, acc_uuid, f"hp-{key}", acc_uuid, ROLE_VALID_FROM)
    human = _principal("human")
    inst = execute_command(
        rt,
        human,
        ch.RegisterProviderInstance(MM_BASE, live.team_name, live.team_id, live.bot_user_id),
        idempotency_key="hp-inst",
        correlation_id="hp",
    )
    execute_command(
        rt,
        human,
        ch.RegisterSlashCommand(
            provider_instance_id=inst.resource_id,
            callback_url=f"{server}/api/v1/providers/mattermost/commands",
            trigger=TRIGGER,
        ),
        idempotency_key="hp-slash",
        correlation_id="hp",
    )
    execute_command(
        rt,
        human,
        ch.ImportChannel(inst.resource_id, live.channel_id, "work", display_name="acceptance"),
        idempotency_key="hp-import",
        correlation_id="hp",
    )
    with Session(engine) as s, s.begin():
        instance = prov.load_instance(s, inst.resource_id)
        assert instance is not None
        channel = prov.internal_channel(s, instance.id, live.channel_id)
        for key, (_acct, acc_uuid, _typ) in ACCOUNTS.items():
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
                    "e": live.users[key]["id"],
                    "a": acc_uuid,
                },
            )
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": channel["id"], "a": acc_uuid},
            )
    yield Ctx(
        rt=rt,
        live=live,
        base=server,
        provider_instance_id=inst.resource_id,
        instance_uuid=instance.id,
        channel_uuid=channel["id"],
        channel_public_id=str(channel["channel_id"]),
    )
    engine.dispose()


def _principal(key: str) -> Principal:
    acct, acc_uuid, typ = ACCOUNTS[key]
    return Principal(acct, str(acc_uuid), typ, f"sha256:{acct}")


def _engine(ctx: Ctx) -> Engine:
    return ctx.rt.session_factory.kw["bind"]  # type: ignore[no-any-return]


# --- the two surfaces a Human uses --------------------------------------------------------------


def _slash(ctx: Ctx, who: str, command: str, *, root: str | None = None) -> str:
    """Ask Mattermost to run one `/colab` command; it calls this server and returns the reply."""
    body: dict[str, Any] = {
        "channel_id": ctx.live.channel_id,
        "team_id": ctx.live.team_id,
        "command": f"/{TRIGGER} {command}",
    }
    if root:
        body["root_id"] = root
    reply = _api(ctx.live.users[who]["token"], "POST", "/commands/execute", json=body)
    return str(reply.get("text", ""))


def _press(ctx: Ctx, who: str, post_id: str, action_id: str) -> None:
    """Press a card button; Mattermost posts the signed context back to this server."""
    _api(ctx.live.users[who]["token"], "POST", f"/posts/{post_id}/actions/{action_id}")


def _post(ctx: Ctx, post_id: str) -> Post:
    """One post as Mattermost holds it."""
    return prov.client_for(_instance(ctx)).get_post(post_id)


def _thread(ctx: Ctx, root_post_id: str) -> list[Post]:
    """The replies Mattermost holds under a root post (the root itself excluded)."""
    data = _api(ctx.live.admin_token, "GET", f"/posts/{root_post_id}/thread")
    posts = data.get("posts", {})
    return [
        Post(
            id=str(p["id"]),
            channel_id=str(p["channel_id"]),
            user_id=str(p["user_id"]),
            message=str(p["message"]),
            root_id=str(p.get("root_id") or ""),
            props=dict(p.get("props") or {}),
        )
        for p in posts.values()
        if str(p.get("root_id") or "") == root_post_id
    ]


def _instance(ctx: Ctx) -> prov.ProviderInstance:
    with Session(_engine(ctx)) as s:
        instance = prov.load_instance(s, ctx.provider_instance_id)
    assert instance is not None
    return instance


def _drain(ctx: Ctx) -> None:
    """Deliver the cards and patches the Renderer queued, into the real channel."""
    instance = _instance(ctx)
    client = prov.client_for(instance)  # bot token from the environment, as in production
    with Session(_engine(ctx)) as s, s.begin():
        provider = MattermostChannelProvider(client, instance)
        for _ in range(3):  # a card must be delivered before its patches and replies bind
            drain_channels(s, {"mattermost": provider}, CLOCK, str(WS))
            bind_delivered_cards(s)


# --- reading the state back ---------------------------------------------------------------------


def _id_in(reply: str, pattern: str) -> str:
    match = re.search(pattern, reply)
    assert match, f"no id matching {pattern!r} in reply: {reply!r}"
    return match.group(1)


def _root_post(ctx: Ctx, task_id: str) -> str:
    with Session(_engine(ctx)) as s:
        binding = prov.binding_for_subject(s, ctx.instance_uuid, "task", task_id)
    assert binding is not None, f"no thread binding for {task_id}"
    return binding.root_post_id


def _card_post(ctx: Ctx, subject_type: str, subject_id: str) -> str:
    with Session(_engine(ctx)) as s:
        row = s.execute(
            text(
                "SELECT post_id FROM channel_posts WHERE subject_type = :t AND subject_id = :s "
                "AND role = 'card' AND status = 'sent' AND post_id IS NOT NULL"
            ),
            {"t": subject_type, "s": subject_id},
        ).first()
    if row is None:
        with Session(_engine(ctx)) as s:
            pend = s.execute(
                text(
                    "SELECT kind, status, attempts, coalesce(last_error, '-') FROM delivery_outbox "
                    "WHERE workspace_id = :w AND status <> 'sent' ORDER BY created_at LIMIT 5"
                ),
                {"w": WS},
            ).all()
            counts = s.execute(
                text(
                    "SELECT status, count(*) FROM delivery_outbox WHERE workspace_id = :w "
                    "GROUP BY status"
                ),
                {"w": WS},
            ).all()
        raise AssertionError(
            f"no delivered {subject_type} card for {subject_id}: {list(counts)} {list(pend)}"
        )
    return str(row[0])


def _button_ids(post: Post) -> list[str]:
    attachments = post.props.get("attachments") or []
    return [
        str(action.get("id"))
        for attachment in attachments
        for action in (attachment.get("actions") or [])
    ]


def _artifact_root() -> Path:
    return Path(os.environ["AGENT_COLAB_ARTIFACT_ROOT"])


def _artifact(ctx: Ctx, n: int) -> str:
    """The Agent registers its evidence Artifact through its own surface."""
    result = execute_command(
        ctx.rt,
        _principal("agent"),
        art.RegisterArtifact("report.md", "text/markdown", content=f"report {n}".encode()),
        idempotency_key=f"hp-art-{uuid.uuid4().hex}",
        correlation_id=f"hp-{n}",
        extras={"artifact_storage": ArtifactStorage(_artifact_root())},
    )
    return str(result.resource_id)


def _notify(ctx: Ctx, approval_id: str) -> int:
    """Run the notification rules over the approval request, the way an operator tick does.

    Nothing in the command path fires the rules engine on its own, so the acceptance path drives
    it explicitly rather than asserting a behaviour the product does not perform here.
    """
    with Session(_engine(ctx)) as s, s.begin():
        events = PostgresEventStore(s, clock=CLOCK).stream(str(WS), "approval", approval_id)
        requested = [e for e in events if e["type"] == "APPROVAL_REQUESTED"]
        assert requested, f"no APPROVAL_REQUESTED event for {approval_id}"
        return len(NotificationEngine(clock=CLOCK).on_event(s, requested[0]))


def _notifications(ctx: Ctx) -> int:
    with Session(_engine(ctx)) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM notifications WHERE workspace_id = :w"), {"w": WS}
            ).scalar_one()
        )


def _document_status(ctx: Ctx, task_id: str) -> str | None:
    with Session(_engine(ctx)) as s:
        row = s.execute(
            text(
                "SELECT v.status FROM document_versions v JOIN documents d "
                "ON d.document_id = v.document_id WHERE d.source_type = 'task' "
                "AND d.source_id = :t ORDER BY v.version DESC LIMIT 1"
            ),
            {"t": task_id},
        ).first()
    return None if row is None else str(row[0])


def _action_audit(ctx: Ctx, subject_id: str) -> str:
    """What the server recorded for the last button press on this subject."""
    with Session(_engine(ctx)) as s:
        row = s.execute(
            text(
                "SELECT action, result, error_code FROM audit_events "
                "WHERE target_type = 'mattermost_action' AND target_id = :s "
                "ORDER BY occurred_at DESC LIMIT 1"
            ),
            {"s": subject_id},
        ).first()
    return "no audit row" if row is None else f"{row[0]} {row[1]} {row[2]}"


def _approval_status(ctx: Ctx, approval_id: str) -> str:
    with Session(_engine(ctx)) as s:
        return str(
            s.execute(
                text("SELECT status FROM approval_grants WHERE approval_id = :a"),
                {"a": approval_id},
            ).scalar_one()
        )


# --- one pass -----------------------------------------------------------------------------------


def human_path(ctx: Ctx, n: int, *, with_schedule: bool = False) -> dict[str, Any]:
    """One full pass through Mattermost. Returns the checkpoints a caller asserts on."""
    tag = f"{n:03d}-{uuid.uuid4().hex[:6]}"
    notifications_before = _notifications(ctx)
    agent_name = ctx.live.users["agent"]["username"]
    verifier_name = ctx.live.users["verifier"]["username"]

    if with_schedule:  # V-P7-02: the chain may also start from a Schedule
        run_now = _slash(ctx, "human", f"schedule run-now {ctx.schedule_id}")
        assert "Manual Run requested" in run_now, run_now

    created = _slash(
        ctx,
        "human",
        f'task create "Acceptance {tag}" --criteria "report attached" --domain research',
    )
    task_id = _id_in(created, r"Task (\S+) created")
    root_post = _root_post(ctx, task_id)
    root = _post(ctx, root_post)  # Mattermost's own copy of the Task thread root
    assert root.root_id == "", f"the Task thread root is a root post: {root.root_id!r}"
    assert root.channel_id == ctx.live.channel_id
    assert task_id in root.message, root.message
    _drain(ctx)
    task_card = _post(ctx, _card_post(ctx, "task", task_id))
    assert task_card.channel_id == ctx.live.channel_id
    assert task_card.user_id == ctx.live.bot_user_id, "the card is posted by the gateway bot"

    delegated = _slash(ctx, "human", f"task delegate {task_id} --to @{agent_name}")
    assert "delegated to" in delegated, delegated
    accepted = _slash(ctx, "agent", "task accept", root=root_post)
    assert "accepted" in accepted, accepted
    progressed = _slash(ctx, "agent", 'task progress "drafting"', root=root_post)
    assert "Progress on" in progressed, progressed

    requested = _slash(ctx, "human", f"approve request {task_id} --action tool:task_delegate")
    approval_id = _id_in(requested, r"Approval (\S+) requested")
    assert _notify(ctx, approval_id) >= 1, "the approval request must produce a notification"
    _drain(ctx)
    approval_post_id = _card_post(ctx, "approval", approval_id)
    approval_card = _post(ctx, approval_post_id)
    approve_id = chan_actions.button_action_id("approval", approval_id, "approve")
    assert approve_id in _button_ids(approval_card), (
        f"Mattermost holds no Approve button on the card: {_button_ids(approval_card)}"
    )
    _press(ctx, "approver", approval_post_id, approve_id)
    decided = _approval_status(ctx, approval_id)
    assert decided == "APPROVED", f"{decided}; server recorded: {_action_audit(ctx, approval_id)}"

    artifact_id = _artifact(ctx, n)
    submitted = _slash(ctx, "agent", f"task submit --evidence {artifact_id}", root=root_post)
    assert "submitted" in submitted, submitted
    assigned = _slash(ctx, "human", f"verify assign {task_id} --to @{verifier_name}")
    assert "assigned to" in assigned, assigned
    passed = _slash(ctx, "verifier", f"verify pass {task_id} --evidence {artifact_id}")
    assert "PASS" in passed.upper(), passed
    completed = _slash(ctx, "human", "task complete", root=root_post)
    assert "completed" in completed, completed
    shown = _slash(ctx, "human", f"doc show {task_id}")
    assert "FINALIZED" in shown, shown
    _drain(ctx)

    replies = _thread(ctx, root_post)
    assert len(replies) >= 5, f"thread replies Mattermost holds: {len(replies)}"
    assert _document_status(ctx, task_id) == "FINALIZED"
    notifications_after = _notifications(ctx)
    assert notifications_after > notifications_before, "the approval must notify its approvers"
    return {
        "task_id": task_id,
        "approval_id": approval_id,
        "artifact_id": artifact_id,
        "root_post_id": root_post,
        "approval_post_id": approval_post_id,
        "thread_replies": len(replies),
        "notifications": notifications_after - notifications_before,
    }


# --- the two criteria ---------------------------------------------------------------------------


def test_human_path_ten_consecutive_times(ctx: Ctx) -> None:
    """V-P7-22: ten consecutive runs against Mattermost, with cards, threads and notifications."""
    runs = [human_path(ctx, n) for n in range(10)]
    assert len(runs) == 10
    assert all(r["thread_replies"] >= 5 and r["notifications"] >= 1 for r in runs), runs
    assert len({r["task_id"] for r in runs}) == 10
    assert len({r["root_post_id"] for r in runs}) == 10
    print(f"human path: {len(runs)}/10 consecutive successes against {MM_BASE}")


def test_full_end_to_end_twenty_consecutive_times(ctx: Ctx) -> None:
    """V-P7-02: twenty consecutive runs of Mattermost → Schedule → Agent → Approval → Artifact →
    Document → Verification."""
    human = _principal("human")
    created = execute_command(
        ctx.rt,
        human,
        sch.CreateSchedule(
            name="acceptance schedule",
            cron_expression="0 9 * * *",
            timezone="UTC",
            channel_id=ctx.channel_public_id,
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
        ctx.rt,
        human,
        sch.EnableSchedule(schedule_id=schedule_id),
        idempotency_key="hp-sched-enable",
        correlation_id="hp",
    )
    ctx.schedule_id = schedule_id

    runs = [human_path(ctx, 100 + n, with_schedule=True) for n in range(20)]
    assert len(runs) == 20 and len({r["task_id"] for r in runs}) == 20
    with Session(_engine(ctx)) as s:
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
    print(
        f"full end to end: {len(runs)}/20 consecutive successes against {MM_BASE}, "
        f"{manual_runs} Schedule Runs"
    )
