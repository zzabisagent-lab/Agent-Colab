# Human-path acceptance automation (P7-09)

`tests/e2e/test_human_path_acceptance.py` drives the whole collaboration path the way a person
does: every Human step is a `/colab` slash command or a card button, never a REST call, and every
one of them goes through a real Mattermost Team Edition instance.

## What one pass covers

| Step | Surface | Assertion |
|---|---|---|
| Create a Task with acceptance criteria | `/colab task create … --criteria …` | Mattermost holds the thread root, a root post in the channel |
| Delegate to an Agent | `/colab task delegate … --to @agent` | accepted by the Agent in the thread |
| Report progress | `/colab task progress …` (in thread) | thread reply under the root |
| Request an approval | `/colab approve request … --action tool:task_delegate` | approval card with buttons; the rules engine yields at least one recipient |
| Decide it | card **Approve** button, pressed in Mattermost | the grant becomes `APPROVED` |
| Submit with evidence | `/colab task submit --evidence <artifact>` | the Agent's Artifact is the evidence |
| Independent verification | `/colab verify assign` then `/colab verify pass` | a different Account passes it |
| Complete | `/colab task complete` (in thread) | closure gate satisfied |
| Read the closing Document | `/colab doc show <task>` | the reply names the `FINALIZED` version |

Each pass also asserts, against Mattermost's own copy of the posts, that the thread root is a root
post, that the Task card was posted by the gateway bot, that the approval card carries the Approve
button the product computed, and that at least five replies hang under the root. It asserts in the
database that the closing Document reached `FINALIZED` and that the approval produced a
notification.

## Which Mattermost

A real one. Nothing on this path is simulated:

- The instance is the local Team Edition. If it is not already running the test starts it with
  `scripts/dev/mattermost-local.sh start`; if it cannot be reached the test skips with the reason.
- A fresh team, channel and four member accounts are created for each run through the Mattermost
  API with the admin token, and the bot is added to the team and the channel.
- The application under test is served on loopback so Mattermost can reach its callbacks, and the
  slash command is registered *in Mattermost* by the product's own `RegisterSlashCommand`.
- Each command is executed by Mattermost (`POST /api/v4/commands/execute` as that member), so
  Mattermost calls `POST /api/v1/providers/mattermost/commands` with its own verification token,
  `trigger_id` and `root_id`. The test reads the reply Mattermost returns.
- Each button press is a real press (`POST /api/v4/posts/{post}/actions/{action}` as the approver),
  so Mattermost posts the signed integration context back to
  `POST /api/v1/providers/mattermost/actions`.
- The gateway posts with the bot token and registers the command with the admin token, both taken
  from the environment (`AGENT_COLAB_MATTERMOST_BOT_TOKEN`, `AGENT_COLAB_MATTERMOST_ADMIN_TOKEN`)
  exactly as a deployment configures them. Credentials live only in
  `~/.local/opt/mattermost/.spike-credentials` and are never printed.
- **The bot account must hold no elevated role.** During earlier probing the local bot had been
  promoted to system admin, which would let the run pass on rights a deployed bot would not have
  and hide a permission failure. It is a plain `system_user` again, and the acceptance evidence was
  re-recorded against it. Mattermost refuses to demote a bot while only one non-bot system admin
  exists, so a second admin account was created to allow the demotion. Anyone reproducing this
  should check the bot's roles before trusting a pass.

Two consequences of driving the real thing are worth knowing:

- `AGENT_COLAB_BASE_URL` must be set, because Mattermost calls the button callback URL itself. A
  relative URL resolves against the Mattermost site and the press fails with an action integration
  error, so card buttons carry the absolute URL built from that base.
- The background gateway drain is switched off (`AGENT_COLAB_GATEWAY_DRAIN=0`) and the path drains
  the channel outbox itself between steps, so a card is asserted at a defined point rather than
  raced against a timer. The drain is the product's own `drain_channels` over
  `MattermostChannelProvider` with a bot-token client.

## Running it

```
export AGENT_COLAB_TEST_DATABASE_URL=postgresql://colab@127.0.0.1:54329/colab_test
uv run pytest tests/e2e/test_human_path_acceptance.py -q -s
```

- `test_human_path_ten_consecutive_times` — V-P7-22, ten consecutive passes.
- `test_full_end_to_end_twenty_consecutive_times` — V-P7-02, twenty consecutive passes that also
  start a Schedule Run from the channel (`/colab schedule run-now`), covering
  Mattermost → Schedule → Agent → Approval → Artifact → Document → Verification.

Both together take about two minutes on the build host. `COLAB_MATTERMOST_URL` and
`COLAB_MATTERMOST_CREDENTIALS` point the run at another instance and its credentials file.

## Notifications

Nothing in the command path fires the notification rules engine on its own, so the acceptance path
runs it explicitly over the approval Event the way an operator tick does, and asserts it selects at
least one eligible approver. That is a deliberate honesty: the test drives the surface that
produces notifications rather than asserting an automatic behaviour the product does not perform
on this path.
