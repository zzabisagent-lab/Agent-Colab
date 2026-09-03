"""Break-glass (spec §4.4; P4-10).

Activation needs the System Owner (``admin.break_glass``), a valid single-use recovery code and a
TOTP code (MFA re-authentication). The session is time-limited (``security.breakglass_ttl_s``,
default 60 minutes), every request carrying ``X-Break-Glass-Session`` is recorded and audited,
activation and termination are announced in the ops channel immediately, and termination or
expiry opens an automatic post-hoc verification Task for an independent Verifier. Nothing here
relaxes Event immutability (DB triggers) or allows plaintext secret reads.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.application import bus
from server.domain.clock import Clock
from server.events.store import AppendRequest, EventStore, EventStoreError
from server.notifications.outbox import enqueue as enqueue_notification
from server.observability.audit import append_audit
from server.security import policy as secpolicy

POSTHOC_CRITERIA = (
    {
        "statement": "justification and every action of the break-glass session reviewed",
        "check_type": "human_attest",
        "required": True,
    },
)


def _iso_ms(when: dt.datetime) -> str:
    """Event contract timestamp form: UTC, milliseconds, ``Z`` suffix."""
    when = when.astimezone(dt.UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


class BreakGlassError(bus.CommandError):
    pass


@dataclass(frozen=True)
class BreakGlassSession:
    session_id: str
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    scope: str
    reason: str
    started_at: dt.datetime
    expires_at: dt.datetime
    ended_at: dt.datetime | None
    ended_reason: str | None
    posthoc_task_id: str | None

    @property
    def active(self) -> bool:
        return self.ended_at is None


def _load(session: Session, session_id: str, *, for_update: bool = False) -> BreakGlassSession:
    stmt = (
        "SELECT session_id, workspace_id, account_id, scope, reason, started_at, expires_at, "
        "ended_at, ended_reason, posthoc_task_id FROM breakglass_sessions WHERE session_id = :s"
    )
    row = session.execute(
        text(stmt + (" FOR UPDATE" if for_update else "")), {"s": session_id}
    ).first()
    if row is None:
        raise BreakGlassError("BREAK_GLASS_NOT_FOUND", session_id, status=404)
    return BreakGlassSession(*row)


def load(session: Session, session_id: str) -> BreakGlassSession:
    return _load(session, session_id)


def active_session_for(session: Session, account_uuid: str, now: dt.datetime) -> str | None:
    row = session.execute(
        text(
            "SELECT session_id FROM breakglass_sessions WHERE account_id = :a AND ended_at IS NULL "
            "AND expires_at > :now ORDER BY started_at DESC LIMIT 1"
        ),
        {"a": uuid.UUID(account_uuid), "now": now},
    ).first()
    return None if row is None else str(row[0])


def _announce(
    session: Session,
    workspace_id: uuid.UUID,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    now: dt.datetime,
) -> None:
    """Immediate ops-channel announcement through the notification outbox (no secret material)."""
    enqueue_notification(
        session,
        str(workspace_id),
        "notification",
        "mattermost:ops_channel",
        f"breakglass:{payload['session_id']}:{event_type}",
        {
            "event_type": event_type,
            "event_id": event_id,
            "session_id": payload["session_id"],
            "scope": payload.get("scope", {}).get("description")
            if isinstance(payload.get("scope"), dict)
            else None,
            "reason": payload.get("reason"),
            "message": f"[break-glass] {event_type} session {payload['session_id']}"
            + (f": {payload.get('ended_reason')}" if payload.get("ended_reason") else ""),
        },
        event_id,
        now,
    )


def activate(
    session: Session,
    store: EventStore,
    *,
    workspace_id: uuid.UUID,
    account_uuid: str,
    account_label: str,
    scope: str,
    reason: str,
    correlation_id: str,
    clock: Clock,
) -> BreakGlassSession:
    """Open a session (proofs were verified by the caller); Event + announcement + audit."""
    now = clock.now()
    if not scope.strip() or not reason.strip():
        raise BreakGlassError("BREAK_GLASS_SCOPE_REQUIRED", "scope and reason are mandatory", 400)
    if active_session_for(session, account_uuid, now) is not None:
        raise BreakGlassError("BREAK_GLASS_ALREADY_ACTIVE", "an active session exists", 409)
    ttl = secpolicy.int_value("security.breakglass_ttl_s")
    session_id = "bg-" + uuid.uuid4().hex[:20]
    expires = now + dt.timedelta(seconds=ttl)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "scope": {"description": scope},
        "reason": reason,
        "expires_at": _iso_ms(expires),
    }
    res = store.append(
        AppendRequest(
            workspace_id=str(workspace_id),
            aggregate_type="break_glass",
            aggregate_id=session_id,
            type="BREAK_GLASS_STARTED",
            actor_account_id=account_uuid,
            correlation_id=correlation_id,
            idempotency_scope="break_glass:start",
            idempotency_key=session_id,
            payload=payload,
        )
    )
    session.execute(
        text(
            "INSERT INTO breakglass_sessions (session_id, workspace_id, account_id, scope, reason, "
            "started_at, expires_at, start_event_id) VALUES (:s, :w, :a, :sc, :r, :now, :exp, :e)"
        ),
        {
            "s": session_id,
            "w": workspace_id,
            "a": uuid.UUID(account_uuid),
            "sc": scope,
            "r": reason,
            "now": now,
            "exp": expires,
            "e": res.event_id,
        },
    )
    append_audit(
        session,
        action="breakglass.activate",
        target_type="break_glass",
        target_id=session_id,
        result="OK",
        actor_label=account_label,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        actor_account_id=uuid.UUID(account_uuid),
        metadata={"scope": scope, "expires_at": expires.isoformat()},
        clock=clock,
    )
    _announce(session, workspace_id, res.event_id, "BREAK_GLASS_STARTED", payload, now)
    return _load(session, session_id)


def record_action(
    session: Session,
    session_id: str,
    *,
    method: str,
    path: str,
    status_code: int,
    correlation_id: str,
    now: dt.datetime,
) -> None:
    """Every request made under an active session is recorded and audited (spec §4.4)."""
    row = session.execute(
        text(
            "SELECT workspace_id, account_id, ended_at, expires_at FROM breakglass_sessions "
            "WHERE session_id = :s"
        ),
        {"s": session_id},
    ).first()
    if row is None:
        return
    active = row[2] is None and row[3] > now
    audit_id = append_audit(
        session,
        action="breakglass.action",
        target_type="break_glass",
        target_id=session_id,
        result="OK" if active else "IGNORED",
        actor_label=str(row[1]),
        correlation_id=correlation_id,
        workspace_id=row[0],
        actor_account_id=row[1],
        error_code=None if active else "BREAK_GLASS_INACTIVE",
        metadata={"method": method, "path": path, "status": status_code},
    )
    session.execute(
        text(
            "INSERT INTO breakglass_actions (session_id, occurred_at, method, path, status_code, "
            "correlation_id, audit_id) VALUES (:s, :now, :m, :p, :st, :c, :a)"
        ),
        {
            "s": session_id,
            "now": now,
            "m": method,
            "p": path,
            "st": status_code,
            "c": correlation_id,
            "a": audit_id,
        },
    )


def _end(
    session: Session,
    store: EventStore,
    bg: BreakGlassSession,
    *,
    ended_reason: str,
    actor_uuid: str,
    actor_label: str,
    correlation_id: str,
    clock: Clock,
) -> BreakGlassSession:
    now = clock.now()
    payload: dict[str, Any] = {"session_id": bg.session_id, "ended_reason": ended_reason}
    res = store.append(
        AppendRequest(
            workspace_id=str(bg.workspace_id),
            aggregate_type="break_glass",
            aggregate_id=bg.session_id,
            type="BREAK_GLASS_ENDED",
            actor_account_id=actor_uuid,
            correlation_id=correlation_id,
            idempotency_scope="break_glass:end",
            idempotency_key=bg.session_id,
            payload=payload,
        )
    )
    session.execute(
        text(
            "UPDATE breakglass_sessions SET ended_at = :now, ended_reason = :r, end_event_id = :e "
            "WHERE session_id = :s"
        ),
        {"now": now, "r": ended_reason, "e": res.event_id, "s": bg.session_id},
    )
    append_audit(
        session,
        action="breakglass.end",
        target_type="break_glass",
        target_id=bg.session_id,
        result="OK",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=bg.workspace_id,
        actor_account_id=uuid.UUID(actor_uuid),
        metadata={"ended_reason": ended_reason},
        clock=clock,
    )
    _announce(session, bg.workspace_id, res.event_id, "BREAK_GLASS_ENDED", payload, now)
    return _load(session, bg.session_id)


def open_posthoc(
    session: Session,
    store: EventStore,
    session_id: str,
    *,
    correlation_id: str,
    clock: Clock,
    runtime: Any,
) -> str | None:
    """Open the automatic post-hoc verification Task for an ended session.

    Runs in its own transaction *after* the ending transaction committed: policy evaluation inside
    Task creation / verifier assignment may audit denials independently, which must never wait on
    an audit-chain lock held by the caller.
    """
    bg = _load(session, session_id, for_update=True)
    if bg.active or bg.posthoc_task_id:
        return bg.posthoc_task_id
    task_id = _open_posthoc_verification(session, store, bg, correlation_id, clock, runtime)
    if task_id:
        session.execute(
            text("UPDATE breakglass_sessions SET posthoc_task_id = :t WHERE session_id = :s"),
            {"t": task_id, "s": bg.session_id},
        )
    return task_id


def terminate(
    session: Session,
    store: EventStore,
    session_id: str,
    *,
    actor_uuid: str,
    actor_label: str,
    correlation_id: str,
    clock: Clock,
) -> BreakGlassSession:
    bg = _load(session, session_id, for_update=True)
    if not bg.active:
        raise BreakGlassError("BREAK_GLASS_ENDED", bg.ended_reason or "ended", status=409)
    if str(bg.account_id) != actor_uuid:
        raise BreakGlassError("BREAK_GLASS_NOT_OWNER", "only the activating Owner terminates", 403)
    return _end(
        session,
        store,
        bg,
        ended_reason="TERMINATED",
        actor_uuid=actor_uuid,
        actor_label=actor_label,
        correlation_id=correlation_id,
        clock=clock,
    )


def expire_sessions(session: Session, store: EventStore, *, clock: Clock) -> list[str]:
    """Sweep: sessions past ``expires_at`` end automatically (post-hoc Task included)."""
    now = clock.now()
    rows = session.execute(
        text(
            "SELECT session_id FROM breakglass_sessions WHERE ended_at "
            "IS NULL AND expires_at <= :now"
        ),
        {"now": now},
    ).all()
    ended: list[str] = []
    for (sid,) in rows:
        bg = _load(session, str(sid), for_update=True)
        _end(
            session,
            store,
            bg,
            ended_reason="EXPIRED",
            actor_uuid=str(bg.account_id),
            actor_label="system:breakglass-sweep",
            correlation_id=f"bg-expire:{sid}",
            clock=clock,
        )
        ended.append(str(sid))
    return ended


def _open_posthoc_verification(
    session: Session,
    store: EventStore,
    bg: BreakGlassSession,
    correlation_id: str,
    clock: Clock,
    runtime: Any,
) -> str | None:
    """Automatic post-hoc verification Task assigned by the §7D.2 engine (Owner = implementer)."""
    from server.application import tasks as t
    from server.application import verification as v
    from server.application.bus import Principal

    channel = session.execute(
        text(
            "SELECT id FROM channels WHERE workspace_id = :w AND status <> 'deleted' "
            "ORDER BY CASE WHEN channel_type = 'ops' THEN 0 ELSE 1 END, channel_id LIMIT 1"
        ),
        {"w": bg.workspace_id},
    ).first()
    owner = session.execute(
        text("SELECT account_id, account_type FROM accounts WHERE id = :i"), {"i": bg.account_id}
    ).first()
    if channel is None or owner is None:
        _skip(session, bg, "NO_CHANNEL", correlation_id, clock)
        return None
    principal = Principal(
        str(owner[0]), str(bg.account_id), str(owner[1]), f"breakglass:{bg.session_id}"
    )
    ctx = bus.CommandContext(
        session=session,
        store=store,
        authorizer=runtime.authorizer if runtime is not None else None,
        clock=clock,
        principal=principal,
        workspace_id=str(bg.workspace_id),
        correlation_id=correlation_id,
        idempotency_key=f"bg-posthoc:{bg.session_id}",
    )
    try:
        created = bus.execute(
            t.CreateTask(
                title=f"Post-hoc review of break-glass session {bg.session_id}",
                channel_id=str(channel[0]),
                domain="security",
                risk="HIGH",
                criteria=POSTHOC_CRITERIA,
            ),
            ctx,
        )
        task_id = created.resource_id
        policy_hash = hashlib.sha256(
            json.dumps({"session": bg.session_id, "scope": bg.scope}, sort_keys=True).encode()
        ).hexdigest()
        vctx = bus.CommandContext(
            **{**ctx.__dict__, "idempotency_key": f"bg-posthoc-vr:{bg.session_id}"}
        )
        bus.execute(
            v.CreateVerificationRun(
                target_type="task",
                target_id=task_id,
                implementer_account_id=str(owner[0]),
                verifier_account_id="",
                implementer_credential_fingerprint=f"breakglass:{bg.session_id}",
                verifier_credential_fingerprint="auto",
                target_commit=f"break-glass:{bg.session_id}",
                effective_policy_hash=policy_hash,
                task_id=task_id,
                auto_assign=True,
            ),
            vctx,
        )
        return task_id
    except (bus.CommandError, EventStoreError) as exc:
        _skip(session, bg, exc.code, correlation_id, clock)
        return None


def _skip(
    session: Session, bg: BreakGlassSession, code: str, correlation_id: str, clock: Clock
) -> None:
    append_audit(
        session,
        action="breakglass.posthoc_pending",
        target_type="break_glass",
        target_id=bg.session_id,
        result="DEFERRED",
        actor_label="system:breakglass",
        correlation_id=correlation_id,
        workspace_id=bg.workspace_id,
        actor_account_id=bg.account_id,
        error_code=code,
        metadata={"note": "post-hoc verification Task could not be opened automatically"},
        clock=clock,
    )
