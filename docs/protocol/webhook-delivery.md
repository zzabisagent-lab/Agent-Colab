# REST/Webhook push delivery contract (P3-11)

Authority: development plan §7.3, §7B.1–§7B.4, §7.5; validation plan V-P3-22, V-P3-06, CS-02/09/11.
Machine-readable: `schemas/adapters/webhook-envelope.v1.schema.json`, `work-item.v1`,
`delivery-receipt.v1`, `work-result.v1`, `probe-response.v1`. Code: `server/agents/adapters/webhook.py`,
`server/agents/webhook_delivery.py`, `server/api/v1/work.py`, `server/agents/webhook_signing.py`.

## 1. Outbound: server → Agent endpoint

Every adapter call is one `POST` to the registered endpoint URL (`agents.endpoint.url`).

| Header | Value |
|---|---|
| `X-Colab-Op` | `probe` \| `deliver` \| `invoke` \| `cancel` |
| `X-Colab-Timestamp` | unix seconds at signing time |
| `X-Colab-Nonce` | ≥ 16 random bytes, base64url; never reused |
| `X-Colab-Signature` | hex HMAC-SHA256 over `"{timestamp}.{nonce}.{sha256hex(body)}"` |
| `X-Colab-Key-Ref` | Secret Broker reference of the signing key (`sec-…[@vN]`); the key value is never sent or stored |
| `X-Colab-Correlation-Id` | work item / invocation correlation id (echoed by the Agent, CS-08) |
| `X-Colab-Delivery-No` | delivery generation (`deliver` only; 1 = first delivery, 2–4 = redeliveries) |
| `Content-Type` | `application/json` |

Bodies: `deliver` sends the `colab.work-item.v1` envelope (payload fetched separately via
`payload_ref`; secret handles are opaque ids). `probe` sends `{"op":"probe","agent_id"}` and expects
`200` + `colab.probe-response.v1`. `invoke` sends `{op, tool, input, deadline, secret_handles,
correlation_id}` and expects `200` + `{result, usage | usage_unavailable, events?, artifacts?,
correlation_id}`; an unadvertised tool returns `{"error_code":"CAPABILITY_UNSUPPORTED"}`. `cancel`
sends `{op, target_id}` and expects `200` (acknowledgement within 10 s, cleanup within 60 s).

`deliver` expects **`202`** with a `colab.delivery-receipt.v1` body (`accepted_at` or a
`rejection_code` in `CAPABILITY_UNSUPPORTED|CAPACITY|POLICY|OTHER`). A rejection becomes
`WORK_ITEM_REJECTED`; §7D.3 re-routing applies.

Signing keys are resolved per call from `agents.credential_ref` through `SigningKeyResolver`
(`server/agents/signing_keys.py`; development resolver reads `AGENT_COLAB_WEBHOOK_KEY_<REF>` — this
is the Phase 4 Secret Broker seam). Key bytes never appear in the database, outbox, logs or errors.

### Retry schedule and exactly-once

Deliveries go through the transactional outbox (`delivery_outbox`, kind `webhook.deliver`, one row
per work item *generation*). Each attempt is signed afresh (the 5-minute window forbids replaying a
signed body). Failures: backoff 1 s, 5 s, 25 s, 125 s, 625 s; dead after 5 attempts (outbox row only,
never Events). On `202` the item becomes `DELIVERED` with a `delivery` receipt carrying the receipt
digest and a `WORK_ITEM_DELIVERED` Event; a later retry of the same generation finds that receipt
and sends nothing (one side effect per receipt). No `ACKED` within 60 s → the timeout sweep re-queues
the item and generation `n+1` is pushed (≤ 3 redeliveries, then `EXPIRED`).

### Stable adapter error codes (CS-11)

| Situation | Code | Retryable |
|---|---|---|
| timeout | `ADAPTER_TIMEOUT` | yes |
| connection failure, 5xx | `ADAPTER_UNREACHABLE` | yes |
| 401/403 | `ADAPTER_AUTH_FAILED` | no |
| 429 | `ADAPTER_RATE_LIMITED` | yes |
| other 4xx, non-JSON or schema-invalid response, receipt for another item | `ADAPTER_BAD_RESPONSE` | no |
| unadvertised tool | `CAPABILITY_UNSUPPORTED` | no |
| anything else | `ADAPTER_INTERNAL` | no |

## 2. Inbound: Agent → server

Service-token routes (Bearer token of the Agent's Account; the Agent identity is derived from the
credential, never from the body; non-owner access is a normalized `404`):

| Route | Body | Result |
|---|---|---|
| `GET /api/v1/work/{id}` | — | envelope + status + payload + receipts |
| `POST /api/v1/work/{id}/ack` | — | DELIVERED → ACKED |
| `POST /api/v1/work/{id}/reject` | `{"reason_code": …}` | → REJECTED (`422` for an unknown code) |
| `POST /api/v1/work/{id}/result` | `colab.work-result.v1` | exactly once; a second result returns `DUPLICATE_RESULT_IGNORED` (`replayed: true`), leaves a `duplicate_result` receipt and an audit row |

Signed callbacks without a token — `POST /api/v1/agents/{agent_id}/webhook/callbacks` with the same
four signing headers (the Agent signs with the key behind its `credential_ref`), optional
`X-Colab-Body-Sha256`, body `{"op": "result"|"ack"|"reject", "work_item_id", "result"?,
"reason_code"?}`. Verification order (§7.5): timestamp within ±300 s → signature → body hash →
one-time nonce (`webhook_nonces`, kept 24 h, pruned on insert). Failures are `401` with
`WEBHOOK_TIMESTAMP_EXPIRED | WEBHOOK_SIGNATURE_INVALID | WEBHOOK_BODY_HASH_MISMATCH |
WEBHOOK_NONCE_REUSED | WEBHOOK_HEADER_MISSING` and zero side effects; the nonce doubles as the
command idempotency key. Suspended/revoked Agents get `403 AGENT_INACTIVE`.
