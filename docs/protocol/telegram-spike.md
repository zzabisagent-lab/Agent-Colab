# Telegram Bot API spike (P0-13, V-P0-19)

Executed 2026-09-02 14:01 UTC against the two forum-enabled test supergroups (`chat-A`, `chat-B`)
with the Agent-Colab test bot. Raw, redacted request/response log:
`evidence/phase-0/spikes/telegram/calls.jsonl` (86 calls); observations:
`evidence/phase-0/spikes/telegram/summary.json`. Script: `spikes/telegram/spike.py`
(`uv run python -m spikes.telegram.spike`). Token, bot id, chat ids, chat titles, human user
data and message texts never appear in the evidence.

## Environment and rights

| Item | Observed |
|---|---|
| Bot API | `https://api.telegram.org` (Bot API 9.x era: `reply_parameters`, forum topics, `is_topic_message`) |
| Chats | both `type: supergroup`, `is_forum: true` |
| Bot membership | `administrator` in both, with `can_manage_topics`, `can_delete_messages`, `can_pin_messages`, `can_manage_chat`; `can_promote_members: false` |
| Update delivery | `getUpdates` (long-polling). A webhook needs a public HTTPS endpoint and was not used; Phase 2 chooses per deployment (P2-04). |

## Results per step

| Step | Result |
|---|---|
| `createForumTopic` (chat A, chat B) | possible; returns `message_thread_id` (3 in A, 4 in B) |
| `sendMessage` into the topic | possible; message carries `message_thread_id` = topic id and `is_topic_message: true` |
| `sendMessage` with `reply_parameters` inside the topic | possible; reply keeps the same `message_thread_id`, `reply_to_message` is populated (id 4) |
| Reply to the topic's own thread id | possible; the replied message is the `forum_topic_created` service message with `message_id == message_thread_id` |
| `editMessageText` on the bot's own message | possible; `edit_date` set, thread id preserved |
| `editMessageText` with unchanged text | HTTP 400 `message is not modified` (must be treated as success/no-op by the Renderer) |
| `editMessageText` on a message not owned by the bot (topic service message) | HTTP 400 `message can't be edited` |
| Edit after the documented 48 h window | not testable in a spike; documented Bot API limit, contract keeps `EDIT_WINDOW_HOURS = 48` |
| `sendMessage` to the General topic with `message_thread_id` omitted | possible; the message has **no** `message_thread_id` and no `is_topic_message` |
| `sendMessage` to the General topic with `message_thread_id: 1` | HTTP 400 `message thread not found` |
| Cross-topic reply (`message_thread_id` = topic B, `reply_parameters` → a General-topic message) | possible; the message lands in the thread named by the **parameter** (4) and `reply_to_message` is present |
| `getUpdates` | 12 updates available; human messages and `my_chat_member` events from group setup plus the two `forum_topic_created` service messages authored by the bot (they *are* delivered as updates with `from.is_bot=true`, `message_thread_id`, `is_topic_message: true`). The bot's ordinary own messages are not delivered. |
| Burst of 40 messages into one topic (5 concurrent) | 16 accepted in 24.2 s, 24 × HTTP 429 with `retry_after` 31-33 s; accepted messages arrived in groups of ~5 every ~7 s (server-side pacing ≈ 1 msg/s), then the per-group budget (~20/min) closed; one message after waiting `retry_after` succeeded |
| `deleteMessage` (23 own messages) | possible, 0 failures |
| `closeForumTopic` + `deleteForumTopic` (both spike topics) | possible |

## Semantics fixed for the Bridge contract

- **Topic id**: `message_thread_id` of a topic equals the `message_id` of its `forum_topic_created`
  service message; that service message is delivered as an update and can be replied to.
- **General topic**: messages have no `message_thread_id`; sending with `message_thread_id: 1` is
  rejected. The contract therefore represents the General topic as `null` and omits the field
  (the Bot API's "General topic id = 1" applies to topic-management methods only; not used here).
- **Replies**: `reply_parameters.message_id` may reference any message in the chat; the target
  thread is decided by `message_thread_id`. The Bridge always sets the thread explicitly.
- **Edits**: own messages only, otherwise 400; unchanged content is 400 `message is not modified`.
- **Rate limit**: per-chat token bucket 20 per 60 s, sustained 1 msg/s, one in-flight send per
  chat, honour `retry_after` (cap 120 s) before any retry; exceeding the budget yields 429 with
  `retry_after` ≈ 30 s, which would breach the p95 ≤ 5 s delivery target (V-P2-15) if not paced.
- **Update shape**: topic messages carry `message_thread_id` and `is_topic_message: true`; replies
  carry `reply_to_message`; General-topic messages carry neither field.

## Contradictions

None with spec §10 / development plan §6.5. One assumption of the P0-13 task text was corrected by
observation: the General topic cannot be addressed as thread id 1 in `sendMessage`; the contract
uses `null`/omitted (machine-checked by `tests/unit/test_telegram_contract.py`).
