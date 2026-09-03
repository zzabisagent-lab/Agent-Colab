"""Shared seed for the Phase 4 security tests: a Workspace with an Owner, an Administrator, a
Member, an Agent and a service Account, service tokens for each, an ops channel, default-ish roles
built with the policy repository, and a real app (``create_app``) with a master key."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.identity.principals import token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository
from server.secrets.envelope import new_master_key

OWNER_PERMS = [
    "admin.*",
    "ops.*",
    "task.*",
    "approval.*",
    "verification.*",
    "artifact.*",
    "channel.*",
    "agent.*",
    "events.*",
    "notification.self",
    "identity.link",
    "work.poll",
]
ADMIN_PERMS = [
    "admin.settings",
    "admin.accounts",
    "admin.audit",
    "agent.manage",
    "channel.manage",
    "task.*",
    "approval.*",
    "verification.*",
]
MEMBER_PERMS = [
    "task.read",
    "task.list",
    "task.create",
    "task.delegate",
    "approval.request",
    "approval.read",
]
AGENT_PERMS = ["task.*", "approval.*", "work.poll", "verification.submit"]


@dataclass
class Seed:
    ws: uuid.UUID = field(default_factory=uuid.uuid4)
    channel: uuid.UUID = field(default_factory=uuid.uuid4)
    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    prefix: str = ""

    def account_id(self, who: str) -> str:
        return f"acct-{self.prefix}-{who}"

    def bearer(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


def seed(engine: Engine, prefix: str) -> Seed:
    s = Seed(prefix=prefix)
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    with Session(engine) as db, db.begin():
        db.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :n)"),
            {"i": s.ws, "w": f"ws-{prefix}", "n": prefix},
        )
        members = (
            ("owner", "human", OWNER_PERMS, "role-system-owner"),
            ("admin", "human", ADMIN_PERMS, "role-administrator"),
            ("member", "human", MEMBER_PERMS, f"role-{prefix}-member"),
            ("member2", "human", OWNER_PERMS, f"role-{prefix}-owner2"),
            ("agent", "agent", AGENT_PERMS, f"role-{prefix}-agent"),
            ("system", "service", OWNER_PERMS, f"role-{prefix}-system"),
        )
        repo = PostgresPolicyRepository()
        for who, typ, _perms, _role in members:
            acc = uuid.uuid4()
            s.ids[who] = acc
            tok = f"svc-{prefix}-{who}-{uuid.uuid4().hex[:12]}"
            s.tokens[who] = tok
            db.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": s.account_id(who), "w": s.ws, "t": typ},
            )
            db.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                    "VALUES (:i, :a, :f, :h)"
                ),
                {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{prefix}-{who}", "h": token_hash(tok)},
            )
        db.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, :c, :w, 'ops', 'ops')"
            ),
            {"i": s.channel, "c": f"chan-{prefix}-ops", "w": s.ws},
        )
        for who, _typ, perms, role in members:
            db.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": s.channel, "a": s.ids[who]},
            )
            existing = db.execute(
                text("SELECT 1 FROM roles WHERE role_id = :r"), {"r": role}
            ).first()
            if existing is None:
                repo.create_role(db, s.ws, role, role)
                repo.commit_role_version(
                    db, role, perms, [], {"max_risk": "CRITICAL"}, s.ids["owner"]
                )
            repo.assign_role(db, s.ids[who], role, s.ids["owner"], now)
    return s


def make_app(database_url: str, base_url: str = "http://127.0.0.1:8080") -> FastAPI:
    os.environ["AGENT_COLAB_GATEWAY_DRAIN"] = "0"
    return create_app(
        Settings(database_url=database_url, base_url=base_url, master_key_b64=new_master_key())
    )


def login(client: Any, token: str) -> None:
    r = client.post("/api/v1/auth/sessions", json={"service_token": token})
    assert r.status_code == 201, r.text


def csrf(client: Any) -> dict[str, str]:
    r = client.get("/api/v1/auth/csrf")
    assert r.status_code == 200
    return {"X-CSRF-Token": r.json()["csrf_token"]}
