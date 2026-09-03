"""Shared seed for the Secret Broker tests: one Workspace, an administrator, two Agents (mcp and
mattermost_bot), a channel and roles with the secret permissions."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.agents import registry as reg
from server.api.dispatch import Runtime, execute_command
from server.application import agents as ag
from server.application.authz import BusAuthorizer
from server.db.engine import make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import Principal
from server.policy.repository import PostgresPolicyRepository
from server.secrets.envelope import MasterKey, new_master_key
from server.usage.versions import activate_from_file

T0 = dt.datetime(2026, 9, 1, 8, 0, tzinfo=dt.UTC)
MASTER = MasterKey.from_b64("mk-test-1", new_master_key())


class Seed:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.ws = uuid.uuid4()
        self.admin = uuid.uuid4()
        self.channel = uuid.uuid4()
        self.admin_p = Principal(f"acct-{tag}-admin", str(self.admin), "human", f"sha256:{tag}")
        self.approver = uuid.uuid4()
        self.approver2 = uuid.uuid4()
        self.approver2_p = Principal(
            f"acct-{tag}-approver2",
            str(self.approver2),
            "human",
            f"sha256:{tag}-approver2",
            mfa_verified=True,
        )
        self.approver_p = Principal(  # a re-authenticated Human (HIGH-risk decisions need MFA)
            f"acct-{tag}-approver",
            str(self.approver),
            "human",
            f"sha256:{tag}-approver",
            mfa_verified=True,
        )
        self.agents: dict[str, Principal] = {}

    def create(self, engine: Engine) -> None:
        with Session(engine) as s, s.begin():
            s.execute(
                text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :w)"),
                {"i": self.ws, "w": f"ws-{self.tag}"},
            )
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, 'human', 'Admin')"
                ),
                {"i": self.admin, "a": f"acct-{self.tag}-admin", "w": self.ws},
            )
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, 'human', 'Approver')"
                ),
                {"i": self.approver, "a": f"acct-{self.tag}-approver", "w": self.ws},
            )
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, 'human', 'Approver 2')"
                ),
                {"i": self.approver2, "a": f"acct-{self.tag}-approver2", "w": self.ws},
            )
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, 'service', 'system')"
                ),
                {"i": uuid.uuid4(), "a": f"acct-{self.tag}-system", "w": self.ws},
            )
            s.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, channel_type, "
                    "display_name) VALUES (:i, :c, :w, 'work', 'sec')"
                ),
                {"i": self.channel, "c": f"chan-{self.tag}", "w": self.ws},
            )
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": self.channel, "a": self.admin},
            )
            repo = PostgresPolicyRepository()
            repo.create_role(s, self.ws, f"role-{self.tag}-admin", "admin")
            repo.commit_role_version(
                s,
                f"role-{self.tag}-admin",
                ["agent.manage", "task.*", "secret.*", "approval.*", "artifact.*", "document.*"],
                [],
                {},
                self.admin,
            )
            repo.assign_role(s, self.admin, f"role-{self.tag}-admin", self.admin, T0)
            repo.assign_role(s, self.approver, f"role-{self.tag}-admin", self.admin, T0)
            repo.assign_role(s, self.approver2, f"role-{self.tag}-admin", self.admin, T0)
            repo.create_role(s, self.ws, f"role-{self.tag}-worker", "worker")
            repo.commit_role_version(
                s,
                f"role-{self.tag}-worker",
                ["agent.self", "task.*", "work.poll", "secret.lease", "artifact.*"],
                [],
                {},
                self.admin,
            )
        with Session(engine) as s, s.begin():
            activate_from_file(s)

    def runtime(self, engine: Engine, clock: FixedClock) -> Runtime:
        return Runtime(make_session_factory(engine), BusAuthorizer(), None, clock, str(self.ws))

    def run(self, rt: Runtime, principal: Principal, cmd: Any, key: str) -> Any:
        return execute_command(
            rt,
            principal,
            cmd,
            idempotency_key=f"{self.tag}-{key}",
            correlation_id=f"corr-{self.tag}",
            extras={"master_key": MASTER},
        )

    def ensure_agent(self, engine: Engine, rt: Runtime, agent_id: str) -> Principal:
        """The Agent principal, registering it when this test runs on its own."""
        return self.agents.get(agent_id) or self.register_agent(engine, rt, agent_id)

    def register_agent(
        self, engine: Engine, rt: Runtime, agent_id: str, adapter_type: str = "mcp"
    ) -> Principal:
        self.run(
            rt,
            self.admin_p,
            ag.RegisterAgent(
                agent_id,
                agent_id,
                adapter_type,
                roles=(f"role-{self.tag}-worker",),
                channel_ids=(f"chan-{self.tag}",),
                endpoint={"provider_instance_id": "mm:x", "bot_user_id": "b"}
                if adapter_type == "mattermost_bot"
                else {},
            ),
            f"reg-{agent_id}",
        )
        self.run(
            rt,
            self.admin_p,
            ag.ActivateAgent(
                agent_id, probe={"identity_hash": f"id-{agent_id}", "capabilities": []}
            ),
            f"act-{agent_id}",
        )
        with Session(engine) as s:
            row = reg.load_agent(s, self.ws, agent_id)
            assert row is not None
        p = Principal(row.account_public_id, str(row.account_id), "agent", f"sha256:{agent_id}")
        self.agents[agent_id] = p
        return p
