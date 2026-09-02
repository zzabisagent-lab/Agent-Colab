# MCP Streamable HTTP spike (P0-11, V-P0-17)

Purpose: prove, with logs, that the §7B.2/§7B.3 MCP pull model works with the pinned SDK:
`work_poll(max_wait ≤ 30 s)` answers within 30 s, un-acked items are redelivered after a
reconnect, `work_result` is idempotent, and whether `colab://inbox/{agent_id}` subscribe
notifications are available. Spike code: `spikes/mcp/server.py`, `spikes/mcp/client.py`
(not product code). Evidence: `evidence/phase-0/spikes/mcp/{client,server}.jsonl` (+ stderr logs).

## Environment

| Item | Value |
|---|---|
| SDK | `mcp` 2.1.1 (`MCPServer`, `streamable_http_client`, `ClientSession`) |
| Transport | Streamable HTTP, path `/mcp`, stateful session manager, uvicorn 0.52.4, httpx 0.28.1 |
| Bind | `127.0.0.1:8767` (`SPIKE_PORT`; 8765 was occupied by another local process at run time) |
| Negotiated protocol | `2025-11-25` (client handshake ladder `2024-11-05 … 2025-11-25`) |
| Run | 2026-09-02, client exit 0 |

## Measurements (from `client.jsonl`)

| Scenario | Result |
|---|---|
| (a) `work_poll(max_wait_s=30)` on an empty inbox | returned `items: []` after **29.587 s** (re-run after Finding F-P0-002-01: the server stops waiting `SAFETY_MARGIN_S = 0.5 s` before the caller's `max_wait_s`, so the end-to-end response including transport overhead is ≤ 30.000 s; the first run measured 30.084 s and violated the bound) |
| (b) enqueue → poll → disconnect **without ack** → new session → poll | first poll delivered `wi-00000000000000ff` with `delivery_count=1` in 0.007 s; after reconnect the same item was redelivered with `delivery_count=2` in 0.016 s |
| (c) `work_ack` then `work_result` twice, then poll | first result `RESULT_ACCEPTED`, second `DUPLICATE_RESULT_IGNORED`; poll after ack returned `[]` after the 2 s wait (no redelivery) |
| (d) `subscriptions/listen` on `colab://inbox/{agent_id}` | **not available**: `ListenNotSupportedError` — listen requires protocol `2026-07-28`; the SDK's session client negotiates at most `2025-11-25` |
| (d2) legacy `resources/subscribe` | **not available**: server answers `Method not found` (removed as of 2026-07-28 in the SDK; client emits `MCPDeprecationWarning`) |

Server-side log (`server.jsonl`) shows the matching `enqueued`, `poll_delivered` (waited 0.0 s),
`acked`, `result_accepted`, `duplicate_result_ignored`, and `poll_empty` (waited 30.0 s / 2.0 s)
entries.

## Decision for P3-10 (MCP server transport)

- Delivery mode for MCP Agents is **long-poll only**: `work_poll(max_wait ≤ 30 s)` on the
  stateful Streamable HTTP session; the server keeps un-acked items in the durable inbox and
  redelivers them to any later poll, including after a reconnect (development plan §7B.3
  "otherwise long-polling only" applies).
- `colab://inbox/{agent_id}` stays exposed as a readable resource (snapshot of un-acked items);
  subscribe notifications are re-evaluated when the pinned SDK offers `subscriptions/listen` on
  the session transport (ADR follow-up; additive change, no contract impact).
- `work_result` is idempotent per `work_item_id` (first result wins, duplicates audited) and
  `work_ack` is the only action that stops redelivery — both proven above.
- One concurrent `work_poll` per session and Bearer/mTLS authentication are P3-10 scope.
