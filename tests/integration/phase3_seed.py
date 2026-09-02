"""Shared seeding for the Phase 3 orchestration tests (workspace, accounts, agents, channel)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import bus
from server.application.authz import AllowAllAuthorizer
from server.application.bus import CommandContext, CommandResult, Principal, execute
from server.domain.clock import Clock
from server.events.postgres_store import PostgresEventStore

CRITERIA = ({"statement": "evidence attached", "check_type": "evidence"},)


class Seed:
    """One workspace with a channel, a human delegator, a system service Account and Agents."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.ws = uuid.uuid4()
        self.channel = uuid.uuid4()
        self.channel_id = f"chan-{tag}"
        self.accounts: dict[str, uuid.UUID] = {}
        self.agents: dict[str, str] = {}  # public account id -> agent_id

    def account(self, name: str) -> uuid.UUID:
        return self.accounts[name]

    def principal(self, name: str) -> Principal:
        typ = "agent" if name in self.agents else ("service" if "system" in name else "human")
        return Principal(name, str(self.accounts[name]), typ, f"fp-{name}", self.agents.get(name))

    def create(self, engine: Engine, template_limits: dict[str, Any] | None = None) -> None:
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :w)"),
                {"i": self.ws, "w": f"ws-{self.tag}"},
            )
            self.add_account(c, f"acct-{self.tag}-human", "human")
            self.add_account(c, f"acct-{self.tag}-system", "service")
            template_id = None
            if template_limits is not None:
                template_id = f"tpl-{self.tag}"
                definition = {
                    "default_roles": ["worker"],
                    "task_domain": "research",
                    "risk_policy": {"default_risk": "LOW", "max_risk": "CRITICAL"},
                    "limits": template_limits,
                    "documentation_template": "task-skeleton-v1",
                    "documentation_policy": {"auto_draft": True},
                    "retention_days": 365,
                    "telegram_commands": {"allow_commands": False},
                    "secret_scope": [],
                    "artifact_policy": {},
                }
                c.execute(
                    text(
                        "INSERT INTO channel_templates (workspace_id, template_id, name, "
                        "channel_type, definition, protected, version, status) VALUES (:w, :t, "
                        ":t, 'work', CAST(:d AS jsonb), false, 1, 'active')"
                    ),
                    {"w": self.ws, "t": template_id, "d": json.dumps(definition)},
                )
            c.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, channel_type, "
                    "display_name, template_id) VALUES (:i, :c, :w, 'work', :c, :t)"
                ),
                {"i": self.channel, "c": self.channel_id, "w": self.ws, "t": template_id},
            )
            self.join_channel(c, f"acct-{self.tag}-human")
            self.join_channel(c, f"acct-{self.tag}-system")

    def add_account(self, c: Any, name: str, typ: str, fingerprint: str | None = None) -> uuid.UUID:
        acc = uuid.uuid4()
        self.accounts[name] = acc
        c.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, :a, :w, :t, :a)"
            ),
            {"i": acc, "a": name, "w": self.ws, "t": typ},
        )
        c.execute(
            text(
                "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                "VALUES (:i, :a, :f, :h)"
            ),
            {"i": uuid.uuid4(), "a": acc, "f": fingerprint or f"fp-{name}", "h": f"hash-{name}"},
        )
        return acc

    def join_channel(
        self, c: Any, name: str, permissions: tuple[str, ...] = ("read", "write")
    ) -> None:
        c.execute(
            text(
                "INSERT INTO channel_members (channel_id, account_id, permissions) "
                "VALUES (:c, :a, CAST(:p AS jsonb))"
            ),
            {"c": self.channel, "a": self.accounts[name], "p": json.dumps(list(permissions))},
        )

    def add_agent(
        self,
        c: Any,
        name: str,
        *,
        status: str = "active",
        online: bool = True,
        capacity: int = 2,
        member: bool = True,
        capabilities: tuple[tuple[str, str | None], ...] = (("cap-research", "research"),),
        adapter_type: str = "mcp",
        fingerprint: str | None = None,
        limits: dict[str, Any] | None = None,
    ) -> str:
        acc = self.add_account(c, name, "agent", fingerprint)
        agent_id = f"agent-{name.removeprefix('acct-')}"
        self.agents[name] = agent_id
        c.execute(
            text(
                "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, status, "
                "display_name, online, capacity, limits) VALUES (:i, :g, :w, :a, :t, :s, :g, :o, "
                ":cap, CAST(:l AS jsonb))"
            ),
            {
                "i": uuid.uuid4(),
                "g": agent_id,
                "w": self.ws,
                "a": acc,
                "t": adapter_type,
                "s": status,
                "o": online,
                "cap": capacity,
                "l": json.dumps(limits or {}),
            },
        )
        for cap_id, domain in capabilities:
            c.execute(
                text(
                    "INSERT INTO capabilities (id, capability_id, tool, domain) "
                    "VALUES (:i, :c, :c, :d) ON CONFLICT (capability_id) DO NOTHING"
                ),
                {"i": uuid.uuid4(), "c": cap_id, "d": domain},
            )
            c.execute(
                text("INSERT INTO agent_capabilities (agent_id, capability_id) VALUES (:g, :c)"),
                {"g": agent_id, "c": cap_id},
            )
        if member:
            self.join_channel(c, name)
        return agent_id

    # ---- command execution -------------------------------------------------------------
    def run(
        self,
        engine: Engine,
        cmd: Any,
        who: Principal,
        key: str,
        clock: Clock,
        *,
        extras: dict[str, Any] | None = None,
        authorizer: Any = None,
    ) -> CommandResult:
        with Session(engine) as s, s.begin():
            ctx = CommandContext(
                session=s,
                store=PostgresEventStore(s, clock=clock),
                authorizer=authorizer or AllowAllAuthorizer(),
                clock=clock,
                principal=who,
                workspace_id=str(self.ws),
                correlation_id=f"corr-{self.tag}",
                idempotency_key=key,
                extras=extras or {},
            )
            return execute(cmd, ctx)

    def run_expect(self, engine: Engine, cmd: Any, who: Principal, key: str, clock: Clock) -> str:
        try:
            self.run(engine, cmd, who, key, clock)
        except bus.CommandError as exc:
            return exc.code
        return "OK"


def status_of(engine: Engine, task_id: str) -> str:
    with Session(engine) as s:
        return str(
            s.execute(
                text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
            ).scalar()
        )


def event_count(engine: Engine, task_id: str) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM events WHERE task_id = :t"), {"t": task_id}
            ).scalar_one()
        )


def event_types(engine: Engine, aggregate_id: str) -> list[str]:
    with Session(engine) as s:
        return [
            str(r[0])
            for r in s.execute(
                text("SELECT type FROM events WHERE aggregate_id = :t ORDER BY recorded_seq"),
                {"t": aggregate_id},
            )
        ]


def utc(y: int, m: int, d: int, h: int = 0) -> dt.datetime:
    return dt.datetime(y, m, d, h, tzinfo=dt.UTC)
