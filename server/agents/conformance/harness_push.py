"""Conformance harnesses for the push adapter types (webhook, mattermost_bot).

Both reuse :class:`SimulatedAgent` for the Agent side (timing, idempotent side effects, results,
disconnect/reconnect, failure injection). The webhook harness puts a signature-verifying fake
endpoint behind ``httpx.MockTransport``; the bot harness feeds the adapter's message sink and
health probe. Secret handle values never reach the harness logs.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from server.agents import webhook_signing as ws
from server.agents.adapters.contract import Adapter, CancelAck, Heartbeat, Usage, WorkItemView
from server.agents.adapters.mattermost_bot import MattermostBotAdapter
from server.agents.adapters.webhook import HEADER_OP, WebhookAdapter
from server.agents.conformance.harness import SimulatedAgent, VirtualClock, register_harness
from server.agents.signing_keys import StaticSigningKeyResolver

KEY_REF = "sec-conformance-hook@v1"
KEY = b"conformance-signing-key-0123456789ab"


def _iso_ms(when: dt.datetime) -> str:
    """The receipt schema's timestamp form: UTC with milliseconds and a ``Z`` suffix."""
    return (
        when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
    )


def _usage_doc(usage: Usage) -> dict[str, Any]:
    if usage.usage_unavailable:
        return {"usage_unavailable": {"reason": usage.usage_unavailable}}
    return {
        "usage": {
            "model": usage.model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "tool_calls": usage.tool_calls,
            "wall_time_ms": usage.wall_time_ms,
        }
    }


def _view_from_envelope(body: Mapping[str, Any]) -> WorkItemView:
    deadline = body.get("deadline")
    return WorkItemView(
        work_item_id=str(body["work_item_id"]),
        kind=str(body.get("kind", "invoke")),
        agent_id=str(body.get("agent_id", "")),
        task_id=body.get("task_id"),
        correlation_id=str(body.get("correlation_id", "")),
        deadline=dt.datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
        if deadline
        else dt.datetime.now(dt.UTC),
        payload_ref=str(body.get("payload_ref", "")),
        secret_handles=tuple(str(h) for h in body.get("secret_handles", [])),
        expected_result_schema=str(body.get("expected_result_schema", "colab.work-result.v1")),
        idempotency_key=str(body.get("idempotency_key", body["work_item_id"])),
        payload=dict(body.get("payload", {})),
    )


class _Transport:
    """Base for both harnesses: shared bookkeeping over a SimulatedAgent."""

    def __init__(self, agent: SimulatedAgent, clock: VirtualClock) -> None:
        self.agent = agent
        self.clock = clock

    def side_effects(self, work_item_id: str) -> int:
        return self.agent.side_effect_log.get(work_item_id, 0)

    def results(self, work_item_id: str) -> int:
        return self.agent.result_count.get(work_item_id, 0)

    def acked_at(self, work_item_id: str) -> dt.datetime | None:
        rec = self.agent.items.get(work_item_id)
        return None if rec is None else rec.acked_at

    def accepted_at(self, work_item_id: str) -> dt.datetime | None:
        rec = self.agent.items.get(work_item_id)
        return None if rec is None else rec.accepted_at

    def logs(self) -> list[str]:
        lines = list(self.agent.log_lines)
        for rec in self.agent.items.values():
            if rec.result is not None:
                lines.append(json.dumps(rec.result, sort_keys=True))
        return lines

    def inject_failure(self, kind: str | None) -> None:
        self.agent.failure = kind

    def disconnect(self) -> None:
        self.agent.connected = False

    def reconnect(self) -> list[str]:
        return self.agent.reconnect()

    def cancel_timing(self, ack: CancelAck) -> tuple[float, float]:
        issued = self.clock.now()
        return (
            (ack.acknowledged_at - issued).total_seconds(),
            (ack.cleanup_deadline - ack.acknowledged_at).total_seconds(),
        )


@dataclass
class WebhookSimulationHarness(_Transport):
    """``webhook`` type: a signature-verifying fake endpoint behind httpx.MockTransport."""

    endpoint: Mapping[str, Any]
    agent_factory: Callable[[VirtualClock], SimulatedAgent] = lambda clock: SimulatedAgent(clock)
    adapter_type: str = "webhook"
    clock: VirtualClock = field(default_factory=VirtualClock)
    tools: tuple[str, ...] = ("cap_echo",)

    def __post_init__(self) -> None:
        super().__init__(self.agent_factory(self.clock), self.clock)
        self.nonces = ws.InMemoryNonceStore()
        self.agent_id = str(self.endpoint.get("agent_id", "agent-conf-hook"))
        cfg = {
            "url": str(self.endpoint.get("url", "https://agent.conformance.test/colab")),
            "agent_id": self.agent_id,
            "credential_ref": KEY_REF,
            "capabilities": list(self.tools),
            "health_check": True,
        }
        self._adapter = WebhookAdapter(
            cfg,
            resolver=StaticSigningKeyResolver({KEY_REF: KEY}),
            clock=self.clock,
            transport=httpx.MockTransport(self._handle),
        )

    def adapter(self) -> Adapter:
        return self._adapter

    # ------------------------------------------------------------------ fake endpoint
    def _injected(self) -> httpx.Response | None:
        kind = self.agent.failure
        if kind is None:
            return None
        self.agent.failure = None  # one-shot, like the MCP simulation
        if kind == "timeout":
            raise httpx.ReadTimeout("simulated timeout")
        if kind == "unreachable":
            raise httpx.ConnectError("simulated connection refused")
        if kind == "auth":
            return httpx.Response(401, json={"error": "bad signature"})
        if kind == "bad_response":
            return httpx.Response(200, content=b"<html>not json</html>")
        if kind == "rate_limited":
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(500, json={"error": kind})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        injected = self._injected()
        if injected is not None:
            return injected
        body = request.content
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            ws.verify(
                KEY,
                {
                    ws.HEADER_TIMESTAMP: headers.get(ws.HEADER_TIMESTAMP.lower(), ""),
                    ws.HEADER_NONCE: headers.get(ws.HEADER_NONCE.lower(), ""),
                    ws.HEADER_SIGNATURE: headers.get(ws.HEADER_SIGNATURE.lower(), ""),
                },
                body,
                self.clock,
                self.nonces,
            )
        except ws.WebhookError as exc:
            return httpx.Response(401, json={"error": exc.code})
        data = json.loads(body)
        op = headers.get(HEADER_OP.lower(), data.get("op", ""))
        if op == "probe":
            return httpx.Response(
                200,
                json={
                    "schema_id": "colab.probe-response.v1",
                    "identity": {
                        "agent_id": self.agent_id,
                        "adapter_type": "webhook",
                        "instance_fingerprint": "sha256:" + "c" * 64,
                    },
                    "runtime": {"product": "conformance-http-agent"},
                    "capabilities": {
                        "tools": list(self.tools),
                        "unsupported": ["tool_not_advertised"],
                        "secret_handles": "supported",
                        "cancel": "supported",
                    },
                    "delivery_modes": ["push"],
                    "limits": {"concurrent_tasks": 3},
                },
            )
        if op == "deliver":
            view = _view_from_envelope(data)
            receipt = self.agent.enqueue(view)
            return httpx.Response(
                202,
                json={
                    "schema_id": "colab.delivery-receipt.v1",
                    "work_item_id": receipt.work_item_id,
                    "correlation_id": view.correlation_id,
                    "accepted_at": _iso_ms(receipt.accepted_at or self.clock.now()),
                },
            )
        if op == "invoke":
            tool = str(data.get("tool"))
            if tool not in self.tools:
                return httpx.Response(200, json={"error_code": "CAPABILITY_UNSUPPORTED"})
            secrets = list(data.get("secret_handles", []))
            self.agent.log_lines.append(f"invoke {tool} with {len(secrets)} secret handle(s)")
            doc: dict[str, Any] = {
                "result": {"echo": dict(data.get("input", {})), "tool": tool},
                "correlation_id": data.get("correlation_id")
                if self.agent.echo_correlation
                else "corr-lost",
            }
            doc.update(
                _usage_doc(
                    Usage(
                        model="hook-1",
                        input_tokens=10,
                        output_tokens=5,
                        tool_calls=1,
                        wall_time_ms=40,
                    )
                    if self.agent.include_usage
                    else Usage(usage_unavailable="ADAPTER_NO_METERING")
                )
            )
            if self.agent.leak_secrets and secrets:
                doc["result"]["debug"] = secrets[0]
            return httpx.Response(200, json=doc)
        if op == "cancel":
            self.agent.log_lines.append(f"cancel {data.get('target_id')}")
            return httpx.Response(200, json={"acknowledged": True})
        if op == "heartbeat":
            beat = self.agent.latest_heartbeat(self.agent_id)
            assert beat is not None
            return httpx.Response(
                200,
                json={
                    "health": beat.health,
                    "capacity": beat.capacity,
                    "capabilities": list(beat.capabilities),
                    **_usage_doc(beat.usage_since_last),
                },
            )
        return httpx.Response(400, json={"error": "unknown op"})

    def heartbeats(self, count: int) -> list[Heartbeat]:
        out: list[Heartbeat] = []
        for _ in range(count):
            out.append(self._adapter.heartbeat())
            self.clock.advance(self.agent.heartbeat_interval_s)
        return out


@dataclass
class BotSimulationHarness(_Transport):
    """``mattermost_bot`` type: the adapter's message sink and health probe are simulated."""

    endpoint: Mapping[str, Any]
    agent_factory: Callable[[VirtualClock], SimulatedAgent] = lambda clock: SimulatedAgent(clock)
    adapter_type: str = "mattermost_bot"
    clock: VirtualClock = field(default_factory=VirtualClock)
    tools: tuple[str, ...] = ("cap_echo",)

    def __post_init__(self) -> None:
        super().__init__(self.agent_factory(self.clock), self.clock)
        self.messages: list[dict[str, Any]] = []
        cfg = {
            "agent_id": str(self.endpoint.get("agent_id", "agent-conf-bot")),
            "provider_instance_id": str(self.endpoint.get("provider_instance_id", "mm:conf")),
            "bot_user_id": str(self.endpoint.get("bot_user_id", "bot-conf")),
            "bot_username": "conformance-bot",
            "capabilities": list(self.tools),
        }
        self._adapter = MattermostBotAdapter(
            cfg, sink=self._sink, clock=self.clock, health_probe=self._health
        )

    def adapter(self) -> Adapter:
        return self._adapter

    def _sink(self, item: WorkItemView, message: dict[str, Any]) -> str | None:
        self.messages.append(message)
        self.agent.enqueue(item)  # the bot reads the thread and acts (idempotent per item)
        return f"post-{item.work_item_id}"

    def _health(self) -> None:
        self.agent._raise_injected()

    def logs(self) -> list[str]:
        return super().logs() + [str(m.get("message", "")) for m in self.messages]

    def heartbeats(self, count: int) -> list[Heartbeat]:
        out: list[Heartbeat] = []
        for _ in range(count):
            beat = self.agent.latest_heartbeat(self._adapter.agent_id)
            assert beat is not None
            self._adapter.record_heartbeat(beat)  # bots report through the server (REST)
            out.append(self._adapter.heartbeat())
            self.clock.advance(self.agent.heartbeat_interval_s)
        return out


register_harness("webhook", lambda endpoint: WebhookSimulationHarness(endpoint))
register_harness("mattermost_bot", lambda endpoint: BotSimulationHarness(endpoint))
