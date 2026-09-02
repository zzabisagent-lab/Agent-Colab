"""External identity link lifecycle (development plan §6.5, §7A.5; spec §9.2, §10.2; V-P1-23).

A link binds one (provider instance, external user) to exactly one Account. Only an ``active``
link grants command permissions; ``pending``/``pending_admin``/``suspended``/``revoked`` links
yield zero side effects. Every transition appends an Event through the ``EventStore`` protocol
and an audit row. Challenge codes: 10-minute TTL, single-use, hashed at rest, 5 failures within
the window lock the (instance, user) for 15 minutes (§21.1).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from server.domain import defaults
from server.domain.clock import Clock, SystemClock, isoformat_utc
from server.events.store import AppendRequest, EventStore
from server.identity.principals import IdentityError, Principal
from server.identity.repository import Challenge, IdentityRepository, Link, SqlIdentityRepository
from server.observability.audit import append_audit

AGGREGATE = "external_identity_link"
ACTIVE = "active"
NON_REVOKED = ("pending", "pending_admin", "active", "suspended")


def link_id_for(provider_instance_id: str, external_user_id: str) -> str:
    """Deterministic aggregate id so Events can precede the link row (challenge stage)."""
    digest = hashlib.sha256(f"{provider_instance_id}|{external_user_id}".encode()).hexdigest()
    return "link-" + digest[:24]


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChallengeIssued:
    link_id: str
    code: str  # returned exactly once to be delivered by DM; never stored
    expires_at: dt.datetime


@dataclass
class ExternalLinkService:
    """Service over a repository, an event store, and an optional SQL session for audit rows."""

    repo: IdentityRepository
    store: EventStore
    session: Session | None = None
    clock: Clock | None = None

    # ------------------------------------------------------------ helpers
    def _now(self) -> dt.datetime:
        return (self.clock or SystemClock()).now()

    def _audit(
        self,
        action: str,
        target_id: str,
        result: str,
        actor: str,
        correlation_id: str,
        workspace_id: uuid.UUID,
        error_code: str | None = None,
        **meta: object,
    ) -> None:
        if self.session is not None:
            append_audit(
                self.session,
                action=action,
                target_type="external_identity_link",
                target_id=target_id,
                result=result,
                actor_label=actor,
                correlation_id=correlation_id,
                workspace_id=workspace_id,
                error_code=error_code,
                metadata=dict(meta),
                clock=self.clock,
            )

    def _event(
        self,
        workspace_id: uuid.UUID,
        link_id: str,
        event_type: str,
        actor_account_uuid: uuid.UUID,
        correlation_id: str,
        operation: str,
        payload: dict[str, object],
    ) -> str:
        result = self.store.append(
            AppendRequest(
                workspace_id=str(workspace_id),
                aggregate_type=AGGREGATE,
                aggregate_id=link_id,
                type=event_type,
                actor_account_id=str(actor_account_uuid),
                correlation_id=correlation_id,
                idempotency_scope=f"{AGGREGATE}:{operation}",
                idempotency_key=f"{link_id}:{operation}:{isoformat_utc(self._now())}:{uuid.uuid4().hex[:8]}",
                payload=payload,
            )
        )
        return result.event_id

    def _instance(self, provider_instance_id: str) -> tuple[uuid.UUID, uuid.UUID]:
        inst = self.repo.provider_instance(provider_instance_id)
        if inst is None or inst.status != "active":
            raise IdentityError("PROVIDER_INSTANCE_UNKNOWN", provider_instance_id)
        return inst.id, inst.workspace_id

    # ------------------------------------------------------------ challenge
    def start_challenge(
        self,
        provider_instance_id: str,
        external_user_id: str,
        *,
        actor_account_uuid: uuid.UUID,
        correlation_id: str,
    ) -> ChallengeIssued:
        inst_uuid, ws = self._instance(provider_instance_id)
        existing = self.repo.get_link(inst_uuid, external_user_id)
        if existing is not None and existing.status in NON_REVOKED:
            self._audit(
                "identity.link_challenge",
                existing.link_id,
                "DENY",
                str(actor_account_uuid),
                correlation_id,
                ws,
                error_code="EXTERNAL_IDENTITY_DUPLICATE",
            )
            raise IdentityError("EXTERNAL_IDENTITY_DUPLICATE", "a non-revoked link already exists")
        now = self._now()
        latest = self.repo.latest_challenge(inst_uuid, external_user_id)
        if latest is not None and latest.locked_until is not None and latest.locked_until > now:
            raise IdentityError("EXTERNAL_IDENTITY_LOCKED", "too many failures; retry later")
        code = f"{secrets.randbelow(10**8):08d}"
        expires = now + dt.timedelta(minutes=defaults.LINK_CHALLENGE_TTL_MIN)
        self.repo.insert_challenge(
            Challenge(
                uuid.uuid4(),
                inst_uuid,
                external_user_id,
                _code_hash(code),
                None,
                expires,
                None,
                0,
                None,
            )
        )
        link_id = link_id_for(provider_instance_id, external_user_id)
        self._event(
            ws,
            link_id,
            "IDENTITY_LINK_CHALLENGED",
            actor_account_uuid,
            correlation_id,
            "challenge",
            {
                "link_id": link_id,
                "provider_instance_id": provider_instance_id,
                "expires_at": isoformat_utc(expires),
            },
        )
        self._audit(
            "identity.link_challenge",
            link_id,
            "OK",
            str(actor_account_uuid),
            correlation_id,
            ws,
            provider_instance_id=provider_instance_id,
        )
        return ChallengeIssued(link_id, code, expires)

    def _check_code(self, inst_uuid: uuid.UUID, external_user_id: str, code: str) -> Challenge:
        now = self._now()
        ch = self.repo.latest_challenge(inst_uuid, external_user_id)
        if ch is None:
            raise IdentityError("EXTERNAL_IDENTITY_CHALLENGE_INVALID", "no challenge")
        if ch.locked_until is not None and ch.locked_until > now:
            raise IdentityError("EXTERNAL_IDENTITY_LOCKED", "locked after repeated failures")
        if ch.used_at is not None:
            raise IdentityError("EXTERNAL_IDENTITY_CHALLENGE_USED", "code already used")
        if not hmac.compare_digest(ch.code_hash, _code_hash(code)):
            failures = ch.failures + 1
            locked = None
            if failures > defaults.LINK_CHALLENGE_MAX_FAILURES:  # 15-minute lockout from the 6th
                locked = now + dt.timedelta(minutes=defaults.LINK_CHALLENGE_LOCKOUT_MIN)
            self.repo.update_challenge(replace(ch, failures=failures, locked_until=locked))
            code_err = (
                "EXTERNAL_IDENTITY_LOCKED" if locked else "EXTERNAL_IDENTITY_CHALLENGE_INVALID"
            )
            raise IdentityError(code_err, f"wrong code ({failures} failures)")
        if ch.expires_at <= now:
            raise IdentityError("EXTERNAL_IDENTITY_CHALLENGE_EXPIRED", "code expired")
        return ch

    def confirm_challenge(
        self,
        provider_instance_id: str,
        external_user_id: str,
        code: str,
        account_id: str,
        *,
        path: str,
        actor_account_uuid: uuid.UUID,
        correlation_id: str,
    ) -> Link:
        """``path='web'`` → active/signed_challenge; ``path='command'`` → pending_admin."""
        if path not in ("web", "command"):
            raise IdentityError("EXTERNAL_IDENTITY_PATH_INVALID", path)
        inst_uuid, ws = self._instance(provider_instance_id)
        account_uuid = self.repo.account_uuid(account_id)
        if account_uuid is None:
            raise IdentityError("ACCOUNT_NOT_FOUND", account_id)
        ch = self._check_code(inst_uuid, external_user_id, code)
        now = self._now()
        existing = self.repo.get_link(inst_uuid, external_user_id)
        if existing is not None and existing.status in NON_REVOKED:
            raise IdentityError("EXTERNAL_IDENTITY_DUPLICATE", "a non-revoked link already exists")
        link_id = link_id_for(provider_instance_id, external_user_id)
        status = ACTIVE if path == "web" else "pending_admin"
        method = "signed_challenge" if path == "web" else "admin_approval"
        link = Link(
            existing.id if existing else uuid.uuid4(),
            link_id,
            inst_uuid,
            external_user_id,
            account_uuid,
            account_id,
            method,
            status,
            now if status == ACTIVE else None,
        )
        self.repo.update_challenge(replace(ch, used_at=now, account_uuid=account_uuid))
        if existing is None:
            self.repo.insert_link(link, now)
        else:
            self.repo.update_link(link, now)
        if status == ACTIVE:
            self._event(
                ws,
                link_id,
                "IDENTITY_LINK_VERIFIED",
                actor_account_uuid,
                correlation_id,
                "verify",
                {"link_id": link_id, "account_id": account_id, "verification_method": method},
            )
        self._audit(
            "identity.link_confirm",
            link_id,
            "OK",
            str(actor_account_uuid),
            correlation_id,
            ws,
            status=status,
            method=method,
        )
        return link

    def approve_pending_link(
        self, link_id: str, *, admin_account_uuid: uuid.UUID, correlation_id: str
    ) -> Link:
        link = self.repo.get_link_by_id(link_id)
        if link is None:
            raise IdentityError("EXTERNAL_IDENTITY_NOT_FOUND", link_id)
        if link.status != "pending_admin":
            raise IdentityError("EXTERNAL_IDENTITY_TRANSITION_INVALID", f"{link.status} -> active")
        ws = self._workspace_of_link(link)
        now = self._now()
        updated = replace(link, status=ACTIVE, verified_at=now)
        self.repo.update_link(updated, now)
        self._event(
            ws,
            link_id,
            "IDENTITY_LINK_VERIFIED",
            admin_account_uuid,
            correlation_id,
            "verify",
            {
                "link_id": link_id,
                "account_id": link.account_id,
                "verification_method": "admin_approval",
            },
        )
        self._audit(
            "identity.link_approve", link_id, "OK", str(admin_account_uuid), correlation_id, ws
        )
        return updated

    def _workspace_of_link(self, link: Link) -> uuid.UUID:
        inst = self.repo.instance_by_uuid(link.provider_instance)
        if inst is None:
            raise IdentityError("PROVIDER_INSTANCE_UNKNOWN", str(link.provider_instance))
        return inst.workspace_id

    def _transition(
        self,
        link_id: str,
        new_status: str,
        reason_code: str,
        event_type: str,
        operation: str,
        allowed_from: tuple[str, ...],
        *,
        actor_account_uuid: uuid.UUID,
        correlation_id: str,
    ) -> Link:
        link = self.repo.get_link_by_id(link_id)
        if link is None:
            raise IdentityError("EXTERNAL_IDENTITY_NOT_FOUND", link_id)
        if link.status not in allowed_from:
            raise IdentityError(
                "EXTERNAL_IDENTITY_TRANSITION_INVALID", f"{link.status} -> {new_status}"
            )
        ws = self._workspace_of_link(link)
        now = self._now()
        updated = replace(link, status=new_status)
        self.repo.update_link(updated, now)
        self._event(
            ws,
            link_id,
            event_type,
            actor_account_uuid,
            correlation_id,
            operation,
            {"link_id": link_id, "reason_code": reason_code},
        )
        self._audit(
            f"identity.link_{operation}",
            link_id,
            "OK",
            str(actor_account_uuid),
            correlation_id,
            ws,
            reason_code=reason_code,
        )
        return updated

    def suspend_link(
        self, link_id: str, reason_code: str, *, actor_account_uuid: uuid.UUID, correlation_id: str
    ) -> Link:
        return self._transition(
            link_id,
            "suspended",
            reason_code,
            "IDENTITY_LINK_SUSPENDED",
            "suspend",
            ("active", "pending_admin", "pending"),
            actor_account_uuid=actor_account_uuid,
            correlation_id=correlation_id,
        )

    def revoke_link(
        self, link_id: str, reason_code: str, *, actor_account_uuid: uuid.UUID, correlation_id: str
    ) -> Link:
        return self._transition(
            link_id,
            "revoked",
            reason_code,
            "IDENTITY_LINK_REVOKED",
            "revoke",
            ("active", "suspended", "pending_admin", "pending"),
            actor_account_uuid=actor_account_uuid,
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------ command principal (read-only)
    def resolve_command_principal(
        self, provider_instance_id: str, external_user_id: str
    ) -> Principal:
        """Principal for an ACTIVE link; any other state is a stable error with no side effects."""
        inst = self.repo.provider_instance(provider_instance_id)
        if inst is None or inst.status != "active":
            raise IdentityError("EXTERNAL_IDENTITY_NOT_ACTIVE", "unknown provider instance")
        link = self.repo.get_link(inst.id, external_user_id)
        if link is None or link.status != ACTIVE:
            raise IdentityError(
                "EXTERNAL_IDENTITY_NOT_ACTIVE", "no active link" if link is None else link.status
            )
        if self.repo.account_uuid(link.account_id) is None:
            raise IdentityError("EXTERNAL_IDENTITY_NOT_ACTIVE", "account not active")
        return Principal(
            account_id=link.account_id,
            account_uuid=str(link.account_uuid),
            account_type="human",
            credential_fingerprint="sha256:"
            + hashlib.sha256(f"link:{link.link_id}".encode()).hexdigest(),
            credential_kind="external_link",
        )


def sql_service(
    session: Session, store: EventStore, clock: Clock | None = None
) -> ExternalLinkService:
    return ExternalLinkService(SqlIdentityRepository(session), store, session, clock)
