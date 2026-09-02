# Mattermost spike (P0-10 / V-P0-16)

- Date: 2026-09-02
- Target: Mattermost Team Edition **11.10.1** (build hash `f9deca98…`), run locally from the
  official tarball with `scripts/dev/mattermost-local.sh` against the user-space PostgreSQL 16
  (`postgres://colab@127.0.0.1:54329/mattermost`). Site URL `http://127.0.0.1:8065`.
- Test tenant: team `colab-test`, channels `work-a`/`work-b`, system-admin user `colabadmin`,
  bot account `agent-colab`. All credentials live only in
  `~/.local/opt/mattermost/.spike-credentials` (outside the repository, mode 0600); every JSON
  saved under `evidence/phase-0/spikes/mattermost/` is redacted (`token`, `password`, `email`
  keys and every token value replaced by `<redacted>`). Mattermost IDs (26-character) are not
  secrets.

## API calls (REST v4, tokens redacted)

| Step | Call | Result | Evidence |
|---|---|---|---|
| first user (open server → system admin) | `POST /api/v4/users` | roles `system_admin system_user` | — (contains e-mail) |
| login | `POST /api/v4/users/login` | `Token` header (session) | — |
| team | `POST /api/v4/teams` `{name: colab-test, type: O}` | created | — |
| channels | `POST /api/v4/channels` ×2 | `work-a`, `work-b` | `channel-work-a.json`, `channel-work-b.json` |
| bot | `POST /api/v4/bots` `{username: agent-colab}` | bot user created | `bot-create.json` |
| bot token | `POST /api/v4/users/{bot}/tokens` | personal access token (bot auth) | — |
| slash command | `POST /api/v4/commands` `{trigger: colab, method: P, url: http://127.0.0.1:8080/api/v1/providers/mattermost/commands, auto_complete: true}` | **possible**, id returned, listed by `GET /api/v4/commands?team_id=…&custom_only=true` | `slash-command-create.json`, `slash-command-list.json` |
| slash delivery | `POST /api/v4/commands/execute` with `/colab task create "Spike task" --criteria "x"` while a listener on 127.0.0.1:8080 captured the request | Mattermost POSTs `application/x-www-form-urlencoded` with `token, team_id, team_domain, channel_id, channel_name, user_id, user_name, command, text, trigger_id, response_url`; the ephemeral JSON response was accepted (HTTP 200) | `slash-command-delivery.json` |
| override enabled | `PUT /api/v4/config` (`EnablePostUsernameOverride/EnablePostIconOverride = true`), then bot `POST /api/v4/posts` with `props.override_username="Research Agent"`, `override_icon_url`, `from_webhook="true"` | post stored with the override props; `user_id` is the bot | `override-and-thread-results.json` |
| override disabled | same with the flags `false` | **the server still stores the props** — Mattermost enforces the flag in the web/mobile clients at render time, not in the API | same |
| prefix fallback | bot post `"[Research Agent] …"` | stored as plain message with `from_bot` | same |
| in-place edit + thread reply | `PUT /api/v4/posts/{id}/patch`, `POST /api/v4/posts` with `root_id` | `edit_at > 0`; reply bound to the root | same (`card_edit_in_place`) |
| WebSocket | `ws://…/api/v4/websocket`, `authentication_challenge`, then post/edit/react | events observed: `hello`, `posted`, `post_edited`, `reaction_added`, `status_change` | `websocket-events.json` |
| client config | `GET /api/v4/config/client?format=old` | `EnablePostUsernameOverride`/`EnablePostIconOverride`/`EnableCommands` are **not** exposed to non-admin clients (`null`) | `client-config-flags.json` |
| admin config | `GET /api/v4/config` (system admin) | the flags are readable | `config-flags-initial.json` |

## Results

| Question | Answer |
|---|---|
| Slash command `/colab` registration through the API | **possible** (per team; `EnableCommands=true`). Autocomplete hints supported. |
| Slash command payload | form-encoded; carries `token` (per-command secret, compared in constant time), `trigger_id` (for interactive dialogs), `response_url` (delayed responses), `channel_id`, `user_id`, `command`, `text`. No `Authorization` header. |
| `@colab` mention form | any post is delivered over the WebSocket `posted` event; the Command Router applies the same grammar when the message starts with `@colab`. |
| `override_username` / `override_icon_url` | **possible** when `ServiceSettings.EnablePostUsernameOverride` and `EnablePostIconOverride` are `true`. When they are `false` the API still accepts and stores the props but clients ignore them, so the server cannot rely on the API response to know how the post will be displayed. |
| Fallback | `[agent-name]` message prefix, applied by the server whenever the override flags are not confirmed `true`. |
| How the server confirms the flags | Setup preflight (P4-03) and the Phase 2 provider client read `GET /api/v4/config` with the configured credential; if the read is forbidden (non-admin bot token) or either flag is `false`, the provider instance is pinned to `identity_display = prefix`. The decision is stored per provider instance and re-checked on every reconnect. |
| Interactive actions | Mattermost posts `props.attachments[].actions[]` with an `integration.url` and `integration.context`; the callback body includes `user_id`, `channel_id`, `team_id`, `post_id`, `trigger_id`, `context`. Mattermost does not sign callbacks, so the server signs its own `context` (see `schemas/api/mattermost/action-callback.v1.schema.json`). |
| WebSocket events needed by §7A.1 | `posted`, `post_edited`, `reaction_added` all delivered to an authenticated WebSocket session of the bot/admin token. |
| In-place card edit + immutable thread replies | supported (`PUT /posts/{id}/patch`, replies with `root_id`). |

## Decision for P2-14 (Agent identity display)

1. Default: `override_username`/`override_icon_url` set **only by the server** from the Agent's
   registered display name; any display identity inside an Agent result payload is ignored and
   audited (§7A.4).
2. Fallback: when the provider instance's override flags are not confirmed `true` (config read
   forbidden or flags `false`), every Agent utterance is prefixed `[<agent display name>] ` and
   `override_*` props are omitted.
3. The mode is recorded per provider instance (`identity_display = override | prefix`) at
   preflight and surfaced in the admin console; V-P2-28 tests both modes by toggling the flag.

## Cleanup

Mattermost was stopped after the spike (`scripts/dev/mattermost-local.sh stop`); the database
and tenant remain for Phase 2 integration tests.
