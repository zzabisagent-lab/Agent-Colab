# Human-path acceptance automation (P7-09)

`tests/e2e/test_human_path_acceptance.py` drives the whole collaboration path the way a person
does: every Human step is a `/colab` slash command or a card button, never a REST call.

## What one pass covers

| Step | Surface | Assertion |
|---|---|---|
| Create a Task with acceptance criteria | `/colab task create … --criteria …` | a root card post appears in the channel |
| Delegate to an Agent | `/colab task delegate … --to @agent` | accepted by the Agent in the thread |
| Report progress | `/colab task progress …` (in thread) | thread reply under the card |
| Request an approval | `/colab approve request … --action tool:task_delegate` | approval card with buttons; the rules engine yields at least one recipient |
| Decide it | card **Approve** button (signed context) | the grant becomes `APPROVED` |
| Submit with evidence | `/colab task submit --evidence <artifact>` | the Agent's Artifact is the evidence |
| Independent verification | `/colab verify assign` then `/colab verify pass` | a different Account passes it |
| Complete | `/colab task complete` (in thread) | closure gate satisfied |
| Read the closing Document | `/colab doc show <task>` | the reply names the `FINALIZED` version |

Each pass also asserts the Task card is a root post, that at least five thread replies hang under
it, that the closing Document reached `FINALIZED`, and that the approval produced a notification.

## Running it

```
export AGENT_COLAB_TEST_DATABASE_URL=postgresql://colab@127.0.0.1:54329/colab_test
uv run pytest tests/e2e/test_human_path_acceptance.py -q -s
```

- `test_human_path_ten_consecutive_times` — V-P7-22, ten consecutive passes.
- `test_full_end_to_end_twenty_consecutive_times` — V-P7-02, twenty consecutive passes that also
  start a Schedule Run from the channel (`/colab schedule run-now`), covering
  Mattermost → Schedule → Agent → Approval → Artifact → Document → Verification.

## Which Mattermost

The test exercises the real Command Router, card Renderer, outbox drain and interactive-action
handler. The Mattermost server itself is the in-process `FakeMattermostClient`, installed through
`server.channels.mattermost.provider.set_client_factory` — the same seam the product uses to reach
a real instance. To run the identical path against the local Team Edition
(`scripts/dev/mattermost-local.sh`), install an `HttpMattermostClient` in that factory and point
the provider instance at the local URL; no test logic changes. Credentials for that instance live
only in `~/.local/opt/mattermost/.spike-credentials` and are never printed.

## Notifications

Nothing in the command path fires the notification rules engine on its own, so the acceptance path
runs it explicitly over the approval Event the way an operator tick does, and asserts it selects at
least one eligible approver. That is a deliberate honesty: the test drives the surface that
produces notifications rather than asserting an automatic behaviour the product does not perform
on this path.
