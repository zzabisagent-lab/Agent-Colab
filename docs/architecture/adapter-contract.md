# Adapter contract and conformance (P3-03, P3-05, P3-15)

Baseline: development plan §7.3 (contract), §7B (work delivery), §7C (usage); validation plan §11.1
(CS-01..CS-12).

## Contract (`server/agents/adapters/contract.py`)

`Adapter` is a runtime-checkable Protocol: `probe() -> Probe`, `deliver(WorkItemView) ->
DeliveryReceipt`, `invoke(tool, payload, deadline, secret_handles, *, correlation_id) ->
InvokeResult`, `cancel(target_id) -> CancelAck`, `heartbeat() -> Heartbeat`,
`normalize_error(exc) -> AdapterError`. `Probe.identity_hash` must be stable across probes; an
adapter advertises `delivery_modes`, explicit `unsupported` tools and `secret_handles:
supported|unsupported`. `Usage` carries §7C fields or a `usage_unavailable` reason.

Stable error codes (`STABLE_ERROR_CODES`): `ADAPTER_TIMEOUT`, `ADAPTER_UNREACHABLE`,
`ADAPTER_AUTH_FAILED`, `ADAPTER_BAD_RESPONSE`, `ADAPTER_RATE_LIMITED`, `CAPABILITY_UNSUPPORTED`,
`ADAPTER_CANCELLED`, `ADAPTER_INTERNAL`. `AdapterError.retryable` drives redelivery.

Adapter *types* are registered by name: built-ins (`mcp`, and the `webhook` / `mattermost_bot`
types from their packages) on import of `server.agents.adapters`; external plugins through
`AGENT_COLAB_ADAPTER_PLUGINS=module:attribute[,...]` whose registrar receives
`register_adapter_type` (V-P3-12: no core change). `adapter_for(type, endpoint)` builds an
instance; endpoint configs never contain secret values (Secret Broker references only).

## MCP-client adapter (`server/agents/adapters/mcp_client.py`, type `mcp`)

Pull mode only. `deliver` queues the work item (idempotent per `work_item_id`; unadvertised tools
and secret items on `secret_handles: unsupported` are rejected with `CAPABILITY_UNSUPPORTED`),
`invoke` queues an `invoke` item and awaits its result until the deadline (`ADAPTER_TIMEOUT`,
retryable), `cancel` cancels the item (ack now, cleanup ≤ 60 s), `heartbeat` reads the last
reported heartbeat. Persistence is an `InboxPort`: `DbInboxPort` (work-item core) in production,
`SimulatedAgent` in the conformance harness. The core does not retain result bodies, so
`DbInboxPort.await_result` returns the `result_ref` with `usage_unavailable: RESULT_REF_ONLY`;
usage itself was recorded by `work_result`.

## Conformance suite (`server/agents/conformance/`)

`run_suite(harness)` executes CS-01..CS-12 against `harness.adapter()`; the `Harness` protocol
supplies the virtual clock, side-effect/result counters, ack/accept timestamps, logs (for the
secret scan), fault injection (`timeout|unreachable|auth|bad_response|rate_limited`), and
disconnect/reconnect. `McpSimulationHarness` is the built-in harness; other adapter types register
theirs with `register_harness`. CLI: `python -m server.agents.conformance --adapter <type>
--endpoint '<json>' [--out report.json]` (exit 1 on FAIL). Report schema:
`schemas/documents/adapter-conformance-report.v1.schema.json` (12 checks, PASS/FAIL, evidence).

| Check | Rule enforced |
|---|---|
| CS-01 | identity hash, capabilities, delivery modes identical over 3 probes |
| CS-02 | same work item delivered twice → same receipt, 1 side effect |
| CS-03 | ack ≤ 60 s, accept ≤ 120 s (task_assignment) |
| CS-04 | invoke result validates against `work-result.v1`; usage or `usage_unavailable` reason |
| CS-05 | cancel acknowledged ≤ 10 s, cleanup ≤ 60 s |
| CS-06 | heartbeats 30 s apart with capacity and usage/reason |
| CS-07 | secret handle values absent from logs/results; unsupported adapters advertise it and reject |
| CS-08 | correlation/task ids echoed 100 % |
| CS-09 | 3 deliveries of one item → 1 side effect, ≤ 1 result |
| CS-10 | unadvertised tool → `CAPABILITY_UNSUPPORTED` |
| CS-11 | 5 injected failure kinds → the expected stable codes |
| CS-12 | un-acked item re-received after reconnect, 1 result |

## Usage conformance (`server/usage/conformance.py`, P3-15)

`normalize_usage` classifies a result/heartbeat payload (usage, explicit reason, or
non-conformant → recorded as unavailable with `ADAPTER_NO_METERING`). `record_result_usage`
runs inside `work_result` (once per accepted result; skipped with a warning when no pricing
version is activated), `record_heartbeat_usage` is for the registry's heartbeat command, and
`usage_unavailable_ratio(session, agent_id, start, end)` reports total/unavailable/estimated
counts for V-P3-26.
