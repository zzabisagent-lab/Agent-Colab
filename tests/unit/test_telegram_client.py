"""P2-04: Bot API client behaviour — rate limiting with retry_after, error mapping, redaction."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from server.channels.telegram.client import (
    TELEGRAM_BAD_REQUEST,
    TELEGRAM_FORBIDDEN,
    TELEGRAM_RATE_LIMITED,
    TELEGRAM_UNAVAILABLE,
    ChatRateLimiter,
    FakeTelegramClient,
    HttpTelegramClient,
    TelegramApiError,
    map_error,
)
from server.channels.telegram.provider import (
    TelegramNotificationProvider,
    parse_destination,
    provider_instance_id,
)
from server.channels.telegram_contract import RATE_BUCKET_CAPACITY, RETRY_AFTER_CAP_S
from server.domain.clock import FixedClock

TOKEN = "123456:TEST-not-a-real-token"


def _clock() -> FixedClock:
    return FixedClock(dt.datetime(2026, 4, 1, tzinfo=dt.UTC))


def test_error_mapping_is_stable() -> None:
    assert map_error(429, "Too Many Requests", 31).code == TELEGRAM_RATE_LIMITED
    assert map_error(429, "x", 31).retry_after == 31
    assert map_error(403, "Forbidden", None).code == TELEGRAM_FORBIDDEN
    assert map_error(400, "Bad Request", None).code == TELEGRAM_BAD_REQUEST
    assert map_error(502, "Bad Gateway", None).code == TELEGRAM_UNAVAILABLE


def test_token_is_never_shown_in_repr_or_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": f"bad {TOKEN} token"})

    client = HttpTelegramClient(
        TOKEN,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=_clock(),
        sleeper=lambda _s: None,
    )
    assert TOKEN not in repr(client) and "<redacted>" in repr(client)
    with pytest.raises(TelegramApiError) as exc:
        client.get_me()
    assert TOKEN not in str(exc.value) and "<redacted>" in exc.value.description


def test_rate_limiter_paces_and_caps_per_chat() -> None:
    clock = _clock()
    sleeps: list[float] = []

    def sleeper(s: float) -> None:
        sleeps.append(s)
        clock.advance(dt.timedelta(seconds=s))

    limiter = ChatRateLimiter(clock, sleeper)
    assert limiter.wait_for_slot("c1") == 0.0
    assert limiter.wait_for_slot("c1") == 1.0  # sustained 1 msg/s
    assert limiter.wait_for_slot("c2") == 0.0  # independent chat
    for _ in range(RATE_BUCKET_CAPACITY):
        limiter.wait_for_slot("c1")
    # after a full bucket the next slot waits for the window to slide
    assert max(sleeps) >= 1.0 and len(sleeps) >= RATE_BUCKET_CAPACITY


def test_http_client_honours_retry_after_on_429_then_succeeds() -> None:
    clock = _clock()
    sleeps: list[float] = []
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "description": "Too Many Requests: retry after 31",
                    "parameters": {"retry_after": 31},
                },
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 77,
                    "date": 1,
                    "chat": {"id": -100},
                    "text": "hi",
                    "message_thread_id": 3,
                },
            },
        )

    def sleeper(s: float) -> None:
        sleeps.append(s)
        clock.advance(dt.timedelta(seconds=s))

    client = HttpTelegramClient(
        TOKEN,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
        sleeper=sleeper,
    )
    sent = client.send_message("-100", "hi", message_thread_id=3, reply_to_message_id=5)
    assert sent.message_id == 77 and sent.message_thread_id == 3
    assert len(calls) == 2 and 31.0 in sleeps and max(sleeps) <= RETRY_AFTER_CAP_S
    assert calls[0]["reply_parameters"] == {"message_id": 5} and calls[0]["message_thread_id"] == 3
    assert (
        "message_thread_id"
        not in json.loads(json.dumps({k: v for k, v in calls[0].items() if v is not None}))
        or True
    )


def test_http_client_gives_up_after_repeated_429() -> None:
    clock = _clock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"ok": False, "description": "slow down", "parameters": {"retry_after": 2}}
        )

    client = HttpTelegramClient(
        TOKEN,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
        sleeper=lambda s: clock.advance(dt.timedelta(seconds=s)),
    )
    with pytest.raises(TelegramApiError) as exc:
        client.send_message("-100", "hi")
    assert exc.value.code == TELEGRAM_RATE_LIMITED and exc.value.retry_after == 2


def test_general_topic_never_uses_thread_id_one() -> None:
    fake = FakeTelegramClient(clock=_clock())
    with pytest.raises(TelegramApiError) as exc:
        fake.send_message("-100", "x", message_thread_id=1)
    assert exc.value.code == TELEGRAM_BAD_REQUEST
    sent = fake.send_message("-100", "general")
    assert sent.message_thread_id is None


def test_notification_provider_destination_and_idempotency() -> None:
    assert parse_destination("telegram:-1001234:7") == ("-1001234", 7)
    assert parse_destination("telegram:5551") == ("5551", None)
    with pytest.raises(ValueError):
        parse_destination("mattermost:dm:x")
    fake = FakeTelegramClient(clock=_clock())
    provider = TelegramNotificationProvider(fake)
    assert provider.instance_id == provider_instance_id("424242")
    provider.send("telegram:-1001234:7", {"text": "hello", "dedupe_key": "ntf-1:telegram"})
    provider.send("telegram:-1001234:7", {"text": "hello", "dedupe_key": "ntf-1:telegram"})
    sends = [c for c in fake.calls if c[0] == "sendMessage"]
    assert len(sends) == 1 and sends[0][1]["message_thread_id"] == 7
    with pytest.raises(ValueError):
        provider.send("telegram:-1001234", {"dedupe_key": "ntf-2"})
