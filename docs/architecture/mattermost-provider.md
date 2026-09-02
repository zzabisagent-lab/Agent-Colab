# Mattermost provider and Command Router (P2-01, P2-10)

Authority: spec §8.7, development plan §7A, §7.5, §7H; contracts from P0-10
(`docs/protocol/mattermost-contract.md`, spike `docs/protocol/mattermost-spike.md`).

## Provider instance

`provider_instances` row per **base URL + team** (`provider_instance_id = mm:<host>:<team>`),
`team_or_bot_ref` = Mattermost team id, `bot_user_id`, `identity_display` (`override` only when
both `EnablePostUsernameOverride` and `EnablePostIconOverride` are confirmed `true` through an
admin-capable config read; otherwise `prefix`, P0-10 spike rule), `config.team_name`.
Registered with `RegisterProviderInstance` (`channel.manage`). Credentials are never stored:
`AGENT_COLAB_MATTERMOST_BOT_TOKEN` (runtime posting/reading) and
`AGENT_COLAB_MATTERMOST_ADMIN_TOKEN` (slash-command registration, config probe; Phase 4 turns
both into Secret references).

## Slash command

`RegisterSlashCommand` creates `/colab` for the team (`POST /api/v4/commands`, method `P`,
callback `POST /api/v1/providers/mattermost/commands`); if the trigger already exists the
command is re-pointed to this instance's callback URL and its token regenerated. Only the
SHA-256 of the per-command verification token is stored (`provider_command_tokens`).

Inbound validation order (§7.5), each failure a 401/403 with zero domain side effects:
provider instance by `team_id` → constant-time token hash compare
(`CALLBACK_SIGNATURE_INVALID`) → one-time `trigger_id` nonce (`provider_nonces`, 5-minute TTL,
`CALLBACK_NONCE_REUSED`). Mattermost sends no timestamp with slash payloads; the trigger id is
single-use by construction and the nonce table enforces it.

## Command Router

`server/channels/router.py` — `Router(runtime).route(SlashRequest) -> CommandResponse`:

1. `parse_command` (P0-10 grammar). Text without the `/colab`/`@colab` prefix is
   `COMMAND_PREFIX_MISSING` and is never executed (product principle 4).
2. Principal = the Mattermost user's **active** ExternalIdentityLink
   (`resolve_command_principal`); unlinked/suspended users may run only `link` and `help`
   (`COMMAND_UNLINKED_RESTRICTED` for anything else; the parser enforces it, the router repeats
   the check).
3. Thread context: the request's `root_id` (or `post_id`) is looked up in `thread_bindings`;
   a bound Task fills an omitted `<task_id>`.
4. Dispatch to the bus command (same handler as REST/MCP) with idempotency key
   `<provider_instance>:sha256(post_id | trigger_id)[:32]` and correlation
   `mm:<instance>:<trigger>`; policy, state machine, and Events are the bus's.
5. Errors → ephemeral (cause + correct example); success → public thread reply under the
   Task's root post; a `task create` reply becomes the Task's root post and is bound in
   `thread_bindings` (P2-11 rewrites it into the card). Replays (same post) return the original
   result without a second post.

Mapping notes: `task progress|submit` from `ACCEPTED` first appends `TASK_STARTED`; a bare
`--evidence <ref>` is attached to every criterion of the current revision (P1-11 needs
per-criterion refs; `crit-…:<ref>` targets one); `task reject` rejects the delivered
assignment work item (`WORK_ITEM_NOT_FOUND` when none — Phase 3 delivers them); `approve
grant|reject` runs the §7E eligibility with `reauth_verified=False`, so HIGH+ decisions answer
`REAUTH_REQUIRED` with web guidance; `verify assign` creates the run for the Task assignee and
the named verifier (P3-13 replaces this with automatic assignment); `verify pass|fail|block`
submits a minimal verdict report; `schedule *`, `brainstorm *`, `doc review|publish` answer
`COMMAND_NOT_AVAILABLE` until their phases; `link start|confirm` delegate to
`LINK_HANDLERS` (P2-13); `notify *` writes `notification_preferences`.

## Channels and templates

`ImportChannel` binds a Mattermost channel (`external_channel_id`) to a `channels` row
(`chan-<sha256(instance|external)[:16]>`) with the policy copied from its template
(`policy/channel-templates.yaml`: work, brainstorm, approval, ops — protected; custom channels
start without one). `ConfigureChannel` merges policy/documentation template/language/retention/
legal hold per channel (schema-validated) and appends `CHANNEL_CONFIGURED`; `ArchiveChannel`
appends `CHANNEL_ARCHIVED`. Templates are CRUD rows in `channel_templates`; deleting a default
is `TEMPLATE_PROTECTED`; per-channel settings never write back to templates.

## Tables added by migration 0003

`channel_templates`, `thread_bindings`, `provider_command_tokens`, `provider_nonces`;
columns `provider_instances.bot_user_id/identity_display/config`,
`channels.template_id/retention_days/legal_hold/archived_at/deleted_at`.

## Evidence

- V-P2-01: `tests/integration/test_mattermost_provider.py` — real Mattermost TE 11.10.1
  (`scripts/dev/mattermost-local.sh`), slash command executed through `/api/v4/commands/execute`,
  `TASK_CREATED` Event and bot thread post verified, free text ignored, forged form refused.
- V-P2-19: `tests/integration/test_channels_db.py`.
- V-P2-24: `tests/unit/test_command_router.py` (fixture `tests/fixtures/mattermost/router-cases.yaml`)
  and `tests/unit/test_mattermost_client.py`.
