"""Persistence for external identity links and challenges (SQL and in-memory implementations)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field, replace
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ProviderInstance:
    id: uuid.UUID
    provider_instance_id: str
    workspace_id: uuid.UUID
    provider: str
    status: str


@dataclass(frozen=True)
class Link:
    id: uuid.UUID
    link_id: str
    provider_instance: uuid.UUID
    external_user_id: str
    account_uuid: uuid.UUID
    account_id: str
    verification_method: str
    status: str
    verified_at: dt.datetime | None


@dataclass(frozen=True)
class Challenge:
    id: uuid.UUID
    provider_instance: uuid.UUID
    external_user_id: str
    code_hash: str
    account_uuid: uuid.UUID | None
    expires_at: dt.datetime
    used_at: dt.datetime | None
    failures: int
    locked_until: dt.datetime | None


class IdentityRepository(Protocol):
    def provider_instance(self, provider_instance_id: str) -> ProviderInstance | None: ...
    def instance_by_uuid(self, instance_uuid: uuid.UUID) -> ProviderInstance | None: ...
    def account_uuid(self, account_id: str) -> uuid.UUID | None: ...
    def account_id(self, account_uuid: uuid.UUID) -> str | None: ...
    def get_link(self, provider_instance: uuid.UUID, external_user_id: str) -> Link | None: ...
    def get_link_by_id(self, link_id: str) -> Link | None: ...
    def insert_link(self, link: Link, now: dt.datetime) -> None: ...
    def update_link(self, link: Link, now: dt.datetime) -> None: ...
    def latest_challenge(
        self, provider_instance: uuid.UUID, external_user_id: str
    ) -> Challenge | None: ...
    def insert_challenge(self, challenge: Challenge) -> None: ...
    def update_challenge(self, challenge: Challenge) -> None: ...


class SqlIdentityRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def provider_instance(self, provider_instance_id: str) -> ProviderInstance | None:
        r = self.s.execute(
            text(
                "SELECT id, provider_instance_id, workspace_id, provider, status "
                "FROM provider_instances WHERE provider_instance_id = :p"
            ),
            {"p": provider_instance_id},
        ).first()
        return None if r is None else ProviderInstance(r[0], r[1], r[2], r[3], r[4])

    def instance_by_uuid(self, instance_uuid: uuid.UUID) -> ProviderInstance | None:
        r = self.s.execute(
            text(
                "SELECT id, provider_instance_id, workspace_id, provider, status "
                "FROM provider_instances WHERE id = :i"
            ),
            {"i": instance_uuid},
        ).first()
        return None if r is None else ProviderInstance(r[0], r[1], r[2], r[3], r[4])

    def account_uuid(self, account_id: str) -> uuid.UUID | None:
        r = self.s.execute(
            text("SELECT id FROM accounts WHERE account_id = :a AND status = 'ACTIVE'"),
            {"a": account_id},
        ).first()
        return None if r is None else uuid.UUID(str(r[0]))

    def account_id(self, account_uuid: uuid.UUID) -> str | None:
        r = self.s.execute(
            text("SELECT account_id FROM accounts WHERE id = :i"), {"i": account_uuid}
        ).first()
        return None if r is None else str(r[0])

    def _row_to_link(self, r: tuple[object, ...]) -> Link:
        return Link(
            uuid.UUID(str(r[0])),
            str(r[1]),
            uuid.UUID(str(r[2])),
            str(r[3]),
            uuid.UUID(str(r[4])),
            str(r[5]),
            str(r[6]),
            str(r[7]),
            r[8],  # type: ignore[arg-type]
        )

    _LINK_SELECT = (
        "SELECT l.id, l.link_id, l.provider_instance_id, l.external_user_id, l.account_id, "
        "a.account_id, l.verification_method, l.status, l.verified_at "
        "FROM external_identity_links l "
        "JOIN accounts a ON a.id = l.account_id "
    )

    def get_link(self, provider_instance: uuid.UUID, external_user_id: str) -> Link | None:
        r = self.s.execute(
            text(
                self._LINK_SELECT + "WHERE l.provider_instance_id = :p AND l.external_user_id = :u"
            ),
            {"p": provider_instance, "u": external_user_id},
        ).first()
        return None if r is None else self._row_to_link(tuple(r))

    def get_link_by_id(self, link_id: str) -> Link | None:
        r = self.s.execute(text(self._LINK_SELECT + "WHERE l.link_id = :l"), {"l": link_id}).first()
        return None if r is None else self._row_to_link(tuple(r))

    def insert_link(self, link: Link, now: dt.datetime) -> None:
        self.s.execute(
            text(
                "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                "external_user_id, account_id, verification_method, status, verified_at, "
                "created_at, updated_at) VALUES "
                "(:id, :link_id, :p, :u, :acc, :m, :st, :v, :now, :now)"
            ),
            {
                "id": link.id,
                "link_id": link.link_id,
                "p": link.provider_instance,
                "u": link.external_user_id,
                "acc": link.account_uuid,
                "m": link.verification_method,
                "st": link.status,
                "v": link.verified_at,
                "now": now,
            },
        )

    def update_link(self, link: Link, now: dt.datetime) -> None:
        self.s.execute(
            text(
                "UPDATE external_identity_links SET account_id = :acc, verification_method = :m, "
                "status = :st, verified_at = :v, updated_at = :now WHERE id = :id"
            ),
            {
                "acc": link.account_uuid,
                "m": link.verification_method,
                "st": link.status,
                "v": link.verified_at,
                "now": now,
                "id": link.id,
            },
        )

    def latest_challenge(
        self, provider_instance: uuid.UUID, external_user_id: str
    ) -> Challenge | None:
        r = self.s.execute(
            text(
                "SELECT id, provider_instance_id, external_user_id, code_hash, account_id, "
                "expires_at, used_at, failures, locked_until FROM identity_link_challenges "
                "WHERE provider_instance_id = :p AND external_user_id = :u "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"p": provider_instance, "u": external_user_id},
        ).first()
        if r is None:
            return None
        return Challenge(
            uuid.UUID(str(r[0])),
            uuid.UUID(str(r[1])),
            str(r[2]),
            str(r[3]),
            uuid.UUID(str(r[4])) if r[4] else None,
            r[5],
            r[6],
            int(r[7]),
            r[8],
        )

    def insert_challenge(self, challenge: Challenge) -> None:
        self.s.execute(
            text(
                "INSERT INTO identity_link_challenges (id, provider_instance_id, external_user_id, "
                "code_hash, account_id, expires_at, used_at, failures, locked_until, created_at) "
                "VALUES (:id, :p, :u, :h, :acc, :exp, :used, :f, :lock, :created)"
            ),
            {
                "id": challenge.id,
                "p": challenge.provider_instance,
                "u": challenge.external_user_id,
                "h": challenge.code_hash,
                "acc": challenge.account_uuid,
                "exp": challenge.expires_at,
                "used": challenge.used_at,
                "f": challenge.failures,
                "lock": challenge.locked_until,
                "created": challenge.expires_at - dt.timedelta(minutes=10),
            },
        )

    def update_challenge(self, challenge: Challenge) -> None:
        self.s.execute(
            text(
                "UPDATE identity_link_challenges SET used_at = :used, failures = :f, "
                "locked_until = :lock, account_id = :acc WHERE id = :id"
            ),
            {
                "used": challenge.used_at,
                "f": challenge.failures,
                "lock": challenge.locked_until,
                "acc": challenge.account_uuid,
                "id": challenge.id,
            },
        )


@dataclass
class InMemoryIdentityRepository:
    """Unit-test repository. Enforces the same uniqueness rules as the schema."""

    instances: dict[str, ProviderInstance] = field(default_factory=dict)
    accounts: dict[str, uuid.UUID] = field(default_factory=dict)
    links: dict[str, Link] = field(default_factory=dict)
    challenges: list[Challenge] = field(default_factory=list)

    def add_instance(
        self, provider_instance_id: str, workspace_id: uuid.UUID, provider: str = "mattermost"
    ) -> ProviderInstance:
        inst = ProviderInstance(
            uuid.uuid4(), provider_instance_id, workspace_id, provider, "active"
        )
        self.instances[provider_instance_id] = inst
        return inst

    def add_account(self, account_id: str) -> uuid.UUID:
        self.accounts[account_id] = uuid.uuid4()
        return self.accounts[account_id]

    def provider_instance(self, provider_instance_id: str) -> ProviderInstance | None:
        return self.instances.get(provider_instance_id)

    def instance_by_uuid(self, instance_uuid: uuid.UUID) -> ProviderInstance | None:
        return next((i for i in self.instances.values() if i.id == instance_uuid), None)

    def account_uuid(self, account_id: str) -> uuid.UUID | None:
        return self.accounts.get(account_id)

    def account_id(self, account_uuid: uuid.UUID) -> str | None:
        return next((k for k, v in self.accounts.items() if v == account_uuid), None)

    def get_link(self, provider_instance: uuid.UUID, external_user_id: str) -> Link | None:
        return next(
            (
                lk
                for lk in self.links.values()
                if lk.provider_instance == provider_instance
                and lk.external_user_id == external_user_id
            ),
            None,
        )

    def get_link_by_id(self, link_id: str) -> Link | None:
        return self.links.get(link_id)

    def insert_link(self, link: Link, now: dt.datetime) -> None:
        if self.get_link(link.provider_instance, link.external_user_id) is not None:
            raise ValueError("UNIQUE(provider_instance_id, external_user_id)")
        self.links[link.link_id] = link

    def update_link(self, link: Link, now: dt.datetime) -> None:
        self.links[link.link_id] = replace(link)

    def latest_challenge(
        self, provider_instance: uuid.UUID, external_user_id: str
    ) -> Challenge | None:
        matches = [
            c
            for c in self.challenges
            if c.provider_instance == provider_instance and c.external_user_id == external_user_id
        ]
        return matches[-1] if matches else None

    def insert_challenge(self, challenge: Challenge) -> None:
        self.challenges.append(challenge)

    def update_challenge(self, challenge: Challenge) -> None:
        self.challenges = [challenge if c.id == challenge.id else c for c in self.challenges]
