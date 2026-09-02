# Notification providers (P2-17, development plan §7G, §7A.2, §10.2)

The P1-13 rules engine writes `notifications` + `delivery_outbox` rows in the Event transaction;
`outbox.drain` hands due rows to a `Provider`. P2-17 supplies the real providers and the
preference/digest plumbing.

## Destinations and providers (`server/notifications/providers.py`)

| Destination | Provider behaviour |
|---|---|
| `mattermost:dm:<account uuid>` | DM to the Account's Mattermost user, resolved through its **active** `external_identity_links` row on an active Mattermost provider instance; no link → `NOTIFICATION_RECIPIENT_UNREACHABLE` recorded on the outbox row (retry/backoff → dead), the drain never crashes |
| `mattermost:thread:<account uuid>` | reply under the subject Task's root post (`thread_bindings`) with an `@username` mention; no binding → `NOTIFICATION_THREAD_UNBOUND` |
| `mattermost:approval_channel|ops_channel|channel:<channel uuid>` | post in the channel's external Mattermost channel; then `TelegramRelayGate` may relay it |
| `smtp:<address>` | `SmtpNotificationProvider` (standard-library `smtplib`, injectable transport); `NOTIFICATION_CHANNEL_DISABLED` unless `AGENT_COLAB_SMTP_HOST` is configured (the engine excludes `smtp` from enabled channels by default) |
| `work_item:<account uuid>` | `NoopProvider` — work items are delivered by the inbox (Phase 3) |

`CompositeProvider` routes by prefix; unknown prefixes are `NOTIFICATION_DESTINATION_INVALID`.
Message text comes from `render_text` (event templates, reminder/re-notify prefixes, digest lists)
and always ends with the notification id in backticks so duplicates are detectable.

### Exactly-once argument

The drain marks the outbox row `sent` in the same transaction in which it called the provider.
A crash between the provider call and the commit retries the row; a DM may therefore be sent
twice in that narrow window. Mitigations: the provider keeps an in-memory guard of recently
delivered keys (`guard_key`: notification id, event id, rule, channel, reminder/re-notify tag,
destination) and refuses the second call within a process; the notification id in every message
makes any residual duplicate visible to the recipient and to audits. Muted recipients are checked
again at send time (`notification_preferences.muted`), so a mute after enqueue still yields zero
sends.

## Telegram relay (`TelegramRelayGate`)

Channel notifications (approval notices → `approval_notice`, everything else → `system_event`)
are relayed to Telegram only through an **enabled** Bridge of that channel whose direction is
`mattermost_to_telegram` or `bidirectional` and whose `content_policy` allows the kind
(`relay_allowed`, pure). Relays are `telegram.send` deliveries in the channel outbox
(`server/channels/outbox.py`) with dedupe key `relay:<bridge>:<event>|<kind>`; the Bridge's
provider (P2-05/P2-06) delivers them, so §7G "whether to relay externally follows the Bridge
policy" holds by construction.

## Digest and preferences (`server/notifications/routing.py`, `server/application/notification_prefs.py`)

- `set_preferences` upserts `notification_preferences` (audited, values are booleans only); bus
  commands `SetNotificationPreferences`, `MuteNotifications`, `UnmuteNotifications`, `SetDigest`
  require `notification.self` and act on the caller's own Account (used by `/colab notify` and
  `PUT /api/v1/notifications/preferences`).
- `DigestScheduler.flush_due` delivers the `notification_digest` rows whose hour has arrived
  (`deliver_at` = next full hour at planning time) through the same drain, one DM per recipient
  listing every item; `pending_digests` shows what is queued. Everything is driven by the injected
  Clock.
- `POST /api/v1/notifications/drain` (`ops.manage`) runs the drain once with the provider stored
  on `app.state.notification_provider`.

## Wiring (parent, `create_app`)

```python
from server.notifications.providers import (
    CompositeProvider,
    MattermostNotificationProvider,
    NoopProvider,
    SmtpNotificationProvider,
    TelegramRelayGate,
)

mm = MattermostNotificationProvider(
    app.state.session_factory, relay_gate=TelegramRelayGate(), clock=runtime.clock
)
smtp = SmtpNotificationProvider(
    os.environ.get("AGENT_COLAB_SMTP_HOST"),
    int(os.environ.get("AGENT_COLAB_SMTP_PORT", "587")),
    os.environ.get("AGENT_COLAB_SMTP_SENDER", "agent-colab@localhost"),
)
app.state.notification_provider = CompositeProvider(
    {"mattermost": mm, "smtp": smtp, "work_item": NoopProvider()}
)
app.include_router(notifications_router)  # before the MCP root mount
```

## Verification

`tests/unit/test_notification_providers.py` (routing, SMTP gating, rendering, guard keys, relay
decisions) and `tests/integration/test_notification_delivery.py` (V-P2-31: DMs to approvers /
verifier / delegator, thread mentions under the Task root, approval-channel post, muted → zero,
digest → nothing until the hour then exactly one batched DM, mute toggle after enqueue, relay
enqueued only when the Bridge policy allows).
