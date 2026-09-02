"""Telegram Bot API client (P2-04) honouring the P0-13 constraints.

- ``TelegramClient`` is the protocol the Bridge (P2-05/06) and notification provider use;
  ``HttpTelegramClient`` talks to ``https://api.telegram.org`` with httpx; ``FakeTelegramClient``
  is the deterministic in-memory double for unit tests.
- Rate limiting: a per-chat token bucket from ``server.channels.telegram_contract`` (capacity
  20 per 60 s, sustained 1 msg/s, one in-flight send per chat) plus capped ``retry_after`` on 429.
- The bot token is never logged: ``repr`` and error messages redact it.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from server.channels.telegram_contract import (
    RATE_BUCKET_CAPACITY,
    RATE_BUCKET_WINDOW_S,
    RATE_SUSTAINED_PER_S,
    RETRY_AFTER_CAP_S,
    retry_delay,
)
from server.domain.clock import Clock, SystemClock

API_BASE = "https://api.telegram.org"
TELEGRAM_RATE_LIMITED = "TELEGRAM_RATE_LIMITED"
TELEGRAM_FORBIDDEN = "TELEGRAM_FORBIDDEN"
TELEGRAM_BAD_REQUEST = "TELEGRAM_BAD_REQUEST"
TELEGRAM_UNAVAILABLE = "TELEGRAM_UNAVAILABLE"
MAX_SEND_ATTEMPTS = 4


class TelegramApiError(RuntimeError):
    def __init__(
        self, code: str, description: str, *, status: int = 0, retry_after: int | None = None
    ) -> None:
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.status = status
        self.retry_after = retry_after


def map_error(status: int, description: str, retry_after: int | None) -> TelegramApiError:
    if status == 429:
        return TelegramApiError(
            TELEGRAM_RATE_LIMITED, description, status=status, retry_after=retry_after
        )
    if status in (401, 403):
        return TelegramApiError(TELEGRAM_FORBIDDEN, description, status=status)
    if 400 <= status < 500:
        return TelegramApiError(TELEGRAM_BAD_REQUEST, description, status=status)
    return TelegramApiError(TELEGRAM_UNAVAILABLE, description, status=status)


@dataclass(frozen=True)
class SentMessage:
    message_id: int
    chat_id: str
    date: int
    message_thread_id: int | None = None
    text: str | None = None


@dataclass(frozen=True)
class ForumTopic:
    message_thread_id: int
    name: str


@dataclass(frozen=True)
class FileInfo:
    file_id: str
    file_path: str | None
    file_size: int | None


class TelegramClient(Protocol):
    def get_me(self) -> dict[str, Any]: ...

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage: ...

    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> SentMessage: ...

    def delete_message(self, chat_id: str, message_id: int) -> bool: ...

    def create_forum_topic(self, chat_id: str, name: str) -> ForumTopic: ...

    def close_forum_topic(self, chat_id: str, message_thread_id: int) -> bool: ...

    def delete_forum_topic(self, chat_id: str, message_thread_id: int) -> bool: ...

    def get_chat(self, chat_id: str) -> dict[str, Any]: ...

    def get_chat_member(self, chat_id: str, user_id: int) -> dict[str, Any]: ...

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]: ...

    def set_webhook(self, url: str, secret_token: str) -> bool: ...

    def delete_webhook(self) -> bool: ...

    def send_document(
        self, chat_id: str, filename: str, data: bytes, *, message_thread_id: int | None = None
    ) -> SentMessage: ...

    def get_file(self, file_id: str) -> FileInfo: ...

    def download_file(self, file_path: str) -> bytes: ...


class ChatRateLimiter:
    """Token bucket per chat (contract: 20 per 60 s, 1 msg/s sustained, one in-flight send)."""

    def __init__(self, clock: Clock, sleeper: Callable[[float], None]) -> None:
        self._clock = clock
        self._sleep = sleeper
        self._sent: dict[str, list[dt.datetime]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock_for(self, chat_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(chat_id, threading.Lock())

    def wait_for_slot(self, chat_id: str) -> float:
        """Sleep as needed before a send; returns the delay applied (seconds)."""
        now = self._clock.now()
        window = dt.timedelta(seconds=RATE_BUCKET_WINDOW_S)
        history = [t for t in self._sent.get(chat_id, []) if now - t < window]
        delay = 0.0
        if history:
            since_last = (now - history[-1]).total_seconds()
            min_gap = 1.0 / RATE_SUSTAINED_PER_S
            if since_last < min_gap:
                delay = max(delay, min_gap - since_last)
        if len(history) >= RATE_BUCKET_CAPACITY:
            delay = max(delay, (history[0] + window - now).total_seconds())
        if delay > 0:
            self._sleep(delay)
        self._sent[chat_id] = [*history, self._clock.now()]
        return delay


class HttpTelegramClient:
    def __init__(
        self,
        token: str,
        *,
        http: httpx.Client | None = None,
        clock: Clock | None = None,
        sleeper: Callable[[float], None] | None = None,
        base_url: str = API_BASE,
    ) -> None:
        if not token or ":" not in token:
            raise TelegramApiError(TELEGRAM_FORBIDDEN, "bot token malformed")
        self._token = token
        self._http = http or httpx.Client(timeout=35.0)
        self._clock = clock or SystemClock()
        import time

        self._sleep = sleeper or time.sleep
        self._base = f"{base_url}/bot{token}"
        self._file_base = f"{base_url}/file/bot{token}"
        self._limiter = ChatRateLimiter(self._clock, self._sleep)
        self.bot_id = token.split(":", 1)[0]

    def __repr__(self) -> str:
        return f"HttpTelegramClient(bot_id={self.bot_id}, token=<redacted>)"

    # -- transport ---------------------------------------------------------------------------
    def _call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._http.post(f"{self._base}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise TelegramApiError(TELEGRAM_UNAVAILABLE, type(exc).__name__) from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise TelegramApiError(
                TELEGRAM_UNAVAILABLE, "non-JSON response", status=resp.status_code
            ) from exc
        if not body.get("ok"):
            params_out = body.get("parameters") or {}
            raise map_error(
                int(resp.status_code),
                str(body.get("description", "")).replace(self._token, "<redacted>"),
                params_out.get("retry_after"),
            )
        return body["result"]

    def _send_with_limits(self, chat_id: str, method: str, **params: Any) -> Any:
        with self._limiter.lock_for(chat_id):
            for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
                self._limiter.wait_for_slot(chat_id)
                try:
                    return self._call(method, chat_id=chat_id, **params)
                except TelegramApiError as exc:
                    if exc.code != TELEGRAM_RATE_LIMITED or attempt == MAX_SEND_ATTEMPTS:
                        raise
                    self._sleep(min(retry_delay(exc.retry_after, attempt), RETRY_AFTER_CAP_S))
        raise TelegramApiError(TELEGRAM_UNAVAILABLE, "unreachable")  # pragma: no cover

    # -- API -----------------------------------------------------------------------------------
    def get_me(self) -> dict[str, Any]:
        result: dict[str, Any] = self._call("getMe")
        return result

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        params: dict[str, Any] = {"text": text, "message_thread_id": message_thread_id}
        if reply_to_message_id is not None:
            params["reply_parameters"] = {"message_id": reply_to_message_id}
        if parse_mode:
            params["parse_mode"] = parse_mode
        return _sent(self._send_with_limits(chat_id, "sendMessage", **params))

    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> SentMessage:
        return _sent(
            self._send_with_limits(chat_id, "editMessageText", message_id=message_id, text=text)
        )

    def delete_message(self, chat_id: str, message_id: int) -> bool:
        return bool(self._call("deleteMessage", chat_id=chat_id, message_id=message_id))

    def create_forum_topic(self, chat_id: str, name: str) -> ForumTopic:
        r = self._send_with_limits(chat_id, "createForumTopic", name=name)
        return ForumTopic(int(r["message_thread_id"]), str(r["name"]))

    def close_forum_topic(self, chat_id: str, message_thread_id: int) -> bool:
        return bool(
            self._call("closeForumTopic", chat_id=chat_id, message_thread_id=message_thread_id)
        )

    def delete_forum_topic(self, chat_id: str, message_thread_id: int) -> bool:
        return bool(
            self._call("deleteForumTopic", chat_id=chat_id, message_thread_id=message_thread_id)
        )

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call("getChat", chat_id=chat_id)
        return result

    def get_chat_member(self, chat_id: str, user_id: int) -> dict[str, Any]:
        result: dict[str, Any] = self._call("getChatMember", chat_id=chat_id, user_id=user_id)
        return result

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call("getUpdates", offset=offset, timeout=timeout)
        return result

    def set_webhook(self, url: str, secret_token: str) -> bool:
        return bool(self._call("setWebhook", url=url, secret_token=secret_token))

    def delete_webhook(self) -> bool:
        return bool(self._call("deleteWebhook"))

    def send_document(
        self, chat_id: str, filename: str, data: bytes, *, message_thread_id: int | None = None
    ) -> SentMessage:
        with self._limiter.lock_for(chat_id):
            self._limiter.wait_for_slot(chat_id)
            fields: dict[str, Any] = {"chat_id": chat_id}
            if message_thread_id is not None:
                fields["message_thread_id"] = str(message_thread_id)
            try:
                resp = self._http.post(
                    f"{self._base}/sendDocument", data=fields, files={"document": (filename, data)}
                )
            except httpx.HTTPError as exc:
                raise TelegramApiError(TELEGRAM_UNAVAILABLE, type(exc).__name__) from exc
            body = resp.json()
            if not body.get("ok"):
                raise map_error(int(resp.status_code), str(body.get("description", "")), None)
            return _sent(body["result"])

    def get_file(self, file_id: str) -> FileInfo:
        r = self._call("getFile", file_id=file_id)
        return FileInfo(str(r["file_id"]), r.get("file_path"), r.get("file_size"))

    def download_file(self, file_path: str) -> bytes:
        try:
            resp = self._http.get(f"{self._file_base}/{file_path}")
        except httpx.HTTPError as exc:
            raise TelegramApiError(TELEGRAM_UNAVAILABLE, type(exc).__name__) from exc
        if resp.status_code != 200:
            raise map_error(int(resp.status_code), "file download failed", None)
        return resp.content


def _sent(r: dict[str, Any]) -> SentMessage:
    chat = r.get("chat") or {}
    return SentMessage(
        message_id=int(r["message_id"]),
        chat_id=str(chat.get("id", "")),
        date=int(r.get("date", 0)),
        message_thread_id=r.get("message_thread_id"),
        text=r.get("text"),
    )


@dataclass
class FakeTelegramClient:
    """Deterministic in-memory Bot API double: records calls, simulates 429s, topics, updates."""

    bot_id: str = "424242"
    clock: Clock = field(default_factory=SystemClock)
    rate_limit_after: int | None = None  # raise 429 after N sends (None = never)
    retry_after: int = 31
    fail_forbidden_chats: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    messages: dict[str, list[SentMessage]] = field(default_factory=dict)
    updates: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 100
    _sends: int = 0
    webhook: tuple[str, str] | None = None

    def _record(self, method: str, **params: Any) -> None:
        self.calls.append((method, params))

    def get_me(self) -> dict[str, Any]:
        self._record("getMe")
        return {"id": int(self.bot_id), "is_bot": True, "username": "agent_colab_bot"}

    def _emit(self, chat_id: str, text: str | None, thread: int | None) -> SentMessage:
        if chat_id in self.fail_forbidden_chats:
            raise TelegramApiError(TELEGRAM_FORBIDDEN, "bot is not a member", status=403)
        self._sends += 1
        if self.rate_limit_after is not None and self._sends > self.rate_limit_after:
            self.rate_limit_after = None  # one 429, then succeed
            raise TelegramApiError(
                TELEGRAM_RATE_LIMITED, "Too Many Requests", status=429, retry_after=self.retry_after
            )
        self._next_id += 1
        msg = SentMessage(self._next_id, chat_id, int(self.clock.now().timestamp()), thread, text)
        self.messages.setdefault(chat_id, []).append(msg)
        return msg

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> SentMessage:
        self._record(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
        )
        if message_thread_id == 1:
            raise TelegramApiError(TELEGRAM_BAD_REQUEST, "message thread not found", status=400)
        return self._emit(chat_id, text, message_thread_id)

    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> SentMessage:
        self._record("editMessageText", chat_id=chat_id, message_id=message_id, text=text)
        for i, m in enumerate(self.messages.get(chat_id, [])):
            if m.message_id == message_id:
                edited = SentMessage(m.message_id, m.chat_id, m.date, m.message_thread_id, text)
                self.messages[chat_id][i] = edited
                return edited
        raise TelegramApiError(TELEGRAM_BAD_REQUEST, "message can't be edited", status=400)

    def delete_message(self, chat_id: str, message_id: int) -> bool:
        self._record("deleteMessage", chat_id=chat_id, message_id=message_id)
        before = len(self.messages.get(chat_id, []))
        self.messages[chat_id] = [
            m for m in self.messages.get(chat_id, []) if m.message_id != message_id
        ]
        return len(self.messages[chat_id]) < before

    def create_forum_topic(self, chat_id: str, name: str) -> ForumTopic:
        self._record("createForumTopic", chat_id=chat_id, name=name)
        svc = self._emit(chat_id, None, None)
        return ForumTopic(svc.message_id, name)

    def close_forum_topic(self, chat_id: str, message_thread_id: int) -> bool:
        self._record("closeForumTopic", chat_id=chat_id, message_thread_id=message_thread_id)
        return True

    def delete_forum_topic(self, chat_id: str, message_thread_id: int) -> bool:
        self._record("deleteForumTopic", chat_id=chat_id, message_thread_id=message_thread_id)
        return True

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        self._record("getChat", chat_id=chat_id)
        return {"id": chat_id, "type": "supergroup", "is_forum": True}

    def get_chat_member(self, chat_id: str, user_id: int) -> dict[str, Any]:
        self._record("getChatMember", chat_id=chat_id, user_id=user_id)
        return {"status": "administrator", "can_manage_topics": True}

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        self._record("getUpdates", offset=offset, timeout=timeout)
        return [u for u in self.updates if offset is None or int(u["update_id"]) >= offset]

    def set_webhook(self, url: str, secret_token: str) -> bool:
        self._record("setWebhook", url=url)
        self.webhook = (url, secret_token)
        return True

    def delete_webhook(self) -> bool:
        self._record("deleteWebhook")
        self.webhook = None
        return True

    def send_document(
        self, chat_id: str, filename: str, data: bytes, *, message_thread_id: int | None = None
    ) -> SentMessage:
        self._record("sendDocument", chat_id=chat_id, filename=filename, size=len(data))
        return self._emit(chat_id, f"[document {filename}]", message_thread_id)

    def get_file(self, file_id: str) -> FileInfo:
        self._record("getFile", file_id=file_id)
        return FileInfo(file_id, f"documents/{file_id}.bin", 4)

    def download_file(self, file_path: str) -> bytes:
        self._record("downloadFile", file_path=file_path)
        return b"data"
