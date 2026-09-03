# RB-BRIDGE-LOOP — a Telegram Bridge is echoing messages

- **Id:** `RB-BRIDGE-LOOP`
- **Trigger:** alert `BRIDGE_LOOP_SUSPECTED`, a climbing `bridge_dead_letters` count, or duplicate
  relays observed in a channel.
- **Severity:** critical. A loop multiplies messages on both sides and burns provider rate limits.

## Detection

1. Count recent mappings per origin:
   `SELECT origin_platform, count(*) FROM message_mappings WHERE bridge_id = :b AND created_at > now() - interval '10 minutes' GROUP BY 1;`
   A loop shows both directions growing together with rising `hop_count`.
2. `SELECT count(*) FROM bridge_dead_letters WHERE bridge_id = :b;` and
   `GET /api/v1/ops/overview` for the outbox backlog.
3. Confirm the origin markers: a relayed message carries its origin, so an echo has the local
   instance as `origin_platform` on the far side.

## Isolation

1. Disable the Bridge immediately:
   `POST /api/v1/channels/{channel_id}/bridges/{bridge_id}/disable`.
   Only that Bridge stops; every other channel keeps relaying.
2. If several Bridges share the chat, disable each one; a chat may be bound to one channel unless
   an administrator recorded an exception.

## Recovery

1. Fix the cause: a duplicate binding of the same chat, a bot relaying its own posts, or a
   direction set to `bidirectional` where the far side also bridges back.
2. Drain or discard the dead letters: replay with
   `server.channels.telegram.bridge.Bridge.replay_dead_letters` once the cause is gone.
3. Re-enable: `POST /api/v1/channels/{channel_id}/bridges/{bridge_id}/enable`, then send one
   canary message and confirm exactly one relay in each direction.

## Post-verification

1. `SELECT count(*) FROM message_mappings WHERE bridge_id = :b AND source_message_id = :m;`
   returns 1 for the canary message.
2. Dead letters stop growing and the outbox backlog returns to its baseline.
3. The disable and enable actions appear in the audit trail with the operator's account.

## Evidence to capture

The per-origin counts before and after, the dead-letter count, the disable and enable audit rows,
and the single-relay proof for the canary message.
