# VR-P2-002 evidence index

All commands ran at target commit `88adc56791b08285330a50bb9c4b8dad897d6b3f`.
Every log records the command output and exit code without credential values.

- `environment.txt`: target, tool versions, baseline/schema hashes.
- `credential-presence.txt`: presence-only check; no values.
- `bridge-provider-outbox.log`: Bridge rules and DB paths, 100+100 bidirectional
  messages, both-provider outage recovery, callback forgery/replay, attachments,
  transactional outbox, renderer and Bridge latency, and Telegram command policy.
- `mattermost-live.log`: real local Mattermost Team Edition slash-command path.
- `admin-ui.log`: frozen web install, production build, Playwright UI authorization,
  and API authorization/audit. Generated `node_modules`, `dist`, and `test-results`
  were removed after the run.
- `channels-templates-i18n.log`: four protected defaults, custom template/channel
  CRUD/application and language/template checks.
- `commands-cards-identity-retention-notifications.log`: grammar, cards/actions,
  external identities, channel lifecycle, link challenge, Agent display identity,
  retention/legal hold, i18n, and notification delivery.
- `telegram-readonly-preflight.log`: exported-credential getMe/getChat checks only;
  confirms an authenticated bot and two distinct forum-enabled chats without
  recording identifiers.
- `static-checks.log`: lint, documentation checks, and redacted gitleaks scan.
- `full-test.log`: complete suite result (1305 passed, two live Telegram tests
  skipped because those test functions read `.env` directly).

Package spot checks required by validation plan §7.4 are covered as follows:
P2-01 by V-P2-01; P2-02 by V-P2-19/20/21/22; P2-03 by V-P2-02/23; P2-04 by
V-P2-09/11; P2-05 by V-P2-03/05/06/13/14/17; P2-06 by V-P2-04/07/08/10/15;
P2-07 by V-P2-12; P2-08 by V-P2-16/20; P2-09 by V-P2-18; P2-10 by V-P2-24;
P2-11 by V-P2-25; P2-12 by V-P2-26; P2-13 by V-P2-27; P2-14 by V-P2-28;
P2-15 by V-P2-29; P2-16 by V-P2-30; and P2-17 by V-P2-31.
