# Interactive actions and Agent identity display (P2-12, P2-14)

Authority: spec §8.7; development plan §7A.1, §7A.3, §7A.4, §7.5; validation plan V-P2-26, V-P2-28.

## Signed button contexts

Card buttons are conveniences. Every button the Renderer emits carries a server-signed
`integration.context` (`server/channels/actions.py`):

| field | meaning |
|---|---|
| `subject_type`, `subject_id`, `action` | what the button does (`accept`, `submit`, `approve`, `reject`, `verify_pass`, `verify_fail`, `cancel`) |
| `issued_at` | unix seconds when the card was rendered (5-minute tolerance at callback) |
| `nonce` | one-time value per button per render |
| `body_sha256` | SHA-256 of the canonical JSON of subject/action |
| `signature` | `HMAC-SHA256(secret, "issued_at|nonce|body_sha256")` (the P0-10 callback contract) |

The secret is the per-instance action secret (`AGENT_COLAB_MATTERMOST_ACTION_SECRET`, a Secret
reference resolved from the environment in Phase 2). Without a secret no button contexts are
attached, so buttons cannot execute anything. `server/channels/task_cards.py` attaches the
contexts when it enqueues the card post or patch.

## Callback validation and execution

`POST /api/v1/providers/mattermost/actions` receives Mattermost's interactive-message callback
(`user_id`, `channel_id`, `post_id`, `team_id`, `trigger_id`, `context`). Mattermost does not
sign callbacks, so the server validates the context it authored itself, in the fixed order
signature → 5-minute timestamp → body hash → one-time nonce (`provider_nonces`, consumed last).
Then:

1. the principal is the Account behind the Mattermost user's **active** ExternalIdentityLink;
   unlinked or suspended users receive link guidance and cause zero side effects (audited
   `action.unlinked`);
2. the button is mapped to the same bus command as REST/MCP/`/colab`
   (accept → `AcceptTask`, cancel → `RequestCancel`, approve/reject → `DecideApproval`,
   verify_pass/fail → `SubmitVerdict` on the active run); the policy engine re-evaluates the
   permission at callback time; denials are normalized and audited (`action.denied`);
3. `submit` never executes from a button (evidence per criterion is mandatory): the reply shows
   the `/colab task submit` usage; approve/reject on a HIGH or CRITICAL approval shows the
   web-console re-authentication guidance and does not approve (`reauth_verified=False`);
4. the command runs with idempotency key `action:<instance>:<post_id>:<action>:<nonce>`, so a
   duplicate click of the same button replays the original outcome and never appends a second
   Event; a click on a stale button after the transition is a stable transition error.

Every rejection is audited with redacted metadata (instance, external user id, post id, button).

## Agent identity display

Agent utterances are posted by the Agent-Colab bot. `server/channels/identity_display.py`:

- `override` mode (Mattermost `EnablePostUsernameOverride`/`EnablePostIconOverride` confirmed
  at preflight): `props.override_username`/`override_icon_url` are set by the server;
- `prefix` mode (fallback): the message is prefixed exactly once with `[agent-name] `;
- any identity field an Agent puts in its payload (`override_username`, `override_icon_url`,
  `display_name`, `username`, `icon_url`, top level or in `props`) is removed and audited as
  `agent.identity_injection_ignored`.

`server/channels/mattermost/delivery.py` (`MattermostChannelProvider`) is the outbox drain's
provider for `mattermost:` destinations: post/patch/ephemeral/DM, idempotent per dedupe key
(an already-sent `channel_posts` row returns its post id without a client call), applying the
identity display whenever a payload names its Agent author.

## Tests

`tests/unit/test_interactive_actions.py`, `tests/unit/test_identity_display.py`,
`tests/integration/test_actions_db.py` (SELF-V-P2-26, SELF-V-P2-28).
