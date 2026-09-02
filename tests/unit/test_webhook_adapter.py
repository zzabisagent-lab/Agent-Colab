"""P3-11/P3-04 (V-P3-22, CS-01/04/05/10/11): webhook adapter — signed envelope, receipts,
error normalization, secret handles as opaque ids only."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from server.agents import webhook_signing as ws
from server.agents.adapters.contract import AdapterError, DeliveryMode, WorkItemView
from server.agents.adapters.webhook import HEADER_DELIVERY_NO, HEADER_OP, WebhookAdapter
from server.agents.signing_keys import StaticSigningKeyResolver, env_name_for
from server.domain.clock import FixedClock

KEY = b"k" * 32
REF = "sec-agent-hook@v1"
T0 = dt.datetime(2026, 5, 1, 9, 0, tzinfo=dt.UTC)
SECRET_VALUE = "hunter2-should-never-travel"


class FakeEndpoint:
    """An Agent endpoint that verifies the server's signature like a real one would."""

    def __init__(self, clock: FixedClock, *, key: bytes = KEY) -> None:
        self.clock = clock
        self.key = key
        self.nonces = ws.InMemoryNonceStore()
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.side_effects: dict[str, int] = {}
        self.fail_times = 0
        self.status_override: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        headers = {k: v for k, v in request.headers.items()}
        try:
            ws.verify(
                self.key,
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
        op = headers[HEADER_OP.lower()]
        self.calls.append((op, data, headers))
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={"error": "forced"})
        if self.fail_times > 0:
            self.fail_times -= 1
            return httpx.Response(503, json={"error": "down"})
        if op == "probe":
            return httpx.Response(
                200,
                json={
                    "schema_id": "colab.probe-response.v1",
                    "identity": {
                        "agent_id": data["agent_id"],
                        "adapter_type": "webhook",
                        "instance_fingerprint": "sha256:" + "a" * 64,
                    },
                    "runtime": {"product": "generic-http-agent"},
                    "capabilities": {
                        "tools": ["echo", "summarize"],
                        "unsupported": ["shell"],
                        "secret_handles": "supported",
                        "cancel": "supported",
                    },
                    "delivery_modes": ["push"],
                    "limits": {"concurrent_tasks": 2},
                },
            )
        if op == "deliver":
            wid = data["work_item_id"]
            self.side_effects[wid] = self.side_effects.get(wid, 0) + (
                0 if wid in self.side_effects else 1
            )
            return httpx.Response(
                202,
                json={
                    "schema_id": "colab.delivery-receipt.v1",
                    "work_item_id": wid,
                    "correlation_id": data["correlation_id"],
                    "accepted_at": "2026-05-01T09:00:01.000Z",
                },
            )
        if op == "invoke":
            if data["tool"] == "shell":
                return httpx.Response(200, json={"error_code": "CAPABILITY_UNSUPPORTED"})
            return httpx.Response(
                200,
                json={
                    "result": {"echo": data["input"]},
                    "correlation_id": data["correlation_id"],
                    "usage": {
                        "model": "m",
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "tool_calls": 0,
                        "wall_time_ms": 3,
                    },
                },
            )
        if op == "cancel":
            return httpx.Response(200, json={"acknowledged": True})
        return httpx.Response(400, json={"error": "unknown op"})


def _adapter(endpoint: FakeEndpoint, clock: FixedClock, **over: Any) -> WebhookAdapter:
    cfg = {
        "url": "https://agent.example.test/colab",
        "agent_id": "agent-hook-1",
        "credential_ref": REF,
        "capabilities": ["echo", "summarize"],
        **over,
    }
    return WebhookAdapter(
        cfg,
        resolver=StaticSigningKeyResolver({REF: KEY}),
        clock=clock,
        transport=httpx.MockTransport(endpoint.handler),
    )


def _item(wid: str = "wi-0123456789abcdef", handles: tuple[str, ...] = ()) -> WorkItemView:
    return WorkItemView(
        work_item_id=wid,
        kind="invoke",
        agent_id="agent-hook-1",
        task_id="task-hook-1",
        correlation_id="corr-hook-1",
        deadline=T0 + dt.timedelta(hours=1),
        payload_ref=f"colab://work/{wid}/payload",
        secret_handles=handles,
        expected_result_schema="colab.work-result.v1",
        idempotency_key="idem-hook-1",
        payload={"delivery_no": 2, "payload_size_bytes": 12},
    )


def test_probe_identity_stable_and_unsupported_declared() -> None:
    clock = FixedClock(T0)
    ep = FakeEndpoint(clock)
    adapter = _adapter(ep, clock)
    probes = [adapter.probe() for _ in range(3)]
    assert len({p.identity_hash for p in probes}) == 1  # CS-01
    assert probes[0].delivery_modes == (DeliveryMode.PUSH,)
    assert "shell" in probes[0].unsupported and probes[0].secret_handles == "supported"
    assert all(c[0] == "probe" for c in ep.calls) and len(ep.calls) == 3


def test_deliver_sends_signed_envelope_and_returns_receipt() -> None:
    clock = FixedClock(T0)
    ep = FakeEndpoint(clock)
    adapter = _adapter(ep, clock)
    receipt = adapter.deliver(_item(handles=("sh-0000000a",)))
    assert receipt.accepted_at is not None and receipt.rejection_code is None
    op, body, headers = ep.calls[-1]
    assert op == "deliver" and headers[HEADER_DELIVERY_NO.lower()] == "2"
    assert body["schema_id"] == "colab.work-item.v1" and body["secret_handles"] == ["sh-0000000a"]
    assert body["payload_ref"].startswith("colab://work/")
    assert headers[ws.HEADER_KEY_REF.lower()] == REF
    assert SECRET_VALUE not in json.dumps(body) and KEY.decode() not in json.dumps(headers)
    # same work item delivered twice → endpoint reports one side effect (CS-02 on the Agent side)
    adapter.deliver(_item())
    assert ep.side_effects["wi-0123456789abcdef"] == 1


def test_endpoint_rejects_tampered_signature_and_reused_nonce() -> None:
    clock = FixedClock(T0)
    ep = FakeEndpoint(clock, key=b"other-key" * 4)  # the Agent holds a different key
    adapter = _adapter(ep, clock)
    with pytest.raises(AdapterError) as exc:
        adapter.deliver(_item())
    assert exc.value.code == "ADAPTER_AUTH_FAILED" and not ep.calls  # 401 before any effect
    # nonce reuse: replaying the exact same signed request is rejected by the endpoint
    ep2 = FakeEndpoint(clock)
    raw = json.dumps({"op": "probe", "agent_id": "agent-hook-1"}).encode()
    headers = ws.sign(KEY, raw, clock, key_ref=REF)
    req = httpx.Request("POST", "https://x/", content=raw, headers={**headers, HEADER_OP: "probe"})
    assert ep2.handler(req).status_code == 200
    assert ep2.handler(req).status_code == 401  # replay


def test_invoke_cancel_and_capability_unsupported() -> None:
    clock = FixedClock(T0)
    ep = FakeEndpoint(clock)
    adapter = _adapter(ep, clock)
    res = adapter.invoke(
        "echo", {"x": 1}, T0 + dt.timedelta(minutes=5), ["sh-0000000b"], correlation_id="c1"
    )
    assert res.result == {"echo": {"x": 1}} and res.usage.output_tokens == 2
    assert res.correlation_id == "c1"  # CS-08
    with pytest.raises(AdapterError) as unsupported:
        adapter.invoke("shell", {}, T0, [], correlation_id="c2")
    assert unsupported.value.code == "CAPABILITY_UNSUPPORTED"  # CS-10 (not advertised)
    ack = adapter.cancel("task-hook-1")
    assert (ack.cleanup_deadline - ack.acknowledged_at) == dt.timedelta(seconds=60)


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (503, "ADAPTER_UNREACHABLE", True),
        (500, "ADAPTER_UNREACHABLE", True),
        (401, "ADAPTER_AUTH_FAILED", False),
        (429, "ADAPTER_RATE_LIMITED", True),
        (418, "ADAPTER_BAD_RESPONSE", False),
    ],
)
def test_error_normalization_status(status: int, code: str, retryable: bool) -> None:
    clock = FixedClock(T0)
    ep = FakeEndpoint(clock)
    ep.status_override = status
    adapter = _adapter(ep, clock)
    with pytest.raises(AdapterError) as exc:
        adapter.deliver(_item())
    assert exc.value.code == code and exc.value.retryable is retryable  # CS-11


def test_error_normalization_transport() -> None:
    clock = FixedClock(T0)

    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def garbage(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, content=b"not json")

    for handler, code in (
        (timeout, "ADAPTER_TIMEOUT"),
        (down, "ADAPTER_UNREACHABLE"),
        (garbage, "ADAPTER_BAD_RESPONSE"),
    ):
        adapter = WebhookAdapter(
            {"url": "https://x/", "agent_id": "agent-hook-1", "credential_ref": REF},
            resolver=StaticSigningKeyResolver({REF: KEY}),
            clock=clock,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(AdapterError) as exc:
            adapter.deliver(_item())
        assert exc.value.code == code
    with pytest.raises(AdapterError) as hb:
        adapter.heartbeat()
    assert hb.value.code == "ADAPTER_UNREACHABLE"


def test_signing_key_reference_never_resolves_to_a_logged_value() -> None:
    assert env_name_for("sec-agent-hook@v1") == "AGENT_COLAB_WEBHOOK_KEY_SEC_AGENT_HOOK_V1"
    with pytest.raises(AdapterError):
        env_name_for("not a ref")
    with pytest.raises(AdapterError) as exc:
        StaticSigningKeyResolver({}).resolve(REF)
    assert KEY.decode() not in str(exc.value)
