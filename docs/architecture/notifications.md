# Notification core (P1-13, development plan §7G)

## Rules

`policy/notification-rules.yaml` (schema `schemas/api/notification/notification-rule.v1.schema.json`)
holds the §7G defaults; `sync_rules` upserts them into `notification_rules` (the FK target of
`notifications`). Each rule: event type → recipient selectors → per-recipient channels
(`mattermost:thread`, `mattermost:dm`, `work_item`, `smtp`) and channel posts
(`mattermost:approval_channel`, `mattermost:ops_channel`, `mattermost:channel`), a dedupe window,
optional quiet hours, reminders (ratio of validity + expiry) and re-notify delay.

## Recipients

`server/notifications/selectors.py` resolves recipients from committed rows only: eligible
approvers = active role assignment whose **current committed RoleVersion** grants
`approval.decide` ∧ channel membership ∧ Human when risk ≥ HIGH, minus requester and
implementing Agent (from `approval_grants`/payload); verifier, delegator (`tasks_projection`
`delegated_by`, the assignment history's latest revision in later phases), channel members,
ops-channel members, administrators (`admin.settings` grant, Humans), agent owner
(`owner_account_id` in the payload, else the Agent's own Account until P3-01 adds an owner
column). Results are sorted for determinism.

## Planning and persistence

`plan_notifications` (pure) unions the selectors' recipients per rule, applies preferences
(`muted` → `suppressed`; `digest` → hourly digest row), quiet hours (deferral to the window end),
and computes `dedupe_key = sha256(rule|recipient|subject|bucket)` with
`bucket = floor(occurred_at / dedupe_window)` (window 0 → each Event is its own bucket).
`NotificationEngine.on_event` inserts `notifications` rows with `ON CONFLICT (dedupe_key) DO
NOTHING`, so a duplicate inside the window is impossible even under concurrent workers, and
enqueues `delivery_outbox` rows in the same transaction (per recipient × channel, channel posts,
digest upserts, reminders at 50 %/expiry, re-notify after 10 minutes for verifiers;
`cancel_pending` drops pending reminders once the verifier accepts).

## Delivery

`outbox.drain` claims due rows `FOR UPDATE SKIP LOCKED`, calls a `Provider` (stubs until
P2-17), marks `sent` or retries with backoff 1/5/25/125/625 s, `dead` after 5 attempts. A
successful per-recipient send appends exactly one `NOTIFICATION_SENT` Event (idempotent on the
outbox id). Notifications are never state authority: provider failures change only outbox rows;
Task/Approval/Event state is untouched (asserted by V-P1-31 tests).
