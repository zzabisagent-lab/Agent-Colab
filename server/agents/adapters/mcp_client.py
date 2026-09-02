"""MCP-client adapter type ("mcp"): pull delivery through the Agent-Colab MCP server (§7B.2).

The Agent is an MCP *client* that long-polls ``work_poll`` (or subscribes to its inbox resource)
and answers with ``work_result``. This adapter therefore never opens a connection to the Agent:
``deliver`` queues a durable work item, ``invoke`` queues an ``invoke`` item and awaits its
result, ``cancel`` queues a ``cancel`` item, ``heartbeat`` reads the latest heartbeat the Agent
reported. The persistence behind these operations is an :class:`InboxPort` — the production
:class:`DbInboxPort` uses the work-item core, the conformance harness uses an in-memory port.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from server.agents.adapters.contract import (
    Adapter,
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

CANCEL_ACK_S = 10
CANCEL_CLEANUP_S = 60


class InboxPort(Protocol):
    """Persistence and Agent-side observation behind the MCP adapter."""

    def enqueue(self, item: WorkItemView) -> DeliveryReceipt: ...

    def await_result(self, work_item_id: str, deadline: dt.datetime) -> dict[str, Any] | None: ...

    def cancel(self, target_id: str, correlation_id: str) -> CancelAck: ...

    def latest_heartbeat(self, agent_id: str) -> Heartbeat | None: ...

    def now(self) -> dt.datetime: ...


def identity_hash(
    agent_id: str,
    adapter_type: str,
    capabilities: Sequence[str],
    modes: Sequence[str],
    secret_handles: str,
) -> str:
    material = json.dumps(
        {
            "agent_id": agent_id,
            "adapter_type": adapter_type,
            "capabilities": sorted(capabilities),
            "delivery_modes": sorted(modes),
            "secret_handles": secret_handles,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(material.encode()).hexdigest()


def usage_from_result(result: Mapping[str, Any]) -> Usage:
    u = result.get("usage")
    if isinstance(u, Mapping):
        return Usage(
            model=u.get("model"),
            input_tokens=int(u.get("input_tokens", 0)),
            output_tokens=int(u.get("output_tokens", 0)),
            tool_calls=int(u.get("tool_calls", 0)),
            wall_time_ms=int(u.get("wall_time_ms", 0)),
            cost_units=u.get("cost_units"),
        )
    reason = result.get("usage_unavailable")
    if isinstance(reason, Mapping):
        reason = reason.get("reason")
    return Usage(usage_unavailable=str(reason) if reason else "ADAPTER_NO_METERING")


@dataclass
class McpClientAdapter:
    """§7.3 contract for MCP-client Agents (pull mode only)."""

    endpoint: Mapping[str, Any]
    port: InboxPort
    adapter_type: str = "mcp"
    deliveries: dict[str, DeliveryReceipt] = field(default_factory=dict)

    # ---- identity ---------------------------------------------------------------------------
    @property
    def agent_id(self) -> str:
        return str(self.endpoint["agent_id"])

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(str(c) for c in self.endpoint.get("capabilities", ()))

    def probe(self) -> Probe:
        caps = self.capabilities
        modes = (DeliveryMode.PULL,)
        secret = "supported" if self.endpoint.get("secret_handles", True) else "unsupported"
        return Probe(
            agent_id=self.agent_id,
            adapter_type=self.adapter_type,
            runtime=dict(self.endpoint.get("runtime", {})),
            capabilities=caps,
            unsupported=tuple(str(u) for u in self.endpoint.get("unsupported", ())),
            delivery_modes=modes,
            limits=dict(self.endpoint.get("limits", {})),
            secret_handles=secret,
            identity_hash=identity_hash(
                self.agent_id, self.adapter_type, caps, [m.value for m in modes], secret
            ),
        )

    # ---- delivery ---------------------------------------------------------------------------
    def deliver(self, item: WorkItemView) -> DeliveryReceipt:
        if item.work_item_id in self.deliveries:  # idempotent per work item (CS-02)
            return self.deliveries[item.work_item_id]
        tool = str(item.payload.get("tool", "")) if item.kind == "invoke" else ""
        if tool and tool not in self.capabilities:
            receipt = DeliveryReceipt(
                item.work_item_id, None, rejection_code="CAPABILITY_UNSUPPORTED"
            )
        elif item.secret_handles and not self.endpoint.get("secret_handles", True):
            receipt = DeliveryReceipt(
                item.work_item_id, None, rejection_code="CAPABILITY_UNSUPPORTED"
            )
        else:
            receipt = self.port.enqueue(item)
        self.deliveries[item.work_item_id] = receipt
        return receipt

    def invoke(
        self,
        tool: str,
        payload: Mapping[str, Any],
        deadline: dt.datetime,
        secret_handles: Sequence[str],
        *,
        correlation_id: str,
    ) -> InvokeResult:
        if tool not in self.capabilities:
            raise AdapterError("CAPABILITY_UNSUPPORTED", f"tool {tool} is not advertised")
        key = str(payload.get("idempotency_key") or f"{correlation_id}:{tool}")
        work_item_id = "wi-" + hashlib.sha256(f"{self.agent_id}|{key}".encode()).hexdigest()[:24]
        item = WorkItemView(
            work_item_id=work_item_id,
            kind="invoke",
            agent_id=self.agent_id,
            task_id=payload.get("task_id"),
            correlation_id=correlation_id,
            deadline=deadline,
            payload_ref=f"colab://work/{work_item_id}/payload",
            secret_handles=tuple(secret_handles),
            expected_result_schema="colab.work-result.v1",
            idempotency_key=key,
            payload={"tool": tool, "input": dict(payload.get("input", payload))},
        )
        receipt = self.deliver(item)
        if receipt.rejection_code:
            raise AdapterError("CAPABILITY_UNSUPPORTED", receipt.rejection_code)
        result = self.port.await_result(work_item_id, deadline)
        if result is None:
            raise AdapterError("ADAPTER_TIMEOUT", f"no result before {deadline.isoformat()}", True)
        if result.get("status") == "FAILED":
            raise self.normalize_error(
                RuntimeError(str(result.get("error_code", "ADAPTER_INTERNAL")))
            )
        return InvokeResult(
            result=dict(result.get("result", {})),
            usage=usage_from_result(result),
            events=tuple(result.get("events", ())),
            artifacts=tuple(result.get("artifacts", ())),
            correlation_id=str(result.get("correlation_id", correlation_id)),
            task_id=result.get("task_id"),
            event_id=result.get("event_id"),
        )

    def cancel(self, target_id: str) -> CancelAck:
        return self.port.cancel(target_id, correlation_id=f"cancel:{target_id}")

    def heartbeat(self) -> Heartbeat:
        hb = self.port.latest_heartbeat(self.agent_id)
        if hb is None:
            raise AdapterError("ADAPTER_UNREACHABLE", "no heartbeat reported yet", True)
        return hb

    def normalize_error(self, exc: BaseException) -> AdapterError:
        if isinstance(exc, AdapterError):
            return exc
        if isinstance(exc, TimeoutError):
            return AdapterError("ADAPTER_TIMEOUT", str(exc), True)
        if isinstance(exc, PermissionError):  # before OSError: PermissionError is an OSError
            return AdapterError("ADAPTER_AUTH_FAILED", str(exc))
        if isinstance(exc, ConnectionError | OSError):
            return AdapterError("ADAPTER_UNREACHABLE", str(exc), True)
        if isinstance(exc, ValueError | KeyError | TypeError):
            return AdapterError("ADAPTER_BAD_RESPONSE", str(exc))
        text = str(exc)
        if "CAPABILITY_UNSUPPORTED" in text:
            return AdapterError("CAPABILITY_UNSUPPORTED", text)
        if "RATE" in text.upper() and "LIMIT" in text.upper():
            return AdapterError("ADAPTER_RATE_LIMITED", text, True)
        if "CANCEL" in text.upper():
            return AdapterError("ADAPTER_CANCELLED", text)
        return AdapterError("ADAPTER_INTERNAL", text)


# ------------------------------------------------------------------- production port


@dataclass
class DbInboxPort:
    """InboxPort on the durable work-item core (``server.work.inbox``)."""

    runtime: Any  # server.api.dispatch.Runtime
    workspace_id: str
    actor_account_id: str
    poll_interval_s: float = 0.5

    def now(self) -> dt.datetime:
        return self.runtime.clock.now()  # type: ignore[no-any-return]

    def enqueue(self, item: WorkItemView) -> DeliveryReceipt:
        from server.db.engine import session_scope
        from server.work import inbox

        with session_scope(self.runtime.session_factory) as session:
            stored = inbox.enqueue(
                session,
                self.runtime.store_for(session),
                workspace_id=self.workspace_id,
                kind=item.kind,
                agent_id=item.agent_id,
                payload=dict(item.payload),
                deadline=item.deadline,
                expected_result_schema=item.expected_result_schema,
                correlation_id=item.correlation_id,
                idempotency_key=item.idempotency_key,
                actor_account_id=self.actor_account_id,
                clock=self.runtime.clock,
                task_id=item.task_id,
                secret_handles=list(item.secret_handles),
                work_item_id=item.work_item_id,
            )
            return DeliveryReceipt(
                stored.work_item_id, stored.created_at, receipt_id=stored.work_item_id
            )

    def await_result(self, work_item_id: str, deadline: dt.datetime) -> dict[str, Any] | None:
        from server.db.engine import session_scope
        from server.work import receipts

        while True:
            with session_scope(self.runtime.session_factory) as session:
                receipt = receipts.result_receipt_of(session, work_item_id)
                if receipt is not None and receipt.result_ref:
                    # the result body is not retained by the core (Events/Artifacts carry
                    # it); usage was recorded in usage_records by work_result
                    return {
                        "status": "SUCCEEDED",
                        "result": {"result_ref": receipt.result_ref},
                        "usage_unavailable": {"reason": "RESULT_REF_ONLY"},
                    }
            if self.now() >= deadline:
                return None
            time.sleep(self.poll_interval_s)

    def cancel(self, target_id: str, correlation_id: str) -> CancelAck:
        from server.db.engine import session_scope
        from server.work import inbox
        from server.work.state import WorkItemError

        now = self.now()
        with session_scope(self.runtime.session_factory) as session:
            try:
                inbox.cancel(
                    session,
                    self.runtime.store_for(session),
                    target_id,
                    "ADAPTER_CANCEL",
                    actor_account_id=self.actor_account_id,
                    clock=self.runtime.clock,
                )
            except WorkItemError as exc:
                raise AdapterError("ADAPTER_BAD_RESPONSE", exc.detail) from exc
        return CancelAck(target_id, now, now + dt.timedelta(seconds=CANCEL_CLEANUP_S))

    def latest_heartbeat(self, agent_id: str) -> Heartbeat | None:
        from sqlalchemy import text

        from server.db.engine import session_scope

        with session_scope(self.runtime.session_factory) as session:
            row = session.execute(
                text(
                    "SELECT reported_at, health, capacity, usage FROM agent_heartbeats "
                    "WHERE agent_id = :a ORDER BY reported_at DESC LIMIT 1"
                ),
                {"a": agent_id},
            ).first()
        if row is None:
            return None
        usage = row[3] if isinstance(row[3], dict) else json.loads(row[3] or "{}")
        return Heartbeat(
            row[0],
            str(row[1]),
            int(row[2]),
            usage_from_result(
                {"usage": usage} if usage and "usage_unavailable" not in usage else usage or {}
            ),
        )


def _factory(endpoint: Mapping[str, Any]) -> Adapter:
    port = endpoint.get("_port")
    if port is None:
        runtime = endpoint.get("_runtime")
        if runtime is None:
            raise AdapterError("ADAPTER_INTERNAL", "mcp adapter needs an inbox port or runtime")
        port = DbInboxPort(
            runtime, str(endpoint["workspace_id"]), str(endpoint["actor_account_id"])
        )
    return McpClientAdapter(endpoint, port)


register_adapter_type("mcp", _factory, replace=True)
_ = uuid  # re-exported for ports that mint ids
