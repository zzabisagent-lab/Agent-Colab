"""Shared seed for the Phase 4 admin tests (accounts, ops, audit, hard delete, backup/restore)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application.authz import BusAuthorizer
from server.application.bus import Command, CommandResult
from server.db.engine import make_session_factory
from server.domain.clock import Clock
from server.identity.principals import Principal, token_hash
from server.policy.repository import PostgresPolicyRepository
from server.secrets.envelope import EnvelopeCrypto, MasterKey, new_master_key
from server.security import reauth

T0 = dt.datetime(2026, 9, 3, 9, 0, tzinfo=dt.UTC)
# role assignments valid for real and virtual clocks
VALID_FROM = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
ADMIN_PERMS = [
    "admin.accounts",
    "admin.audit",
    "admin.settings",
    "admin.hard_delete",
    "approval.decide",
    "approval.request",
    "task.*",
]


@dataclass
class Seed:
    ws: uuid.UUID
    prefix: str
    accounts: dict[str, uuid.UUID]
    tokens: dict[str, str] = field(repr=False)  # never in test output/evidence
    master_key_b64: str = field(repr=False)

    def principal(self, name: str, account_type: str = "human") -> Principal:
        return Principal(
            f"acct-{self.prefix}-{name}",
            str(self.accounts[name]),
            account_type,
            f"sha256:acct-{self.prefix}-{name}",
        )

    def crypto(self) -> EnvelopeCrypto:
        return EnvelopeCrypto(MasterKey.from_b64("mk-test", self.master_key_b64))

    def runtime(self, engine: Engine, clock: Clock) -> Runtime:
        return Runtime(
            make_session_factory(engine), BusAuthorizer(), self.crypto(), clock, str(self.ws)
        )

    def headers(self, name: str, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[name]}", "Idempotency-Key": key}


def seed(
    engine: Engine,
    prefix: str,
    *,
    humans: tuple[str, ...] = ("admin1", "admin2", "admin3", "member"),
) -> Seed:
    """Workspace + three administrators (admin1..3), one plain member, one service Account."""
    ws = uuid.uuid4()
    accounts: dict[str, uuid.UUID] = {}
    tokens: dict[str, str] = {}
    with Session(engine) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :w)"),
            {"i": ws, "w": f"ws-{prefix}"},
        )
        for name in (*humans, "svc"):
            acc = uuid.uuid4()
            accounts[name] = acc
            typ = "service" if name == "svc" else "human"
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": f"acct-{prefix}-{name}", "w": ws, "t": typ},
            )
            tok = f"svc-{prefix}-{name}-{uuid.uuid4().hex[:8]}"
            tokens[name] = tok
            s.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                    "VALUES (:i, :a, :f, :h)"
                ),
                {
                    "i": uuid.uuid4(),
                    "a": acc,
                    "f": f"sha256:acct-{prefix}-{name}",
                    "h": token_hash(tok),
                },
            )
        repo = PostgresPolicyRepository()
        repo.create_role(s, ws, f"role-{prefix}-admin", "admin")
        repo.commit_role_version(s, f"role-{prefix}-admin", ADMIN_PERMS, [], {}, accounts["admin1"])
        repo.create_role(s, ws, f"role-{prefix}-member", "member")
        repo.commit_role_version(
            s, f"role-{prefix}-member", ["task.read"], [], {}, accounts["admin1"]
        )
        for name in humans:
            role = f"role-{prefix}-admin" if name.startswith("admin") else f"role-{prefix}-member"
            repo.assign_role(s, accounts[name], role, accounts["admin1"], VALID_FROM)
    return Seed(ws, prefix, accounts, tokens, new_master_key())


def run(rt: Runtime, principal: Principal, cmd: Command, key: str, **extras: Any) -> CommandResult:
    return execute_command(
        rt, principal, cmd, idempotency_key=key, correlation_id=f"corr-{key}", extras=extras
    )


def install_reauth(*account_uuids: uuid.UUID, at: dt.datetime | None = None) -> None:
    """Fake MFA verifier: the listed accounts have a fresh re-authentication proof."""
    allowed = {str(a) for a in account_uuids}

    def verifier(account_uuid: str, session_id: str | None) -> reauth.ReauthProof | None:
        if account_uuid not in allowed:
            return None
        return reauth.ReauthProof(account_uuid, at or dt.datetime.now(dt.UTC), "totp", session_id)

    reauth.set_verifier(verifier)


def clear_reauth() -> None:
    reauth.set_verifier(None)


def audit_actions(engine: Engine, ws: uuid.UUID, target_id: str) -> list[str]:
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT action FROM audit_events WHERE workspace_id = :w AND target_id = :t "
                "ORDER BY id"
            ),
            {"w": ws, "t": target_id},
        ).all()
    return [str(r[0]) for r in rows]
