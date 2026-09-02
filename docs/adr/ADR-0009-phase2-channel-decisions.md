# ADR-0009: Phase 2 channel decisions

- Status: Accepted (Phase 2)
- Date: 2026-09-02

## Decisions

1. **One outbox, prefix-routed.** Notifications (`notification*` kinds), Mattermost posts and
   Telegram sends share `delivery_outbox`; `drain_channels` claims rows by kind prefix with
   `FOR UPDATE SKIP LOCKED`, so a provider outage never blocks another provider's rows
   (V-P2-08, V-P2-23). Every outbox row references the Event that caused it.
2. **Cards are patched, never re-posted.** `channel_posts` binds `card:<instance>:<task>` to the
   root post id; transitions edit the root card in place and add one thread reply per Event.
   Progress replies within 10 s are coalesced; bodies over 16 k characters become Artifacts.
3. **Card and error language per channel.** `channels.language` overrides the instance default
   (`AGENT_COLAB_DEFAULT_LANGUAGE`); message bundles live in `i18n/<lang>/messages.json`. Event
   types, error codes, status enums and ids are never translated (V-P2-30).
4. **Bridge per channel with server-side relay identity.** A Telegram Bridge belongs to exactly
   one channel; a chat/topic may be bound to one channel unless an administrator records an
   exception (V-P2-17). Relayed messages carry origin markers and message mappings; echoes are
   dropped by origin, dead letters are replayable.
5. **Telegram commands are read-only by default.** `TelegramCommandGateway` runs before the
   relay; verbs execute only for an active external link and only when the Bridge's
   `content_policy.telegram_commands` allows them (`link.*` never). Refusals never touch the bus.
6. **Interactive actions are server-signed.** Button contexts carry a signed, time-bounded
   context; clicks execute the same bus handler as the slash command and are idempotent per
   post/action (V-P2-26).
7. **Soft delete only.** Channel deletion sets `status = deleted` after archival and is blocked
   by enabled Bridges or open Tasks; mappings, Artifacts and Documents keep their references.
   The baseline defines no deletion Event, so `CHANNEL_ARCHIVED` remains the last aggregate Event.
8. **Denials are audited independently of the command transaction.** The bus Authorizer writes
   `policy.deny` audit rows through `independent_audit_sink` (own transaction on the same bind);
   a denial raised out of `execute_command` therefore keeps its audit entry.
9. **Gateway drain in-process.** `ChannelGateway` runs the outbox drain as an asyncio task inside
   the API process (`AGENT_COLAB_GATEWAY_DRAIN=0` disables it for tests); a separate worker is a
   deployment option (Phase 5) not a requirement.

## Consequences

- Provider secrets come only from the environment (`TELEGRAM_BOT_TOKEN`,
  `AGENT_COLAB_MATTERMOST_BOT_TOKEN`, webhook/action secrets); nothing is stored in the database.
- Adding a language means adding one bundle; `server.i18n.missing_keys` guards completeness.
