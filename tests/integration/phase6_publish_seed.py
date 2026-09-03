"""Shared seed for the P6-03/P6-06/P6-07 tests: workspace, accounts, channel, task, document."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application.bus import CommandContext, Principal
from server.artifacts.storage import ArtifactStorage
from server.documents.store import DocumentStore
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.identity.principals import token_hash

T0 = dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.UTC)


class AllowAll:
    """Test authorizer: records the permissions asked for, allows everything."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def require(self, session: Session, principal: str, permission: str, **scope: Any) -> None:
        self.asked.append(permission)


class DenyPermissions:
    """Denies the named permissions, allows the rest (V-P6-18 unauthorized publisher)."""

    def __init__(self, *denied: str) -> None:
        self.denied = frozenset(denied)

    def require(self, session: Session, principal: str, permission: str, **scope: Any) -> None:
        from server.application.bus import CommandError

        if permission in self.denied:
            raise CommandError("POLICY_DENIED", permission, 403)


class Seed:
    """One workspace with the accounts, channel, task and document these tests need."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.ws = uuid.uuid4()
        self.other_ws = uuid.uuid4()
        self.accounts: dict[str, uuid.UUID] = {}
        self.tokens: dict[str, str] = {}
        self.task_id = f"task-{tag}"
        self.document_id = f"doc-{tag}"
        self.clock = FixedClock(T0)

    # ------------------------------------------------------------------ seeding
    def install(self, engine: Engine) -> Seed:
        with Session(engine) as s, s.begin():
            for ws, name in ((self.ws, f"ws-{self.tag}"), (self.other_ws, f"ws-{self.tag}-other")):
                s.execute(
                    text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :w)"),
                    {"i": ws, "w": name},
                )
            for role, kind, ws in (
                ("uploader", "human", self.ws),
                ("reader", "human", self.ws),
                ("stranger", "human", self.ws),
                ("publisher", "human", self.ws),
                ("reviewer", "human", self.ws),
                ("agent", "agent", self.ws),
                ("outsider", "human", self.other_ws),
            ):
                account = uuid.uuid4()
                self.accounts[role] = account
                public = f"acct-{self.tag}-{role}"
                s.execute(
                    text(
                        "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                        "display_name) VALUES (:i, :a, :w, :t, :a)"
                    ),
                    {"i": account, "a": public, "w": ws, "t": kind},
                )
                token = f"svc-{self.tag}-{role}-0001"
                self.tokens[role] = token
                s.execute(
                    text(
                        "INSERT INTO service_credentials (id, account_id, fingerprint, "
                        "token_hash) VALUES (:i, :a, :f, :h)"
                    ),
                    {
                        "i": uuid.uuid4(),
                        "a": account,
                        "f": f"sha256:{public}",
                        "h": token_hash(token),
                    },
                )
            channel = uuid.uuid4()
            s.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, channel_type, "
                    "display_name) VALUES (:i, :c, :w, 'work', :c)"
                ),
                {"i": channel, "c": f"chan-{self.tag}", "w": self.ws},
            )
            self.channel = channel
            s.execute(
                text(
                    "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, "
                    "channel_id, title, domain, risk, status, assignee_account_id, "
                    "created_at, updated_at) VALUES (:t, :w, :t, :c, 'seeded', 'research', 'LOW', "
                    "'OPEN', :a, :n, :n)"
                ),
                {
                    "t": self.task_id,
                    "w": self.ws,
                    "c": channel,
                    "a": self.accounts["reader"],
                    "n": T0,
                },
            )
        return self

    def context(
        self,
        session: Session,
        role: str,
        idem: str,
        *,
        storage: ArtifactStorage | None = None,
        authorizer: Any | None = None,
        extras: dict[str, Any] | None = None,
    ) -> CommandContext:
        account = self.accounts[role]
        return CommandContext(
            session=session,
            store=PostgresEventStore(session, clock=self.clock),
            authorizer=authorizer or AllowAll(),
            clock=self.clock,
            principal=Principal(
                f"acct-{self.tag}-{role}",
                str(account),
                "agent" if role == "agent" else "human",
                f"sha256:acct-{self.tag}-{role}",
            ),
            workspace_id=str(self.ws),
            correlation_id=f"corr-{self.tag}",
            idempotency_key=idem,
            extras={**({"artifact_storage": storage} if storage else {}), **(extras or {})},
        )

    # ------------------------------------------------------------------ documents
    def finalized_document(
        self, engine: Engine, store: DocumentStore, version: int = 1, body: str | None = None
    ) -> tuple[int, str]:
        """Write a FINALIZED document version to the canonical store and DB. Returns (v, sha)."""
        markdown = body or f"# {self.document_id} v{version}\n\nCanonical body for publishing.\n"
        manifest = {
            "document_id": self.document_id,
            "version": version,
            "title": f"{self.document_id} v{version}",
            "sources": [{"type": "task", "id": self.task_id}],
        }
        store.write_version(str(self.ws), self.document_id, version, markdown, manifest)
        sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        with Session(engine) as s, s.begin():
            event_id = self._seed_event(s, version)
            exists = s.execute(
                text("SELECT 1 FROM documents WHERE document_id = :d"), {"d": self.document_id}
            ).first()
            if exists is None:
                s.execute(
                    text(
                        "INSERT INTO documents (id, document_id, workspace_id, doc_type, "
                        "source_type, source_id, current_version, status) VALUES (:i, :d, :w, "
                        "'task', 'task', :t, :v, 'FINALIZED')"
                    ),
                    {
                        "i": uuid.uuid4(),
                        "d": self.document_id,
                        "w": self.ws,
                        "t": self.task_id,
                        "v": version,
                    },
                )
            else:
                s.execute(
                    text("UPDATE documents SET current_version = :v WHERE document_id = :d"),
                    {"v": version, "d": self.document_id},
                )
            s.execute(
                text(
                    "INSERT INTO document_versions (id, document_id, version, status, "
                    "storage_uri, sha256, manifest, source_freeze_event_seq, event_id) "
                    "VALUES (:i, :d, :v, 'FINALIZED', :uri, :sha, CAST(:m AS jsonb), 1, :e)"
                ),
                {
                    "i": uuid.uuid4(),
                    "d": self.document_id,
                    "v": version,
                    "uri": store.uri(str(self.ws), self.document_id, version),
                    "sha": sha,
                    "m": json.dumps(manifest, sort_keys=True),
                    "e": event_id,
                },
            )
        return version, sha

    def seed_event(self, session: Session, event_type: str, aggregate_id: str) -> str:
        """An Event row other tables can reference by ``event_id``."""
        from server.events.store import AppendRequest

        aggregate = "brainstorm" if event_type.startswith("BRAINSTORM") else "document"
        result = PostgresEventStore(session, clock=self.clock).append(
            AppendRequest(
                workspace_id=str(self.ws),
                aggregate_type=aggregate,
                aggregate_id=aggregate_id,
                type=event_type,
                actor_account_id=str(self.accounts["publisher"]),
                correlation_id=f"corr-{self.tag}",
                idempotency_scope=f"{aggregate}:seed",
                idempotency_key=f"{aggregate_id}-{event_type}",
                payload=self._payload(event_type, aggregate_id),
            )
        )
        return result.event_id

    def _payload(self, event_type: str, aggregate_id: str) -> dict[str, Any]:
        if event_type == "BRAINSTORM_OPENED":
            return {
                "brainstorm_id": aggregate_id,
                "channel_id": f"chan-{self.tag}",
                "topic": "links",
                "facilitator_account_id": f"acct-{self.tag}-publisher",
                "limits": {},
            }
        return {"document_id": aggregate_id}

    def _seed_event(self, session: Session, version: int) -> str:
        """A DOCUMENT_FINALIZED Event the version row can reference."""
        store = PostgresEventStore(session, clock=self.clock)
        from server.events.store import AppendRequest

        result = store.append(
            AppendRequest(
                workspace_id=str(self.ws),
                aggregate_type="document",
                aggregate_id=self.document_id,
                type="DOCUMENT_FINALIZED",
                actor_account_id=str(self.accounts["publisher"]),
                correlation_id=f"corr-{self.tag}",
                idempotency_scope="document:finalize",
                idempotency_key=f"{self.document_id}-v{version}",
                payload={
                    "document_id": self.document_id,
                    "version": version,
                    "verification_id": f"vr-{self.tag}",
                    "sha256": hashlib.sha256(str(version).encode()).hexdigest(),
                },
            )
        )
        return result.event_id


def bare_git_remote(path: Path) -> str:
    """Initialise a bare repository with an empty ``main`` branch, usable as a push remote."""
    import subprocess

    git = "git"
    bare = path / "remote.git"
    seed = path / "seed"
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(path),
        "GIT_AUTHOR_NAME": "Seed",
        "GIT_COMMITTER_NAME": "Seed",
        "GIT_AUTHOR_EMAIL": "seed@localhost",
        "GIT_COMMITTER_EMAIL": "seed@localhost",
    }

    def run(args: list[str], cwd: Path) -> None:
        proc = subprocess.run(
            [git, *args], cwd=str(cwd), capture_output=True, text=True, env=env, check=False
        )
        assert proc.returncode == 0, proc.stderr

    bare.mkdir(parents=True)
    run(["init", "--bare", "--initial-branch=main", "."], bare)
    seed.mkdir(parents=True)
    run(["init", "--initial-branch=main", "."], seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    run(["add", "-A"], seed)
    run(["commit", "-m", "seed"], seed)
    run(["remote", "add", "origin", str(bare)], seed)
    run(["push", "origin", "main"], seed)
    return str(bare)
