"""``/colab link start|confirm`` (P2-13; development plan §7A.5).

``start``: a one-time code with a 10-minute TTL is sent to the Mattermost user by DM (never in
the channel). ``confirm <code>``: the command path has no web session, so the link lands in
``pending_admin`` (verification_method ``admin_approval``) until an Administrator approves it;
the web path (``POST /api/v1/identity/links/confirm`` with a session) becomes ``active`` with
``signed_challenge``. Five wrong codes lock the user for 15 minutes.

The command-path target Account is the one whose ``auth_subject`` is ``mattermost:<username>``
(or whose ``account_id`` is ``acct-<username>``); Administrators pre-create it. Events
IDENTITY_LINK_CHALLENGED/VERIFIED and audit rows are written by ``ExternalLinkService``. The
Event actor for an unlinked user is the instance's system service Account.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime
from server.channels import commands as grammar
from server.channels import router as rt
from server.channels.mattermost import provider as prov
from server.domain.clock import Clock
from server.domain.defaults import LINK_CHALLENGE_TTL_MIN
from server.identity.external_links import sql_service
from server.identity.principals import IdentityError

MESSAGES: dict[str, str] = {
    "link.start.sent": "A one-time code was sent to you by direct message. Run "
    "`/colab link confirm <code>` within {ttl} minutes, or enter the code in the web console.",
    "link.start.dm": "Agent-Colab link code: {code} (valid {ttl} minutes, single use). "
    "Run `/colab link confirm {code}` in Mattermost or enter it in the web console.",
    "link.confirm.pending": "Code accepted. Your link to Account `{account_id}` is pending "
    "Administrator approval.",
    "link.confirm.no_account": "No Agent-Colab Account is prepared for `{username}`. Ask an "
    "Administrator to create one, or confirm the code in the web console while logged in.",
    "link.error": "Link failed: {code} {detail}",
    "link.system_account_missing": "Linking is not configured (system service Account missing).",
}

_STATUS = {
    "EXTERNAL_IDENTITY_LOCKED": "EXTERNAL_IDENTITY_LOCKED",
}


def system_actor_uuid(session: Session, workspace_id: uuid.UUID) -> uuid.UUID | None:
    """The instance's system service Account (created by Setup) acting for unlinked users."""
    row = session.execute(
        text(
            "SELECT id FROM accounts WHERE workspace_id = :ws AND account_type = 'service' "
            "AND status = 'ACTIVE' ORDER BY (account_id LIKE 'acct-system%') DESC, created_at "
            "LIMIT 1"
        ),
        {"ws": workspace_id},
    ).first()
    return None if row is None else uuid.UUID(str(row[0]))


def account_for_mattermost_user(
    session: Session, workspace_id: uuid.UUID, username: str
) -> str | None:
    row = session.execute(
        text(
            "SELECT account_id FROM accounts WHERE workspace_id = :ws AND status = 'ACTIVE' "
            "AND account_type = 'human' AND (auth_subject = :subj OR account_id = :acct) "
            "ORDER BY (auth_subject = :subj) DESC LIMIT 1"
        ),
        {"ws": workspace_id, "subj": f"mattermost:{username}", "acct": f"acct-{username}"},
    ).first()
    return None if row is None else str(row[0])


def _text(key: str, **fields: Any) -> str:
    return MESSAGES[key].format(**fields)


def _error(exc: IdentityError, parsed: grammar.ParsedCommand) -> rt.CommandResponse:
    return rt.CommandResponse(
        "ephemeral", _text("link.error", code=exc.code, detail=exc.detail), exc.code, parsed=parsed
    )


def link_start(
    session: Session,
    runtime: Runtime,
    req: rt.SlashRequest,
    parsed: grammar.ParsedCommand,
    clock: Clock,
) -> rt.CommandResponse:
    inst = prov.load_instance(session, req.provider_instance_id)
    if inst is None:
        return rt.ephemeral("command.error", "PROVIDER_INSTANCE_UNKNOWN", req.provider_instance_id)
    actor = system_actor_uuid(session, inst.workspace_id)
    if actor is None:
        return rt.CommandResponse(
            "ephemeral",
            _text("link.system_account_missing"),
            "SYSTEM_ACCOUNT_MISSING",
            parsed=parsed,
        )
    service = sql_service(session, runtime.store_for(session), clock)
    try:
        issued = service.start_challenge(
            req.provider_instance_id,
            req.user_id,
            actor_account_uuid=actor,
            correlation_id=rt._correlation(req),
        )
    except IdentityError as exc:
        return _error(exc, parsed)
    client = prov.client_for(inst)
    client.direct_message(
        req.user_id, _text("link.start.dm", code=issued.code, ttl=LINK_CHALLENGE_TTL_MIN)
    )
    return rt.CommandResponse(
        "ephemeral", _text("link.start.sent", ttl=LINK_CHALLENGE_TTL_MIN), "OK", parsed=parsed
    )


def link_confirm(
    session: Session,
    runtime: Runtime,
    req: rt.SlashRequest,
    parsed: grammar.ParsedCommand,
    clock: Clock,
) -> rt.CommandResponse:
    inst = prov.load_instance(session, req.provider_instance_id)
    if inst is None:
        return rt.ephemeral("command.error", "PROVIDER_INSTANCE_UNKNOWN", req.provider_instance_id)
    actor = system_actor_uuid(session, inst.workspace_id)
    if actor is None:
        return rt.CommandResponse(
            "ephemeral",
            _text("link.system_account_missing"),
            "SYSTEM_ACCOUNT_MISSING",
            parsed=parsed,
        )
    account_id = account_for_mattermost_user(session, inst.workspace_id, req.user_name)
    if account_id is None:
        return rt.CommandResponse(
            "ephemeral",
            _text("link.confirm.no_account", username=req.user_name),
            "ACCOUNT_NOT_FOUND",
            parsed=parsed,
        )
    code = str(parsed.args.get("code", "")) if parsed.args else ""
    service = sql_service(session, runtime.store_for(session), clock)
    try:
        link = service.confirm_challenge(
            req.provider_instance_id,
            req.user_id,
            code,
            account_id,
            path="command",
            actor_account_uuid=actor,
            correlation_id=rt._correlation(req),
        )
    except IdentityError as exc:
        return _error(exc, parsed)
    return rt.CommandResponse(
        "ephemeral",
        _text("link.confirm.pending", account_id=link.account_id),
        "OK",
        resource_id=link.link_id,
        parsed=parsed,
    )


def register() -> None:
    """Mount the handlers on the Router (called by the app wiring, not at import)."""
    rt.LINK_HANDLERS["start"] = link_start
    rt.LINK_HANDLERS["confirm"] = link_confirm


def unregister() -> None:
    for verb, handler in (("start", link_start), ("confirm", link_confirm)):
        if rt.LINK_HANDLERS.get(verb) is handler:
            del rt.LINK_HANDLERS[verb]
