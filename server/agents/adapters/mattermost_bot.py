"""Mattermost bot adapter (development plan §7B.2; P3-12/P3-04).

Push delivery posts a structured work message in the Task thread (``server.channels.
work_messages``); results come back asynchronously through the bot's thread reply. The adapter
advertises ``secret_handles: unsupported`` — routing (V-P3-23) excludes it from Tasks that
carry secret handles, and ``deliver`` refuses such items as a second line of defence.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from server.agents.adapters.contract import (
    AdapterError,
    CancelAck,
    DeliveryMode,
    DeliveryReceipt,
    Heartbeat,
    InvokeResult,
    Probe,
    Usage,
    WorkItemView,
    register_adapter_type,
)
from server.agents.adapters.secret_support import declare_secret_handle_support
from server.domain.clock import Clock, SystemClock

ADAPTER_TYPE = "mattermost_bot"
MessageSink = Callable[[WorkItemView, dict[str, Any]], str | None]  # returns dedupe/post ref


def _noop_sink(_item: WorkItemView, _message: dict[str, Any]) -> str | None:
    return None


class MattermostBotAdapter:
    """One registered bot Agent; ``sink`` hands rendered work messages to the outbox."""

    adapter_type = ADAPTER_TYPE

    def __init__(
        self,
        endpoint: Mapping[str, Any],
        *,
        sink: MessageSink | None = None,
        clock: Clock | None = None,
        health_probe: Callable[[], None] | None = None,
    ) -> None:
        try:
            self.agent_id = str(endpoint["agent_id"])
            self.provider_instance_id = str(endpoint["provider_instance_id"])
            self.bot_user_id = str(endpoint["bot_user_id"])
        except KeyError as exc:
            raise AdapterError("ADAPTER_BAD_RESPONSE", f"endpoint config missing {exc}") from exc
        self.bot_username = str(endpoint.get("bot_username") or self.agent_id)
        self.capabilities = tuple(str(c) for c in endpoint.get("capabilities", ()))
        self.capacity = int(endpoint.get("capacity", 1))
        self._sink = sink or _noop_sink
        self._health_probe = health_probe  # e.g. a Mattermost ``users/me`` call; raises on failure
        self._clock = clock or SystemClock()
        self._last_heartbeat: Heartbeat | None = None
        self.delivered: dict[str, str | None] = {}

    def probe(self) -> Probe:
        fingerprint = hashlib.sha256(
            f"{ADAPTER_TYPE}|{self.agent_id}|{self.provider_instance_id}|{self.bot_user_id}".encode()
        ).hexdigest()
        return Probe(
            agent_id=self.agent_id,
            adapter_type=ADAPTER_TYPE,
            runtime={"provider_instance_id": self.provider_instance_id},
            capabilities=self.capabilities,
            unsupported=("secret_handles", "invoke_sync"),
            delivery_modes=(DeliveryMode.PUSH,),
            limits={"concurrent_tasks": self.capacity},
            secret_handles="unsupported",  # noqa: S106  # nosec B106 - advertised support level, not a secret
            identity_hash=f"sha256:{fingerprint}",
        )

    def deliver(self, item: WorkItemView) -> DeliveryReceipt:
        if item.secret_handles:
            return DeliveryReceipt(item.work_item_id, None, rejection_code="CAPABILITY_UNSUPPORTED")
        if item.work_item_id in self.delivered:  # idempotent per work item (CS-02)
            return DeliveryReceipt(
                item.work_item_id, self._clock.now(), receipt_id=self.delivered[item.work_item_id]
            )
        from server.agents.adapters.webhook import envelope_for
        from server.channels.work_messages import render_work_message

        envelope = envelope_for(item)
        message = {
            "message": render_work_message(envelope, self.bot_username),
            "props": {
                "agent_colab": {
                    "subject_type": "work_item",
                    "subject_id": item.work_item_id,
                    "work_message": True,
                }
            },
        }
        ref = self._sink(item, message)
        self.delivered[item.work_item_id] = ref
        return DeliveryReceipt(item.work_item_id, self._clock.now(), receipt_id=ref)

    def invoke(
        self,
        tool: str,
        payload: Mapping[str, Any],
        deadline: dt.datetime,
        secret_handles: Sequence[str],
        *,
        correlation_id: str,
    ) -> InvokeResult:
        """Bots have no synchronous call path: the invocation is delivered as an ``invoke`` work
        message and the result arrives through the thread reply (asynchronous adapter)."""
        if secret_handles:
            raise AdapterError("CAPABILITY_UNSUPPORTED", "secret handles unsupported")
        if self.capabilities and tool not in self.capabilities:
            raise AdapterError("CAPABILITY_UNSUPPORTED", tool)
        return InvokeResult(
            result={"status": "DELIVERED_ASYNC", "tool": tool},
            usage=Usage(usage_unavailable="ADAPTER_NO_METERING"),
            correlation_id=correlation_id,
        )

    def cancel(self, target_id: str) -> CancelAck:
        now = self._clock.now()
        return CancelAck(target_id, now, now + dt.timedelta(seconds=60))

    def heartbeat(self) -> Heartbeat:
        if self._health_probe is not None:
            try:
                self._health_probe()
            except BaseException as exc:
                raise self.normalize_error(exc) from exc
        if self._last_heartbeat is None:
            raise AdapterError("ADAPTER_UNREACHABLE", "no heartbeat recorded yet")
        return self._last_heartbeat

    def record_heartbeat(self, heartbeat: Heartbeat) -> None:
        self._last_heartbeat = heartbeat

    def normalize_error(self, exc: BaseException) -> AdapterError:
        if isinstance(exc, AdapterError):
            return exc
        if isinstance(exc, TimeoutError):
            return AdapterError("ADAPTER_TIMEOUT", "Mattermost timed out", retryable=True)
        if isinstance(exc, PermissionError):  # before OSError: PermissionError is an OSError
            return AdapterError("ADAPTER_AUTH_FAILED", "Mattermost rejected the bot token")
        if isinstance(exc, ConnectionError | OSError):
            return AdapterError("ADAPTER_UNREACHABLE", "Mattermost unreachable", retryable=True)
        if isinstance(exc, ValueError | KeyError | TypeError):
            return AdapterError("ADAPTER_BAD_RESPONSE", type(exc).__name__)
        text = str(exc).upper()
        if "RATE" in text and "LIMIT" in text:
            return AdapterError("ADAPTER_RATE_LIMITED", "Mattermost rate limited", retryable=True)
        return AdapterError("ADAPTER_INTERNAL", type(exc).__name__)


def _factory(endpoint: Mapping[str, Any]) -> MattermostBotAdapter:
    return MattermostBotAdapter(endpoint)


register_adapter_type(ADAPTER_TYPE, _factory)
declare_secret_handle_support(ADAPTER_TYPE, False)
