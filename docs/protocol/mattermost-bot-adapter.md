# Mattermost bot adapter contract (P3-12)

Authority: development plan §7B.2 (Mattermost bot row), §7A.4, §7.3; validation plan V-P3-23.
Code: `server/agents/adapters/mattermost_bot.py`, `server/channels/work_messages.py`.

## Registration

`agents.adapter_type = 'mattermost_bot'`, `agents.endpoint = {"provider_instance_id", "bot_user_id",
"bot_username", "capabilities"?, "capacity"?}`. The bot's Mattermost user must hold an **active
external identity link** to the Agent's Account (admin approval or challenge, P1-05/P2-13); replies
by any other user are never interpreted. `probe()` advertises `delivery_modes: [push]`,
`secret_handles: unsupported`, `unsupported: [secret_handles, invoke_sync]`.

## Delivery (push)

A QUEUED work item for a bot Agent becomes one **structured work message** in the Task thread
(root = the Task card post): `@<bot_username> work item <id> (<kind>)`, a ```` ```json ```` code
block with the `colab.work-item.v1` envelope, and reply instructions. The message is enqueued in the
channel outbox (`mattermost.post`, `channel_posts` subject `work_item`/role `work_message`) in the
same transaction as the `DELIVERED` transition (`delivery` receipt, `WORK_ITEM_DELIVERED`); the
dedupe key `workmsg:<instance>:<work_item>:<delivery_no>` makes each generation exactly-once on
the wire. Items that carry secret handles are never posted: they are rejected with
`CAPABILITY_UNSUPPORTED` (`WORK_ITEM_REJECTED`), and routing excludes bot adapters from such Tasks
(`server.agents.adapters.secret_support.supports_secret_handles("mattermost_bot") is False`).

## Result intake

`BotReplyIntake` is a post hook on the Mattermost WebSocket path (`work_messages.register_post_hook`;
the gateway runs hooks before the Telegram relay and drops consumed posts). For a **thread reply by
the linked bot**:

| Reply | Effect |
|---|---|
| ```` ```json ```` block with `schema_id: colab.work-result.v1` for a work item whose work message lives in this thread | one `WorkResult` command (`RESULT_ACCEPTED`); a second reply → `DUPLICATE_RESULT_IGNORED`, audited, no Event |
| `/colab …` | routed to the Command Router as the bot's Account |
| broken JSON, wrong schema, work item not in this thread, policy/validation errors | ephemeral error to the bot (`mattermost.ephemeral` outbox row) + audit `work.bot_reply_rejected`; **zero side effects** |
| plain text | ordinary chat (not consumed) |

Display identity is never taken from the reply (`display_identity` is ignored and audited by the
work result handler, §7A.4).

## Invoke / cancel / heartbeat

`invoke()` is asynchronous for bots: the invocation is delivered as an `invoke` work message and the
result arrives through the thread reply (`InvokeResult.result.status = DELIVERED_ASYNC`,
`usage_unavailable: ADAPTER_NO_METERING`). `cancel()` acknowledges immediately (cleanup deadline
60 s). Heartbeats are reported to the server by the registry API; the adapter returns the last one.
