"""Telegram webhook intake (P2-04; development plan §7.5 provider callbacks).

``POST /api/v1/providers/telegram/updates/{provider_instance_id}`` validates, in this order and
before any normalization: the ``X-Telegram-Bot-Api-Secret-Token`` (constant-time compare with the
configured secret), an optional ``X-Colab-Body-SHA256`` header against the body, the provider
instance (known, active, telegram), the update's timestamp (5-minute tolerance), and replay
(``update_id`` already received -> 200 with zero side effects). Only then is the update
normalized and passed to the registered inbound handler (the Bridge). The intake creates no
domain Event.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from server.api.errors import ApiError
from server.channels.contract import CALLBACK_BODY_HASH_MISMATCH, CALLBACK_TIMESTAMP_EXPIRED
from server.channels.telegram.intake import (
    InboundHandler,
    InboundMessage,
    IntakeError,
    normalize_update,
)
from server.db.engine import session_scope
from server.domain.defaults import CALLBACK_TIMESTAMP_TOLERANCE_S

router = APIRouter(prefix="/api/v1/providers/telegram", tags=["providers"])
SECRET_ENV = "AGENT_COLAB_TELEGRAM_WEBHOOK_SECRET"  # noqa: S105 - the variable name, not a value  # nosec B105 - environment variable name, not a secret
HANDLER_STATE_KEY = "telegram_inbound_handler"


def _secret(request: Request) -> str | None:
    configured = getattr(request.app.state, "telegram_webhook_secret", None)
    return configured or os.environ.get(SECRET_ENV)


def _handler(request: Request) -> InboundHandler:
    handler: Callable[[InboundMessage], None] | None = getattr(
        request.app.state, HANDLER_STATE_KEY, None
    )
    return handler or (lambda _m: None)


@router.post("/updates/{provider_instance_id}", status_code=200)
async def telegram_update(
    provider_instance_id: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    x_colab_body_sha256: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()
    secret = _secret(request)
    if not secret or not x_telegram_bot_api_secret_token:
        raise ApiError(401, "CALLBACK_SIGNATURE_INVALID", "webhook secret token required")
    if not hmac.compare_digest(x_telegram_bot_api_secret_token.encode(), secret.encode()):
        raise ApiError(401, "CALLBACK_SIGNATURE_INVALID", "webhook secret token mismatch")
    if x_colab_body_sha256 is not None:
        digest = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(digest, x_colab_body_sha256.lower()):
            raise ApiError(401, CALLBACK_BODY_HASH_MISMATCH, "body hash mismatch")
    try:
        update = json.loads(body)
    except ValueError as exc:
        raise ApiError(400, "TELEGRAM_UPDATE_INVALID", "body is not JSON") from exc
    runtime = request.app.state.runtime
    if runtime is None:
        raise ApiError(503, "DATABASE_UNAVAILABLE", "database not configured")
    now = runtime.clock.now()
    with session_scope(runtime.session_factory) as session:
        row = session.execute(
            text(
                "SELECT id FROM provider_instances WHERE provider_instance_id = :p "
                "AND provider = 'telegram' AND status = 'active'"
            ),
            {"p": provider_instance_id},
        ).first()
        if row is None:
            raise ApiError(404, "PROVIDER_INSTANCE_UNKNOWN", "unknown telegram provider instance")
        try:
            inbound = normalize_update(
                provider_instance_id, update if isinstance(update, dict) else {}
            )
        except IntakeError as exc:
            raise ApiError(400, exc.code, exc.detail) from exc
        if inbound is None:
            return {"status": "ignored", "reason": "unsupported update kind"}
        message_time = dt.datetime.fromtimestamp(inbound.date, tz=dt.UTC)
        if abs((now - message_time).total_seconds()) > CALLBACK_TIMESTAMP_TOLERANCE_S:
            raise ApiError(403, CALLBACK_TIMESTAMP_EXPIRED, "update older than the tolerance")
        try:
            with session.begin_nested():
                session.execute(
                    text(
                        "INSERT INTO telegram_update_receipts (provider_instance_id, update_id, "
                        "chat_id, message_id) VALUES (:p, :u, :c, :m)"
                    ),
                    {
                        "p": provider_instance_id,
                        "u": inbound.update_id,
                        "c": inbound.chat_id,
                        "m": inbound.message_id,
                    },
                )
        except IntegrityError:
            return {"status": "replayed", "update_id": inbound.update_id}
        _handler(request)(inbound)
    return {"status": "accepted", "update_id": inbound.update_id}
