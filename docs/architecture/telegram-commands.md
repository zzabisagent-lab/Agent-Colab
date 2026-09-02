# Telegram command policy and gateway (P2-08)

Baseline: project spec §10.2 / REQ-BRDG-004 ("Telegram commands read/reply only by default"),
development plan §7A.6 (restricted grammar, ExternalIdentityLink principal), validation plan
V-P2-16 and V-P2-20.

## Decision summary

| Concern | Decision |
|---|---|
| Default | `telegram_bridges.allow_commands = false`: **nothing executes** from Telegram. The user receives a read-only notice at most once per hour per chat (durable throttle: outbox dedupe key `tg-notice:<provider_instance>:<chat>:<user>:<hour bucket>`). No identity lookup, no Event, no Task. |
| Enabled grammar | With `allow_commands = true` only `task show`, `task list`, `approve show`, `doc show` are accepted (§7A.6). |
| Opening write verbs | `telegram_bridges.content_policy.telegram_commands.allowed_verbs` (list of `<resource>.<verb>`) *adds* verbs, e.g. `["task.create"]`. The defaults are never removed; `link.*` never executes from Telegram (the link challenge stays on the primary channel). Malformed entries are ignored. |
| Principal | The Telegram user's ExternalIdentityLink on the bot's provider instance (`tg:<bot id>`), resolved with `resolve_command_principal` — only an **active** link on an **active** instance yields a principal. Unlinked users get link guidance (`TELEGRAM_USER_NOT_LINKED`); suspended/revoked links get `EXTERNAL_IDENTITY_NOT_ACTIVE`. Both are replies only: zero Task/Event/identity side effects. |
| Permissions | The Bridge policy decides whether a verb may *reach* the command bus; the Policy Engine (explicit deny > scope > allow) decides whether the Account may run it. A linked Account without `task.create` gets the bus denial code and no Event. |
| Execution | The Command Router's `<resource> <verb>` mapping runs with the explicit principal, shaped as a slash request in the Bridge's Mattermost channel. Read verbs answer from projections; opened write verbs append Events exactly as from Mattermost (cards/threads follow). |
| Idempotency | Seed `tg:<provider_instance>:<chat>:<message_id>` → bus idempotency key `<mm instance>:<sha256(seed)[:32]>` and reply dedupe key `tg-cmd:<sha256(seed)[:40]>`. A replayed Telegram update yields the same result, no second Event and no second reply. |
| Reply | Transactional channel outbox row `kind = telegram.send`, destination `telegram:<chat>[:<thread>]`, payload `{text, chat_id, message_thread_id, reply_to_message_id, source: "telegram_command", code}`; drained by the Bridge (`TelegramBridgeProvider`) together with relay deliveries. Failure after the Event append rolls back the reply with the Event. |
| Ordering | The gateway runs **before** the Bridge relay. `handled = true` means the message was a Colab command for a bound Bridge and must not be relayed as chat. `NOT_A_COMMAND` / `BRIDGE_NOT_FOUND` return `handled = false` and the relay proceeds. |
| Thread context | In a forum topic the mapped Mattermost root post (message mappings) and its thread binding provide `<task_id>` omission (§7A.2) exactly like a Mattermost thread. |
| Language | Replies use the bound channel's language (channel override, else instance default); English reference wording is the fallback for the `telegram.*` keys. |

## Modules

- `server/channels/policy.py` — `TelegramCommandPolicy` (`allow_commands`, `allowed_verbs`),
  `TelegramCommandPolicy.from_bridge(allow_commands, content_policy)`,
  `evaluate(policy, resource, verb) -> PolicyDecision(allowed, code)` with codes
  `TELEGRAM_COMMANDS_DISABLED` / `TELEGRAM_VERB_NOT_ALLOWED`, and the hourly notice key helpers.
- `server/channels/telegram/commands.py` — `TelegramCommandGateway(runtime, clock, *, router=None,
  executor=None, principal_resolver=None)`, `handle(session, msg: InboundMessage) ->
  TelegramCommandResult(handled, response_text, code, event_id, resource_id, bridge_id, throttled)`,
  `normalize_command_text`, `resolve_verb`, `select_bridge`, `execute_with_router` (the executor
  extension point).

## Wiring (Telegram inbound handler)

```python
gateway = TelegramCommandGateway(runtime, clock)


def on_telegram_message(session: Session, msg: InboundMessage) -> None:
    result = gateway.handle(session, msg)  # same transaction as the relay
    if result.handled:
        return  # command reply enqueued; do not relay as chat
    bridge.on_telegram_message(session, clock, msg)
```

The periodic `bridge.deliver(session, {"telegram": TelegramBridgeProvider(client), ...}, clock,
workspace_id)` drains the command replies with the relay rows.

## Admin surface

`allow_commands` is already part of the Bridge create/update API. Opening extra verbs requires the
Bridge schema (`schemas/api/bridge/telegram-bridge.v1.schema.json`, `content_policy`) to accept a
`telegram_commands` object `{"allowed_verbs": ["<resource>.<verb>", ...]}`; until then the policy
can only be set through the stored `content_policy` JSON (the policy reader tolerates its absence).

## Tests and evidence

- `tests/unit/test_telegram_command_policy.py` + `tests/fixtures/telegram/commands-cases.yaml`
  (policy matrix, command detection, throttle keys).
- `tests/integration/test_telegram_commands_db.py` — V-P2-16 (default → zero execution + hourly
  notice; enabled → restricted grammar; opened verb → executed once) and V-P2-20 (active /
  unlinked / suspended / under-privileged users on the same command).
- Evidence: `evidence/phase-2/SELF-V-P2-16/`, `evidence/phase-2/SELF-V-P2-20/`.
