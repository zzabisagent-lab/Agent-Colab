"""Shared seeding for the Phase 6 brainstorm tests (P6-02/P6-09).

One workspace with a channel, a facilitator, three Agent participants and one Agent that is not a
participant but can summarize — the population V-P6-26 and V-P6-27 need.
"""

from __future__ import annotations

import datetime as dt
import hashlib
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

T0 = dt.datetime(2026, 4, 6, 9, 0, tzinfo=dt.UTC)
AGENT_NAMES = ("alpha", "beta", "gamma")
OUTSIDER = "delta"


class Seed:
    """Workspace, channel, facilitator and four Agents (three seated, one outsider)."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.ws = uuid.uuid4()
        self.channel = uuid.uuid4()
        self.channel_id = f"chan-{tag}"
        self.provider = uuid.uuid4()
        self.artifact_storage: Any = None
        self.accounts: dict[str, uuid.UUID] = {}
        self.agent_ids: dict[str, str] = {}

    # ---- names --------------------------------------------------------------------------
    @property
    def facilitator(self) -> str:
        return f"acct-{self.tag}-facil"

    @property
    def human(self) -> str:
        return f"acct-{self.tag}-human"

    def agent_account(self, name: str) -> str:
        return f"acct-{self.tag}-{name}"

    # ---- seeding ------------------------------------------------------------------------
    def create(self, engine: Engine) -> None:
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :w)"),
                {"i": self.ws, "w": f"ws-{self.tag}"},
            )
            c.execute(
                text(
                    "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, "
                    "provider, base_url, team_or_bot_ref) VALUES (:i, :p, :w, 'mattermost', "
                    "'http://mm.test', 'team')"
                ),
                {"i": self.provider, "p": f"mm:{self.tag}", "w": self.ws},
            )
            c.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, channel_type, "
                    "display_name, provider_instance_id, external_channel_id) "
                    "VALUES (:i, :c, :w, 'work', :c, :p, :e)"
                ),
                {
                    "i": self.channel,
                    "c": self.channel_id,
                    "w": self.ws,
                    "p": self.provider,
                    "e": f"ext-{self.tag}",
                },
            )
            for name in (self.facilitator, self.human):
                self.add_account(c, name, "human")
                self.join_channel(c, name)
            for name in (*AGENT_NAMES, OUTSIDER):
                self.add_agent(c, name)

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

    def add_agent(self, c: Any, name: str) -> None:
        account = self.agent_account(name)
        acc = self.add_account(c, account, "agent")
        agent_id = f"agent-{self.tag}-{name}"
        self.agent_ids[name] = agent_id
        c.execute(
            text(
                "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, "
                "status, display_name, online, capacity) VALUES (:i, :g, :w, :a, 'mcp', 'active', "
                ":g, true, 3)"
            ),
            {"i": uuid.uuid4(), "g": agent_id, "w": self.ws, "a": acc},
        )
        self.join_channel(c, account)

    def spend(self, engine: Engine, brainstorm_id: str, cost_units: int, at: dt.datetime) -> None:
        """Record Agent usage against the session so the budget limit can be exercised."""
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO pricing_versions (pricing_version, table_json, table_sha256, "
                    "activated_at) VALUES (:p, CAST(:r AS jsonb), :h, :n) ON CONFLICT DO NOTHING"
                ),
                {
                    "p": f"pricing-{self.tag}",
                    "r": json.dumps({"default": 1}),
                    "h": hashlib.sha256(f"pricing-{self.tag}".encode()).hexdigest(),
                    "n": at,
                },
            )
            c.execute(
                text(
                    "INSERT INTO usage_records (workspace_id, agent_id, account_id, "
                    "brainstorm_id, model, input_tokens, output_tokens, tool_calls, wall_ms, "
                    "cost_units, source, pricing_version, reported_at) VALUES (:w, :g, :a, "
                    ":b, 'sim-1', 10, 5, 1, 40, :cu, 'reported', :p, :n)"
                ),
                {
                    "p": f"pricing-{self.tag}",
                    "w": self.ws,
                    "g": self.agent_ids[AGENT_NAMES[0]],
                    "a": self.accounts[self.agent_account(AGENT_NAMES[0])],
                    "b": brainstorm_id,
                    "cu": cost_units,
                    "n": at,
                },
            )

    # ---- command execution --------------------------------------------------------------
    def principal(self, name: str) -> Principal:
        typ = (
            "agent"
            if name.startswith(f"acct-{self.tag}-")
            and name
            not in (
                self.facilitator,
                self.human,
            )
            else "human"
        )
        return Principal(name, str(self.accounts[name]), typ, f"fp-{name}")

    def ctx(self, session: Session, who: str, key: str, clock: Clock) -> CommandContext:
        extras: dict[str, Any] = {}
        if self.artifact_storage is not None:
            extras["artifact_storage"] = self.artifact_storage
        return CommandContext(
            session=session,
            store=PostgresEventStore(session, clock=clock),
            authorizer=AllowAllAuthorizer(),
            clock=clock,
            principal=self.principal(who),
            workspace_id=str(self.ws),
            correlation_id=f"corr-{self.tag}",
            idempotency_key=key,
            extras=extras,
        )

    def run(self, engine: Engine, cmd: Any, who: str, key: str, clock: Clock) -> CommandResult:
        with Session(engine) as s, s.begin():
            return execute(cmd, self.ctx(s, who, key, clock))

    def run_expect(self, engine: Engine, cmd: Any, who: str, key: str, clock: Clock) -> str:
        try:
            self.run(engine, cmd, who, key, clock)
        except bus.CommandError as exc:
            return exc.code
        return "OK"

    def read(self, engine: Engine, fn: Any, *args: Any, clock: Clock | None = None) -> Any:
        with Session(engine) as s:
            return fn(self.ctx(s, self.facilitator, "read", clock or FixedClock(T0)), *args)


def event_types(engine: Engine, aggregate_id: str) -> list[str]:
    with Session(engine) as s:
        return [
            str(r[0])
            for r in s.execute(
                text("SELECT type FROM events WHERE aggregate_id = :a ORDER BY aggregate_seq"),
                {"a": aggregate_id},
            ).all()
        ]


def work_items(engine: Engine, brainstorm_id: str) -> list[dict[str, Any]]:
    with Session(engine) as s:
        return [
            dict(r)
            for r in s.execute(
                text(
                    "SELECT work_item_id, agent_id, kind, status FROM work_items "
                    "WHERE brainstorm_id = :b ORDER BY created_at, work_item_id"
                ),
                {"b": brainstorm_id},
            )
            .mappings()
            .all()
        ]


def guidance_requests(engine: Engine, brainstorm_id: str) -> list[dict[str, Any]]:
    with Session(engine) as s:
        return [
            dict(r)
            for r in s.execute(
                text(
                    "SELECT dedupe_key, payload FROM delivery_outbox WHERE kind = 'notification' "
                    "AND payload->>'brainstorm_id' = :b ORDER BY id"
                ),
                {"b": brainstorm_id},
            )
            .mappings()
            .all()
        ]
