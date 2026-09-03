"""Shared seeding for the Phase 5 schedule core tests (workspace, accounts, channel, agent)."""

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
from server.domain.clock import Clock, FixedClock
from server.events.postgres_store import PostgresEventStore

T0 = dt.datetime(2026, 3, 2, 8, 0, tzinfo=dt.UTC)  # a Monday
ACTION_TEMPLATE: dict[str, Any] = {
    "schema_id": "action-template.v1",
    "action": "task_create",
    "input": {"title": "Scheduled digest", "domain": "research", "risk": "LOW"},
}
AGENT_SELECTION: dict[str, Any] = {"mode": "capability", "required_capabilities": ["cap-research"]}


class Seed:
    """One workspace with a channel, a human owner, a system service Account and an Agent."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.ws = uuid.uuid4()
        self.channel = uuid.uuid4()
        self.channel_id = f"chan-{tag}"
        self.accounts: dict[str, uuid.UUID] = {}
        self.agent_id = f"agent-{tag}"

    # ---- seeding ------------------------------------------------------------------------
    def create(self, engine: Engine) -> None:
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :w)"),
                {"i": self.ws, "w": f"ws-{self.tag}"},
            )
            self.add_account(c, self.owner, "human")
            self.add_account(c, self.system, "service")
            self.add_account(c, self.member, "human")
            c.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, channel_type, "
                    "display_name) VALUES (:i, :c, :w, 'work', :c)"
                ),
                {"i": self.channel, "c": self.channel_id, "w": self.ws},
            )
            for name in (self.owner, self.system, self.member):
                self.join_channel(c, name)
            self.add_agent(c)

    @property
    def owner(self) -> str:
        return f"acct-{self.tag}-owner"

    @property
    def system(self) -> str:
        return f"acct-{self.tag}-system"

    @property
    def member(self) -> str:
        return f"acct-{self.tag}-member"

    def add_account(self, c: Any, name: str, typ: str) -> uuid.UUID:
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
            {"i": uuid.uuid4(), "a": acc, "f": f"fp-{name}", "h": f"hash-{name}"},
        )
        return acc

    def join_channel(self, c: Any, name: str) -> None:
        c.execute(
            text(
                "INSERT INTO channel_members (channel_id, account_id, permissions) "
                "VALUES (:c, :a, CAST(:p AS jsonb))"
            ),
            {"c": self.channel, "a": self.accounts[name], "p": json.dumps(["read", "write"])},
        )

    def add_agent(self, c: Any) -> None:
        name = f"acct-{self.tag}-agent"
        acc = self.add_account(c, name, "agent")
        c.execute(
            text(
                "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, status, "
                "display_name, online, capacity) VALUES (:i, :g, :w, :a, 'mcp', 'active', :g, "
                "true, 3)"
            ),
            {"i": uuid.uuid4(), "g": self.agent_id, "w": self.ws, "a": acc},
        )
        c.execute(
            text(
                "INSERT INTO capabilities (id, capability_id, tool, domain) "
                "VALUES (:i, 'cap-research', 'task_create', 'research') "
                "ON CONFLICT (capability_id) DO NOTHING"
            ),
            {"i": uuid.uuid4()},
        )
        c.execute(
            text("INSERT INTO agent_capabilities (agent_id, capability_id) VALUES (:g, :c)"),
            {"g": self.agent_id, "c": "cap-research"},
        )
        self.join_channel(c, name)

    # ---- command execution --------------------------------------------------------------
    def principal(self, name: str) -> Principal:
        typ = "service" if name.endswith("system") else "human"
        return Principal(name, str(self.accounts[name]), typ, f"fp-{name}")

    def ctx(
        self,
        session: Session,
        who: str,
        key: str,
        clock: Clock,
        extras: dict[str, Any] | None = None,
    ) -> CommandContext:
        return CommandContext(
            session=session,
            store=PostgresEventStore(session, clock=clock),
            authorizer=AllowAllAuthorizer(),
            clock=clock,
            principal=self.principal(who),
            workspace_id=str(self.ws),
            correlation_id=f"corr-{self.tag}",
            idempotency_key=key,
            extras=extras or {},
        )

    def run(
        self,
        engine: Engine,
        cmd: Any,
        who: str,
        key: str,
        clock: Clock,
        *,
        extras: dict[str, Any] | None = None,
    ) -> CommandResult:
        with Session(engine) as s, s.begin():
            return execute(cmd, self.ctx(s, who, key, clock, extras))

    def run_expect(self, engine: Engine, cmd: Any, who: str, key: str, clock: Clock) -> str:
        try:
            self.run(engine, cmd, who, key, clock)
        except bus.CommandError as exc:
            return exc.code
        return "OK"

    def read(
        self, engine: Engine, fn: Any, *args: Any, clock: Clock | None = None, **kw: Any
    ) -> Any:
        with Session(engine) as s:
            return fn(self.ctx(s, self.owner, "read", clock or FixedClock(T0)), *args, **kw)


def event_types(engine: Engine, aggregate_id: str) -> list[str]:
    with Session(engine) as s:
        return [
            str(r[0])
            for r in s.execute(
                text("SELECT type FROM events WHERE aggregate_id = :a ORDER BY aggregate_seq"),
                {"a": aggregate_id},
            ).all()
        ]


def run_rows(engine: Engine, schedule_id: str) -> list[dict[str, Any]]:
    with Session(engine) as s:
        return [
            dict(r._mapping)
            for r in s.execute(
                text(
                    "SELECT run_id, run_kind, occurrence_key, scheduled_for, status, "
                    "version_hash, planner_note, retry_of_run_id FROM schedule_runs "
                    "WHERE schedule_id = :s ORDER BY scheduled_for, run_id"
                ),
                {"s": schedule_id},
            ).all()
        ]
