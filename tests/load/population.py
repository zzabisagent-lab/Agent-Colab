"""Seeds the §21.1 population for a load or soak run (P7-04).

The population is real: Accounts and Agents authorised through the Policy Engine, Channels with
memberships, Telegram Bridges, and Schedules on a five-minute cycle offset so the configured
number falls due every minute. Nothing here bypasses a command that production would run for the
same effect except the bulk row inserts that stand in for months of ordinary use.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.identity.principals import token_hash
from server.policy.repository import PostgresPolicyRepository
from tests.load.profile import Profile

WRITER_ROLE_PERMISSIONS = ["task.create", "task.read", "task.progress", "task.delegate"]
AGENT_ROLE_PERMISSIONS = ["task.accept", "task.progress", "task.read", "task.submit"]
OPS_ROLE_PERMISSIONS = ["admin.settings", "task.read", "schedule.read"]
# the Workspace system Account the scheduler tick runs as (development plan §10A.2)
SYSTEM_ROLE_PERMISSIONS = [
    "task.create",
    "task.read",
    "task.progress",
    "task.delegate",
    "task.reassign",
    "verification.assign",
]


@dataclass
class Population:
    """Identifiers a driver needs: the Workspace, its channels and the tokens to call the API."""

    tag: str
    ws: uuid.UUID
    owner_account: uuid.UUID
    owner_token: str = field(repr=False)
    ops_token: str = field(repr=False)
    channel_ids: list[str] = field(default_factory=list)
    channel_uuids: list[uuid.UUID] = field(default_factory=list)
    schedule_ids: list[str] = field(default_factory=list)


def _account(session: Session, ws: uuid.UUID, account_id: str, account_type: str) -> uuid.UUID:
    acc = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
            "VALUES (:i, :a, :w, :t, :a)"
        ),
        {"i": acc, "a": account_id, "w": ws, "t": account_type},
    )
    return acc


def _credential(session: Session, account: uuid.UUID, account_id: str, token: str) -> None:
    session.execute(
        text(
            "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
            "VALUES (:i, :a, :f, :h)"
        ),
        {"i": uuid.uuid4(), "a": account, "f": f"sha256:{account_id}", "h": token_hash(token)},
    )


def seed_population(engine: Engine, profile: Profile, tag: str | None = None) -> Population:
    """Create the profile's Workspace, Accounts, Agents, Channels, Bridges and Schedules."""
    tag = tag or f"load{uuid.uuid4().hex[:6]}"
    ws = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    owner_token = f"svc-{tag}-owner-{uuid.uuid4().hex[:8]}"
    ops_token = f"svc-{tag}-ops-{uuid.uuid4().hex[:8]}"
    pop = Population(
        tag=tag, ws=ws, owner_account=uuid.uuid4(), owner_token=owner_token, ops_token=ops_token
    )
    repo = PostgresPolicyRepository()
    with Session(engine) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :w)"),
            {"i": ws, "w": f"ws-{tag}"},
        )
        owner = _account(s, ws, f"acct-{tag}-owner", "human")
        pop.owner_account = owner
        _credential(s, owner, f"acct-{tag}-owner", owner_token)
        ops = _account(s, ws, f"acct-{tag}-ops", "human")
        _credential(s, ops, f"acct-{tag}-ops", ops_token)
        system = _account(s, ws, f"acct-{tag}-system", "service")

        writer_role, agent_role, ops_role = (
            f"role-{tag}-writer",
            f"role-{tag}-agent",
            f"role-{tag}-ops",
        )
        for role, permissions, holder in (
            (writer_role, WRITER_ROLE_PERMISSIONS, owner),
            (ops_role, OPS_ROLE_PERMISSIONS, ops),
        ):
            repo.create_role(s, ws, role, role)
            repo.commit_role_version(s, role, permissions, [], {}, owner)
            repo.assign_role(s, holder, role, owner, now - dt.timedelta(minutes=1))
        repo.create_role(s, ws, agent_role, agent_role)
        repo.commit_role_version(s, agent_role, AGENT_ROLE_PERMISSIONS, [], {}, owner)
        system_role = f"role-{tag}-system"
        repo.create_role(s, ws, system_role, system_role)
        repo.commit_role_version(s, system_role, SYSTEM_ROLE_PERMISSIONS, [], {}, owner)
        repo.assign_role(s, system, system_role, owner, now - dt.timedelta(minutes=1))

        provider = uuid.uuid4()
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, "
                "provider, base_url, team_or_bot_ref, identity_display) VALUES (:i, :p, :w, "
                "'mattermost', 'http://mm.invalid', :t, 'prefix')"
            ),
            {"i": provider, "p": f"mm:{tag}", "w": ws, "t": f"team-{tag}"},
        )
        telegram = uuid.uuid4()
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, "
                "provider, base_url, team_or_bot_ref) VALUES (:i, :p, :w, 'telegram', NULL, :t)"
            ),
            {"i": telegram, "p": f"tg:{tag}", "w": ws, "t": f"bot-{tag}"},
        )

        for i in range(profile.humans):
            human = _account(s, ws, f"acct-{tag}-h{i:03d}", "human")
            repo.assign_role(s, human, writer_role, owner, now - dt.timedelta(minutes=1))
        for i in range(profile.agents):
            account = _account(s, ws, f"acct-{tag}-a{i:03d}", "agent")
            repo.assign_role(s, account, agent_role, owner, now - dt.timedelta(minutes=1))
            s.execute(
                text(
                    "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, "
                    "status, display_name, online, capacity, last_heartbeat_at) VALUES "
                    "(:i, :g, :w, :a, 'mcp', 'active', :g, true, 50, :now)"
                ),
                {
                    "i": uuid.uuid4(),
                    "g": f"agent-{tag}-{i:03d}",
                    "w": ws,
                    "a": account,
                    "now": now,
                },
            )
        for i in range(profile.channels):
            channel = uuid.uuid4()
            channel_id = f"chan-{tag}-{i:03d}"
            s.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                    "external_channel_id, channel_type, display_name) VALUES "
                    "(:i, :c, :w, :p, :e, 'work', :c)"
                ),
                {
                    "i": channel,
                    "c": channel_id,
                    "w": ws,
                    "p": provider,
                    "e": f"mmchan-{tag}-{i:03d}",
                },
            )
            pop.channel_ids.append(channel_id)
            pop.channel_uuids.append(channel)
            for account in (owner, ops, system):
                s.execute(
                    text(
                        "INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"c": channel, "a": account},
                )
        for i in range(profile.bridges):
            s.execute(
                text(
                    "INSERT INTO telegram_bridges (id, bridge_id, workspace_id, channel_id, "
                    "provider_instance_id, telegram_chat_id, direction, thread_mode, status, "
                    "created_by, created_at, updated_at) VALUES (:i, :b, :w, :c, :p, :chat, "
                    "'bidirectional', 'topic_per_root', 'enabled', :o, :now, :now)"
                ),
                {
                    "i": uuid.uuid4(),
                    "b": f"bridge-{tag}-{i:02d}",
                    "w": ws,
                    "c": pop.channel_uuids[i % len(pop.channel_uuids)],
                    "p": telegram,
                    "chat": f"-100{i:07d}",
                    "o": owner,
                    "now": now,
                },
            )
    _create_schedules(engine, pop, profile, now)
    return pop


def _create_schedules(engine: Engine, pop: Population, profile: Profile, now: dt.datetime) -> None:
    """Schedules on a five-minute cycle, offset so ``due_per_minute`` fall due each minute."""
    if profile.schedules <= 0:
        return
    template = {
        "schema_id": "action-template.v1",
        "action": "task_create",
        "input": {"title": "load", "domain": "research", "risk": "LOW"},
    }
    selection = {"mode": "capability", "required_capabilities": ["cap-load"]}
    with Session(engine) as s, s.begin():
        for i in range(profile.schedules):
            schedule_id = f"sch-{pop.tag}-{i:03d}"
            version_id = f"schv-{uuid.uuid4().hex[:16]}"
            row = uuid.uuid4()
            offset = i % 5
            s.execute(
                text(
                    "INSERT INTO schedules (id, schedule_id, workspace_id, name, status, "
                    "created_by, created_at, updated_at, last_planned_until) VALUES "
                    "(:i, :s, :w, :n, 'ENABLED', :o, :now, :now, :now)"
                ),
                {
                    "i": row,
                    "s": schedule_id,
                    "w": pop.ws,
                    "n": f"load {i:03d}",
                    "o": pop.owner_account,
                    "now": now,
                },
            )
            version_row = uuid.uuid4()
            s.execute(
                text(
                    "INSERT INTO schedule_versions (id, schedule_version_id, schedule_id, "
                    "version, name, channel_id, cron_expression, timezone, "
                    "execution_principal_id, agent_selection, action_template, "
                    "concurrency_policy, missed_run_policy, backfill_limit, "
                    "backfill_window_seconds, max_duration_seconds, min_interval_minutes, "
                    "retry_policy, budget_policy, documentation_policy, snapshot_hash, "
                    "created_by, created_at) VALUES (:i, :v, :s, 1, :n, :c, :cron, 'UTC', :p, "
                    "CAST(:sel AS jsonb), CAST(:tpl AS jsonb), 'ALLOW', 'SKIP', 0, 0, 3600, 1, "
                    "CAST(:retry AS jsonb), CAST(:budget AS jsonb), CAST(:doc AS jsonb), :h, "
                    ":o, :now)"
                ),
                {
                    "i": version_row,
                    "v": version_id,
                    "s": schedule_id,
                    "n": f"load {i:03d}",
                    "c": pop.channel_uuids[i % len(pop.channel_uuids)],
                    "cron": f"{offset}-59/5 * * * *",
                    "p": pop.owner_account,
                    "sel": json.dumps(selection),
                    "tpl": json.dumps(template),
                    "retry": json.dumps({"max_attempts": 3, "backoff_seconds": [1, 5, 25]}),
                    "budget": json.dumps(
                        {"per_run_cost_units": 1000, "daily_cost_units": 10_000_000}
                    ),
                    "doc": json.dumps({"draft": False}),
                    "h": hashlib.sha256(schedule_id.encode()).hexdigest(),
                    "o": pop.owner_account,
                    "now": now,
                },
            )
            s.execute(
                text("UPDATE schedules SET current_version_id = :v WHERE schedule_id = :s"),
                {"v": version_row, "s": schedule_id},
            )
            pop.schedule_ids.append(schedule_id)
