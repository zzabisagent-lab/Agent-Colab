# Work delivery core (P1-12)

Authority: development plan §7B (7B.1–7B.4), §3.1 Work Delivery, §21.1, §7D.3; validation
V-P1-29, V-P3-06/21/25, CS-02/03/09/12.

## Durable inbox

Every piece of work given to an Agent is a `work_items` row (`server/work/inbox.py`) plus
`WORK_ITEM_*` Events on the `work_item` aggregate (`wi-…`). Chat messages are never the only
delivery path. The §7B.1 envelope handed to the Agent (`WorkItem.to_delivery()`) carries
`payload_ref = colab://work/<id>/payload` (body fetched separately, 1 MB limit enforced at
enqueue), `secret_handles`, `expected_result_schema`, `idempotency_key`, and `delivery_no`.

| Operation | Precondition | Effect | Event |
|---|---|---|---|
| `enqueue` | kind valid, key unused (idempotent replay otherwise) | QUEUED, `delivery_count 0` | `WORK_ITEM_QUEUED` |
| `poll` (pull) | rows locked `FOR UPDATE SKIP LOCKED` | QUEUED → DELIVERED, `delivery_count += 1`, `delivery` receipt; un-acked DELIVERED rows returned again unchanged | `WORK_ITEM_DELIVERED` (`delivery_no`) |
| `ack` | owner, DELIVERED (idempotent when already acked) | ACKED, `acked_at` | `WORK_ITEM_ACKED` |
| `start` | owner, ACKED | IN_PROGRESS, `accepted_at`, `accept` receipt | `WORK_ITEM_STARTED` |
| `reject` | owner, reason ∈ {CAPABILITY_UNSUPPORTED, CAPACITY, POLICY, OTHER} | REJECTED | `WORK_ITEM_REJECTED` |
| `cancel` | non-terminal | CANCELLED | `WORK_ITEM_CANCELLED` |
| `result` | owner, DELIVERED/ACKED/IN_PROGRESS, schema-valid | RESULT_RECEIVED, single `result` receipt | `WORK_ITEM_RESULT_RECEIVED` |

Transitions come from `server/work/state.py` (P0-11); owner mismatch is `WORK_ITEM_NOT_OWNER`
(normalized to 404 by the bus layer), invalid moves `WORK_ITEM_TRANSITION_INVALID`.

## Reconnect and redelivery

- **Reconnect** (§7B.3): a poll from any session returns every DELIVERED-but-unacked item of the
  Agent with its current `delivery_no`; no new delivery is counted, so a client that polls often
  does not burn its redeliveries.
- **Ack timeout** (§7B.1, §21.1): `server/work/timeouts.sweep` applies the pure timing model
  `next_action` with the injected Clock. A DELIVERED item without ack for 60 s is re-queued
  (`status = QUEUED`, `delivery_count` kept — a QUEUED row with `delivery_count > 0` is awaiting
  redelivery). The next poll delivers it again with `delivery_no = count + 1`. After the third
  redelivery (`delivery_count = 4`) a further 60 s without ack makes the item `EXPIRED`
  (`reason_code = ACK_TIMEOUT`). Exactly 1 delivery + 3 redeliveries, then EXPIRED (V-P1-29).
- **Deadline**: any open item past `deadline` expires with `reason_code = DEADLINE`.
- **Accept timeout** (§7D.3): a `task_assignment`/`subtask_assignment` acked but not accepted
  within 120 s yields a `REROUTE_REQUIRED` outcome (once), then `WAITING_REQUIRED`; the sweep does
  not modify the item — the router (P3-14) performs the re-routing.

## Exactly-once results

`work_item_receipts` is append-only; the partial unique index
`work_item_receipts_one_result_idx` allows exactly one `result` receipt per work item. A second
submission — same or different body — leaves a `duplicate_result` receipt, an audit row
`work.duplicate_result_ignored`, returns `DUPLICATE_RESULT_IGNORED` with the first receipt id and
result ref, and changes nothing (no state change, no Event). Results are validated against
`schemas/adapters/work-result.v1.schema.json` (usage or `usage_unavailable` mandatory, §7C);
`display_identity` in a result is ignored and audited (§7A.4). A result on a DELIVERED item
implies the ack (an explicit `WORK_ITEM_ACKED` is appended first).

`result_ref = colab://work/<id>/result/<sha256 of the canonical result>`.

## Delivery channels

`server/work/push.py` defines `DeliveryChannel` (push | pull). Phase 1 ships `PullInbox`; the
MCP long-poll transport (P3-10), HMAC webhook push (P3-11), and Mattermost bot delivery (P3-12)
implement the protocol over the same inbox.

## Bus commands (`server/application/work.py`)

`WorkPoll(agent_id, max_items)`, `WorkAck`, `WorkStart`, `WorkReject(reason_code)`,
`WorkResult(result)`, and internal `QueueWorkItem`. The Agent identity is derived from the
credential's Account (`agents.account_id`) — an Agent can only poll and act on its own inbox
(`WORK_ITEM_NOT_OWNER`, 404); a non-Agent account gets `AGENT_NOT_FOUND`. Permission `work.poll`.

## Interpretations recorded

- §7B.1 "redelivered (at most 3 times)" is implemented as exactly three timeout redeliveries
  after the first delivery, matching V-P1-29.
- The P0-11 state table has no `DELIVERED → result` edge; a result on a DELIVERED item is treated
  as implied receipt (ack, then result) rather than rejected, because §7B.4 makes acceptance,
  not the ack, the explicit signal.
- Re-queueing for redelivery reuses `status = QUEUED` (no extra column); `delivery_count`
  distinguishes first deliveries from redeliveries.
