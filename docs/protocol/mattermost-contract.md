# Mattermost interaction contract (P0-10)

Authority: spec §8.7, development plan §7A, §7.5, §7B.2, §21.1. Machine-readable form:
`schemas/api/commands/<resource>.<verb>.v1.schema.json` (45 files, generated from the `VERBS`
table in `server/channels/commands.py` and drift-checked by tests),
`schemas/api/mattermost/task-card.v1.schema.json`, `brainstorm-card.v1.schema.json`,
`thread-rules.v1.json`, `action-callback.v1.schema.json`. Spike results: `mattermost-spike.md`.

## 1. Command grammar

`/colab <resource> <verb> [positional...] [--key value ...]`; the identical grammar is accepted
after an `@colab` mention. Anything else — including free text that merely mentions "colab" or
looks like a command — is **never** interpreted (`COMMAND_PREFIX_MISSING`, no side effects).

Tokenization: whitespace-separated, `"…"`/`'…'` quoting with backslash escapes; options as
`--key value`, `--key=value`, or a bare `--flag`; repeated options accumulate (`--criteria a
--criteria b`); mention lists accept `@a,@b` or `"@a @b"`. Resource and verb are
case-insensitive; arguments are case-preserving.

| resource | verbs | required permission | minimum arguments | target rule |
|---|---|---|---|---|
| task | create | `task.create` | `"title" --criteria "…"` (≥ 1) | — |
| task | delegate, reassign | `task.delegate` | `[task_id] --to @user\|agent-id` | task thread |
| task | accept, reject | `task.accept` | `[task_id]` (reject: `--reason CAPABILITY_UNSUPPORTED\|CAPACITY\|POLICY\|OTHER`) | task thread |
| task | progress | `task.progress` | `[task_id] "message"` | task thread |
| task | submit | `task.submit` | `[task_id] --evidence <ref>` (≥ 1, one per criterion) | task thread |
| task | complete | `task.complete` | `[task_id]` | task thread |
| task | cancel | `task.cancel` | `[task_id]` | task thread |
| task | show | `task.read` | `[task_id]` | task thread |
| task | list | `task.read` | — (`--status --assignee --limit≤100`) | — |
| approve | request | `approval.request` | `[task_id] --action <action>` | task thread |
| approve | grant, reject | `approval.decide` | `<approval_id>` | — |
| approve | show, list | any `approval.*` | show: `<approval_id>` | — |
| verify | assign | `verification.assign` | `[task_id]` | task thread |
| verify | pass, fail | `verification.submit` | `[task_id] --evidence <ref>` (≥ 1) | task thread |
| verify | block | `verification.submit` | `[task_id] --reason "…"` | task thread |
| verify | show | any `verification.*` | `[task_id]` | task thread |
| brainstorm | start | `brainstorm.facilitate` | `"topic" --participants @a,@b` (+ `--turns-per-agent 5 --max-consecutive 1 --total-turns 40 --budget <cost_units> --time <min>`) | — |
| brainstorm | contribute | `brainstorm.contribute` | `[brainstorm_id] "text" [--type IDEA\|CHALLENGE\|QUESTION\|GUIDANCE]` | brainstorm thread |
| brainstorm | summarize | `brainstorm.summarize` | `[brainstorm_id]` | brainstorm thread |
| brainstorm | decide | `brainstorm.facilitate` | `[brainstorm_id] "statement" --rationale "…" --source <event id>…` (`--vote` optional) | brainstorm thread |
| brainstorm | taskify, pause, resume, close | `brainstorm.facilitate` | `[brainstorm_id]` | brainstorm thread |
| brainstorm | show | any `brainstorm.*` | `[brainstorm_id]` | brainstorm thread |
| doc | show | any `document.*` | `<task_id \| brainstorm_id>` | task or brainstorm thread |
| doc | review | `document.finalize` | `<task_id \| brainstorm_id> --result approve\|reject` | task or brainstorm thread |
| doc | publish | `document.publish` | `<task_id \| brainstorm_id>` | task or brainstorm thread |
| schedule | show | any `schedule.*` | `<schedule_id \| run_id>` | — |
| schedule | list | any `schedule.*` | — | — |
| schedule | run-now, cancel-run | `schedule.run` | `<schedule_id>` / `<run_id>` | — |
| schedule | pause, resume | `schedule.manage` | `<schedule_id>` | — |
| link | start, confirm | self (unlinked allowed) | confirm: `<6-digit code>` | — |
| notify | mute, unmute, digest | self | — | — |
| help | — | everyone (unlinked allowed) | `[resource]` | — |

Read verbs of `task`/`verify` use `task.read`; other read verbs require any permission of the
resource's family (`approval.*`, `verification.*`, `brainstorm.*`, `document.*`, `schedule.*`),
matching the "`<resource>.*`" column of §7A.2. The exact vocabulary is fixed in
`policy/permissions.yaml` (P0-12).

Rules:

- **Thread context**: inside a Task/Brainstorm thread the `[task_id]`/`[brainstorm_id]`
  positional may be omitted and resolves to the thread's subject (`target_source = thread`).
  Outside a matching thread the omission is `COMMAND_TARGET_REQUIRED`.
- **Unlinked users** (no active ExternalIdentityLink) may run only `link start`, `link confirm`
  and `help`; everything else is `COMMAND_UNLINKED_RESTRICTED` before argument parsing.
- **Validation** uses the verb's JSON Schema; the first violation becomes
  `COMMAND_ARGS_INVALID` with the field path and the correct example.
- Errors are ephemeral (`CommandError.code`, `message_key`, `example`, `detail`) and create no
  side effects; successful responses are posted publicly in the thread.
- Idempotency: `provider_instance + post_id` (P2-10); the same application command handler and
  Policy as REST/MCP (§7.5).

Stable error codes: `COMMAND_PREFIX_MISSING`, `COMMAND_RESOURCE_UNKNOWN`,
`COMMAND_VERB_UNKNOWN`, `COMMAND_ARGS_INVALID`, `COMMAND_TARGET_REQUIRED`,
`COMMAND_UNLINKED_RESTRICTED`.

## 2. Task card and thread

`thread-rules.v1.json` (TR-01…TR-13): one root post per Task/Brainstorm (the card),
in-place card edits on every transition, exactly one immutable thread reply per transition,
progress coalescing per Task in 10-second windows, bodies over 16,000 characters stored as an
Artifact and linked, sub-Task link cards in the parent thread, buttons as conveniences with
server-side re-authorization and once-only duplicate clicks, HIGH/CRITICAL approvals decided in
the web console only, Agent identity display per the spike decision.

`task-card.v1.schema.json` fixes the card render model (title, status badge, risk, assignee,
verification_status, pending approvals with decision path `button|web`, latest progress with
coalesced count, artifact/document links, sub-task join status, permission-gated buttons,
card_version, language). `brainstorm-card.v1.schema.json` fixes participants, remaining turns,
budget consumption, status, pause reason, decisions and buttons.

## 3. Interactive action callback

Mattermost does not sign action callbacks, so the server signs the `context` it embeds when
rendering a button and validates it on receipt, in this order (`server/channels/contract.py`):

1. `integration_token` (constant-time compare) → `CALLBACK_SIGNATURE_INVALID`
2. `timestamp` within ±300 s of the server clock → `CALLBACK_TIMESTAMP_EXPIRED`
3. `signature = HMAC-SHA256(key, "timestamp|nonce|body_sha256")` → `CALLBACK_SIGNATURE_INVALID`
4. `body_sha256` equals the SHA-256 of the received body → `CALLBACK_BODY_HASH_MISMATCH`
5. one-time `nonce` (consumed last, so a forged request never burns a valid nonce) →
   `CALLBACK_NONCE_REUSED`

Every rejection is HTTP 401/403 with zero domain Events and one redacted AuditEvent; only after
validation does the callback reach the same command handler as `/colab` (V-P2-26).

## 4. Slash command transport (from the spike)

Mattermost delivers `/colab` as `application/x-www-form-urlencoded` with `token`, `team_id`,
`channel_id`, `user_id`, `user_name`, `command`, `text`, `trigger_id`, `response_url`. The
Command Router verifies `token` against the provider instance's registered command token
(constant time), parses `command + " " + text`, and answers ephemeral JSON for errors.
`@colab` mentions arrive as WebSocket `posted` events and take the same path.

## 5. Spike decision

Slash command registration: **possible**. `override_username`/`override_icon_url`: **possible**
when the server confirms `EnablePostUsernameOverride`/`EnablePostIconOverride` via
`GET /api/v4/config`; otherwise the `[agent-name]` prefix fallback is pinned per provider
instance. Details and evidence: `mattermost-spike.md`, `evidence/phase-0/spikes/mattermost/`.
