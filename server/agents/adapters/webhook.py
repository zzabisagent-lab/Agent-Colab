"""REST/Webhook push adapter (development plan §7.3, §7B.2; P3-11/P3-04).

Every call is one signed ``POST`` to the Agent endpoint: headers ``X-Colab-Timestamp``,
``X-Colab-Nonce``, ``X-Colab-Signature`` (HMAC-SHA256 over ``timestamp.nonce.sha256(body)``),
``X-Colab-Key-Ref`` (Secret Broker reference), ``X-Colab-Correlation-Id``, ``X-Colab-Op``
(``probe|deliver|invoke|cancel``) and, for deliveries, ``X-Colab-Delivery-No``. ``deliver``
expects ``202`` with a ``colab.delivery-receipt.v1`` body. Key bytes come from a
``SigningKeyResolver`` and are never logged; secret *handles* travel as opaque ids only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from server.agents import webhook_signing as ws
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
from server.agents.signing_keys import SigningKeyResolver, default_resolver
from server.domain.clock import Clock, SystemClock, isoformat_utc
from server.work.schemas import AdapterSchemaError, validate

ADAPTER_TYPE = "webhook"
HEADER_OP = "X-Colab-Op"
HEADER_CORRELATION = "X-Colab-Correlation-Id"
HEADER_DELIVERY_NO = "X-Colab-Delivery-No"
DEFAULT_TIMEOUT_S = 10.0


def _identity_hash(agent_id: str, url: str) -> str:
    return hashlib.sha256(f"{ADAPTER_TYPE}|{agent_id}|{url}".encode()).hexdigest()


def usage_from(data: Mapping[str, Any] | None) -> Usage:
    """Map a §7C ``usage`` / ``usage_unavailable`` pair onto ``Usage``."""
    if not data:
        return Usage(usage_unavailable="ADAPTER_NO_METERING")
    unavailable = data.get("usage_unavailable")
    if unavailable:
        return Usage(usage_unavailable=str(unavailable.get("reason", "ERROR")))
    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        return Usage(usage_unavailable="ADAPTER_NO_METERING")
    return Usage(
        model=usage.get("model"),
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        tool_calls=int(usage.get("tool_calls", 0)),
        wall_time_ms=int(usage.get("wall_time_ms", 0)),
        cost_units=usage.get("cost_units"),
    )


class WebhookAdapter:
    """Adapter for one registered webhook Agent (endpoint config from ``agents.endpoint``)."""

    adapter_type = ADAPTER_TYPE

    def __init__(
        self,
        endpoint: Mapping[str, Any],
        *,
        resolver: SigningKeyResolver | None = None,
        clock: Clock | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        try:
            self.url = str(endpoint["url"])
            self.agent_id = str(endpoint["agent_id"])
            self.credential_ref = str(endpoint["credential_ref"])
        except KeyError as exc:
            raise AdapterError("ADAPTER_BAD_RESPONSE", f"endpoint config missing {exc}") from exc
        self.timeout_s = float(endpoint.get("timeout_s", DEFAULT_TIMEOUT_S))
        self.capabilities = tuple(str(c) for c in endpoint.get("capabilities", ()))
        self.health_check = bool(endpoint.get("health_check", False))
        self._resolver = resolver or default_resolver()
        self._clock = clock or SystemClock()
        self._transport = transport
        self._last_heartbeat: Heartbeat | None = None

    # ------------------------------------------------------------------ transport
    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=self.timeout_s)

    def _post(
        self, op: str, body: Mapping[str, Any], *, correlation_id: str, extra: Mapping[str, str]
    ) -> httpx.Response:
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        key = self._resolver.resolve(self.credential_ref)
        headers = {
            **ws.sign(key, raw, self._clock, key_ref=self.credential_ref),
            HEADER_CORRELATION: correlation_id,
            HEADER_OP: op,
            "Content-Type": "application/json",
            **extra,
        }
        try:
            with self._client() as client:
                return client.post(self.url, content=raw, headers=headers)
        except Exception as exc:  # normalized below; the body is never logged
            raise self.normalize_error(exc) from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterError("ADAPTER_BAD_RESPONSE", "response is not JSON") from exc
        if not isinstance(data, dict):
            raise AdapterError("ADAPTER_BAD_RESPONSE", "response is not an object")
        return data

    def _check_status(self, response: httpx.Response, expected: int) -> None:
        if response.status_code == expected:
            return
        raise self.normalize_error(
            httpx.HTTPStatusError(
                f"unexpected status {response.status_code}",
                request=response.request,
                response=response,
            )
        )

    # ------------------------------------------------------------------ contract
    def probe(self) -> Probe:
        response = self._post(
            "probe",
            {"op": "probe", "agent_id": self.agent_id},
            correlation_id=f"probe:{self.agent_id}",
            extra={},
        )
        self._check_status(response, 200)
        data = self._json(response)
        try:
            validate("probe_response", data)
        except AdapterSchemaError as exc:
            raise AdapterError("ADAPTER_BAD_RESPONSE", exc.detail) from exc
        caps = data["capabilities"]
        modes = tuple(DeliveryMode(m) for m in data["delivery_modes"])
        return Probe(
            agent_id=str(data["identity"].get("agent_id", self.agent_id)),
            adapter_type=ADAPTER_TYPE,
            runtime=dict(data.get("runtime", {})),
            capabilities=tuple(str(c) for c in caps.get("tools", [])),
            unsupported=tuple(str(c) for c in caps.get("unsupported", [])),
            delivery_modes=modes,
            limits=dict(data.get("limits", {})),
            secret_handles=str(caps.get("secret_handles", "supported")),
            identity_hash=str(data["identity"].get("instance_fingerprint"))
            or _identity_hash(self.agent_id, self.url),
        )

    def deliver(self, item: WorkItemView) -> DeliveryReceipt:
        envelope = envelope_for(item)
        response = self._post(
            "deliver",
            envelope,
            correlation_id=item.correlation_id,
            extra={HEADER_DELIVERY_NO: str(int(item.payload.get("delivery_no", 1)))},
        )
        self._check_status(response, 202)
        data = self._json(response)
        try:
            validate("delivery_receipt", data)
        except AdapterSchemaError as exc:
            raise AdapterError("ADAPTER_BAD_RESPONSE", exc.detail) from exc
        if data["work_item_id"] != item.work_item_id:
            raise AdapterError("ADAPTER_BAD_RESPONSE", "receipt for a different work item")
        accepted = data.get("accepted_at")
        return DeliveryReceipt(
            work_item_id=item.work_item_id,
            accepted_at=dt.datetime.fromisoformat(accepted.replace("Z", "+00:00"))
            if accepted
            else None,
            rejection_code=data.get("rejection_code"),
            receipt_id=hashlib.sha256(
                json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:32],
        )

    def invoke(
        self,
        tool: str,
        payload: Mapping[str, Any],
        deadline: dt.datetime,
        secret_handles: Sequence[str],
        *,
        correlation_id: str,
    ) -> InvokeResult:
        if self.capabilities and tool not in self.capabilities:
            raise AdapterError("CAPABILITY_UNSUPPORTED", tool)
        body = {
            "op": "invoke",
            "tool": tool,
            "input": dict(payload),
            "deadline": isoformat_utc(deadline),
            "secret_handles": list(secret_handles),
            "correlation_id": correlation_id,
        }
        response = self._post("invoke", body, correlation_id=correlation_id, extra={})
        self._check_status(response, 200)
        data = self._json(response)
        if data.get("error_code") == "CAPABILITY_UNSUPPORTED":
            raise AdapterError("CAPABILITY_UNSUPPORTED", tool)
        return InvokeResult(
            result=dict(data.get("result", {})),
            usage=usage_from(data),
            events=list(data.get("events", [])),
            artifacts=list(data.get("artifacts", [])),
            correlation_id=str(data.get("correlation_id", correlation_id)),
            task_id=data.get("task_id"),
            event_id=data.get("event_id"),
        )

    def cancel(self, target_id: str) -> CancelAck:
        response = self._post(
            "cancel",
            {"op": "cancel", "target_id": target_id},
            correlation_id=f"cancel:{target_id}",
            extra={},
        )
        self._check_status(response, 200)
        now = self._clock.now()
        return CancelAck(target_id, now, now + dt.timedelta(seconds=60))

    def heartbeat(self) -> Heartbeat:
        """Webhook Agents report heartbeats to the server (REST) and the last one is returned;
        with ``endpoint.health_check`` the server asks the endpoint (``op=heartbeat``) instead,
        so that liveness and capacity can be confirmed on demand."""
        if not self.health_check:
            if self._last_heartbeat is None:
                raise AdapterError("ADAPTER_UNREACHABLE", "no heartbeat recorded yet")
            return self._last_heartbeat
        response = self._post(
            "heartbeat",
            {"op": "heartbeat", "agent_id": self.agent_id},
            correlation_id=f"heartbeat:{self.agent_id}",
            extra={},
        )
        self._check_status(response, 200)
        data = self._json(response)
        health = str(data.get("health", "ok"))
        if health not in ("ok", "degraded", "draining"):
            raise AdapterError("ADAPTER_BAD_RESPONSE", f"health {health!r}")
        beat = Heartbeat(
            reported_at=self._clock.now(),
            health=health,
            capacity=int(data.get("capacity", 1)),
            usage_since_last=usage_from(data),
            capabilities=tuple(str(c) for c in data.get("capabilities", self.capabilities)),
        )
        self._last_heartbeat = beat
        return beat

    def record_heartbeat(self, heartbeat: Heartbeat) -> None:
        self._last_heartbeat = heartbeat

    def normalize_error(self, exc: BaseException) -> AdapterError:
        if isinstance(exc, AdapterError):
            return exc
        if isinstance(exc, httpx.TimeoutException):
            return AdapterError("ADAPTER_TIMEOUT", "endpoint timed out", retryable=True)
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status in (401, 403):
                return AdapterError("ADAPTER_AUTH_FAILED", f"endpoint returned {status}")
            if status == 429:
                return AdapterError("ADAPTER_RATE_LIMITED", "endpoint rate limited", True)
            if status >= 500:
                return AdapterError("ADAPTER_UNREACHABLE", f"endpoint returned {status}", True)
            return AdapterError("ADAPTER_BAD_RESPONSE", f"endpoint returned {status}")
        if isinstance(exc, httpx.TransportError):
            return AdapterError("ADAPTER_UNREACHABLE", "endpoint unreachable", retryable=True)
        return AdapterError("ADAPTER_INTERNAL", type(exc).__name__)


def envelope_for(item: WorkItemView) -> dict[str, Any]:
    """The §7B.1 work item envelope body (validated against ``colab.work-item.v1``)."""
    body: dict[str, Any] = {
        "schema_id": "colab.work-item.v1",
        "work_item_id": item.work_item_id,
        "kind": item.kind,
        "agent_id": item.agent_id,
        "task_id": item.task_id,
        "correlation_id": item.correlation_id,
        "deadline": isoformat_utc(item.deadline),
        "payload_ref": item.payload_ref,
        "payload_size_bytes": int(item.payload.get("payload_size_bytes", 0)),
        "secret_handles": list(item.secret_handles),
        "expected_result_schema": item.expected_result_schema,
        "idempotency_key": item.idempotency_key,
    }
    if item.payload.get("brainstorm_id"):
        body["brainstorm_id"] = item.payload["brainstorm_id"]
    validate("work_item", body)
    return body


def _factory(endpoint: Mapping[str, Any]) -> WebhookAdapter:
    return WebhookAdapter(endpoint)


register_adapter_type(ADAPTER_TYPE, _factory)
