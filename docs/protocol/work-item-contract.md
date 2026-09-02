# Agent work-item, usage, and webhook contract v1 (P0-11)

Authority: development plan §7.3, §7B, §7C, §7D.3, §21.1; spec §4.2. Machine-readable form under
`schemas/adapters/` and `schemas/api/pricing.v1.schema.json`; code in `server/work/`,
`server/usage/pricing.py`, `server/agents/webhook_signing.py`; fixtures in
`tests/fixtures/work` and `tests/fixtures/usage`; MCP transport spike in `docs/protocol/mcp-spike.md`.

## Schemas (JSON Schema 2020-12)

| Schema | `schema_id` | Purpose |
|---|---|---|
| `work-item.v1` | `colab.work-item.v1` | durable unit of work (§7B.1); `brainstorm_turn` carries `brainstorm_id`, every other kind `task_id`; `payload_ref = colab://work/<id>/payload`, body never inline, ≤ 1 MB; `secret_handles` are lease handle IDs (`sh-…`), never values |
| `delivery-receipt.v1` | `colab.delivery-receipt.v1` | `deliver()` result: exactly one of `accepted_at` or `rejection_code ∈ {CAPABILITY_UNSUPPORTED, CAPACITY, POLICY, OTHER}` |
| `work-result.v1` | `colab.work-result.v1` | exactly-once result: status, result, events[], artifacts[], and `usage` **or** `usage_unavailable`; FAILED/REJECTED require `error_code`; `display_identity` is schema-visible but ignored + audited (§7A.4) |
| `usage.v1` | — | §7C usage block (`model`, `input_tokens`, `output_tokens`, `tool_calls`, `wall_time_ms`, optional `cost_units`) or `usage_unavailable.reason ∈ {ADAPTER_NO_METERING, MODEL_UNKNOWN, ERROR}` |
| `webhook-envelope.v1` | — | push delivery headers + body + expected `202` receipt (below) |
| `probe-response.v1` | `colab.probe-response.v1` | `probe()` identity (stable `instance_fingerprint`), runtime, capabilities with explicit `unsupported[]`, `secret_handles: supported|unsupported`, `cancel`, `delivery_modes ⊆ {push, pull}`, limits |
| `heartbeat.v1` | `colab.heartbeat.v1` | 30 s heartbeat: health, capacity, `usage_since_last` (usage block) |
| `pricing.v1` | — | `policy/pricing.yaml`: `version = pricing-vN`, `cost_units_per_credit = 1000000`, `default` + per-model rates |

Validation: `server.work.schemas.validate(name, instance)` raises `AdapterSchemaError` with code
`<NAME>_SCHEMA_INVALID` (e.g. `WORK_ITEM_SCHEMA_INVALID`).

## Work item state machine (`server/work/state.py`)

```
QUEUED ─deliver─▶ DELIVERED ─ack─▶ ACKED ─start─▶ IN_PROGRESS ─result─▶ RESULT_RECEIVED
   │                │  ▲redeliver     │                 │
   └cancel/expire   └reject/expire/cancel   └reject/expire/cancel/result   └reject/expire/cancel
```

Terminal: `RESULT_RECEIVED | REJECTED | EXPIRED | CANCELLED` (immutable). Any other
`(state, action)` pair → `WORK_ITEM_TRANSITION_INVALID`. Each transition maps to a
`WORK_ITEM_*` Event (`TRANSITION_EVENTS`).

Timing model `next_action(...)` (pure, Clock supplied by the caller):

| Condition | Decision |
|---|---|
| DELIVERED and no ACK within 60 s | `REDELIVER` while deliveries so far ≤ 3, i.e. exactly 3 redeliveries after the first delivery; then `EXPIRE` (`ACK_TIMEOUT_EXHAUSTED`) |
| `task_assignment`/`subtask_assignment` ACKED but not accepted within 120 s | `REROUTE` once (§7D.3), then `WAITING` with delegator/channel notification |
| non-terminal item past `deadline` | `EXPIRE` (`DEADLINE_EXCEEDED`) |

Exactly-once results: `ResultLedger.accept` accepts the first result per `work_item_id`
(`RESULT_ACCEPTED`); later ones return `DUPLICATE_RESULT_IGNORED` and append an audit record.

## Webhook push (REST/Webhook adapter, §7B.2)

Headers: `X-Colab-Timestamp` (unix seconds), `X-Colab-Nonce` (≥ 16 random bytes, url-safe),
`X-Colab-Key-Ref` (Secret Broker reference of the signing key), `X-Colab-Signature` =
`hex(HMAC-SHA256(key, "<timestamp>.<nonce>.<sha256hex(body)>"))`. Receiver order:
window ±300 s → `WEBHOOK_TIMESTAMP_EXPIRED`; signature → `WEBHOOK_SIGNATURE_INVALID`;
declared body digest → `WEBHOOK_BODY_HASH_MISMATCH`; nonce seen within 24 h →
`WEBHOOK_NONCE_REUSED`; missing header → `WEBHOOK_HEADER_MISSING`. The Agent answers `202` with
a `delivery-receipt.v1`. Keys are never logged.

## Usage and pricing (§7C)

- `cost_units` are integers, 1 credit = 1,000,000 cost_units (`defaults.COST_UNITS_PER_CREDIT`).
- `compute_cost_units(report, pricing)`: adapter-supplied `cost_units` → `source=reported`;
  known model → `computed`; unknown model → default rate, `source=estimated`;
  `usage_unavailable` with a valid reason → no record (ratio measured by V-P3-26);
  neither → `USAGE_REQUIRED`; schema violation → `USAGE_INVALID`.
- Formula (ceil per component): `ceil(in×in_rate/1000) + ceil(out×out_rate/1000) +
  tool_calls×call_rate + ceil(wall_ms×sec_rate/1000)`.
- `policy/pricing.yaml` (`pricing-v1`) holds placeholder rates; the System Owner may revise them
  in Admin Settings (ADR-0003 RI-001). `usage_records.pricing_version` pins the version used.

## Error codes

`WORK_ITEM_TRANSITION_INVALID`, `WORK_ITEM_TIMING_INVALID`, `DUPLICATE_RESULT_IGNORED`,
`<NAME>_SCHEMA_INVALID`, `WEBHOOK_SIGNATURE_INVALID`, `WEBHOOK_TIMESTAMP_EXPIRED`,
`WEBHOOK_NONCE_REUSED`, `WEBHOOK_BODY_HASH_MISMATCH`, `WEBHOOK_HEADER_MISSING`,
`USAGE_REQUIRED`, `USAGE_INVALID`, `PRICING_INVALID`.
