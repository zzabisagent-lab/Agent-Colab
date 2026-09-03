# Brainstorm engine (P6-02, P6-09)

Implements development plan §7F and spec §8.3. The session is an Event-sourced aggregate
(`bs-…`); migration `0019` adds the projection the engine reads.

## Session state

`OPEN → PAUSED → OPEN → CLOSED`. The opener is the facilitator (`brainstorm.open`) and is the only
account that may pause, resume, decide, taskify, approve a summary or close (`brainstorm.facilitate`).

| Table | Holds |
|---|---|
| `brainstorms` | topic, channel, facilitator, status, limits, turn counter, round-robin cursor, last contributor and consecutive count |
| `brainstorm_participants` | seat order (deterministic), role, agent id, turns taken |
| `brainstorm_turns` | the transcript, one row per accepted contribution, each bound to its Event |
| `brainstorm_summaries` | DRAFT/APPROVED summaries with the artifact reference and posting time |
| `brainstorm_decisions` | statement, rationale, source Events, action items, optional vote tally |
| `decision_tasks` | Decision → Task provenance (`taskify.provenance` reads it back Task → Decision) |

## Turn distribution

Agent participants are seated in join order and served round-robin from `turn_index`: the engine
queues one `brainstorm_turn` work item (transcript reference, remaining turns, expected
contribution type) for the Agent whose turn it is, and re-queues after every accepted turn. The
work item's idempotency key is `<brainstorm_id>:turn:<n>`, so a pause and resume never duplicate it.
Agents answer through the MCP tool `brainstorm_contribute` (or the REST/slash equivalents) and must
declare `IDEA|CHALLENGE|QUESTION|GUIDANCE`. Humans speak freely and an utterance without a type is
recorded as `IDEA` (§7F). An Agent contributing when another Agent holds the turn is refused with
`BRAINSTORM_NOT_YOUR_TURN`, which keeps the order reproducible without pausing the session.

## Limits

`turns_per_agent`, `max_consecutive`, `total_turns`, `budget_cost_units` and `time_limit_minutes`
(0 means unlimited; per-Agent turn limits do not apply to Humans). `server/brainstorm/limits.py`
decides purely. A breach does two things, as §7F requires and V-P6-26 checks: the offending
contribution is **rejected** with the breach code, and the session moves to `PAUSED`
(`BRAINSTORM_PAUSED`, `reason_code` = the breach) with a guidance request queued for the
facilitator. Because the rejection rolls the caller's transaction back, the pause and the guidance
request are written in their own transaction — the same pattern the Policy Engine uses for denial
audits. The facilitator then resumes (optionally adjusting limits, which also clears the
consecutive counter) or closes.

Budget consumption is read from `usage_records` scoped by `brainstorm_id`, so Agent usage reported
under the session counts against `budget_cost_units` without a second ledger.

## Summary, Decision, Taskify

`summarize` prefers an Agent that holds `brainstorm.summarize` and is **not** a participant, and
falls back to the best-scored participant (`server/agents/routing.py`, ties by ascending agent id).
The draft body is deterministic (template only, no model call): ideas, challenges, questions and
guidance grouped with `[[evt:…]]` citations, so the same transcript always yields the same bytes.
It is stored as an Artifact (the `SUMMARY_RECORDED` Event carries `artifact_id`) and kept `DRAFT`;
**nothing reaches the channel until the facilitator approves it**, at which point the summary is
posted through the normal channel outbox with role `summary`.

`decide` is facilitator-only and records statement, rationale, source Event ids, optional action
items and an optional vote tally (the tally never decides). `taskify` creates one Task per action
item through the ordinary `CreateTask` command, so §7D.1 acceptance criteria are mandatory: an
action item without criteria is refused with `DECISION_ACTION_ITEM_CRITERIA_REQUIRED`. Taskify is
idempotent per `(decision_id, item_index)` and marks the Decision `taskified`.

## Surfaces

REST `/api/v1/brainstorms` (start, list, show, transcript, participants, contributions, pause,
resume, close, summaries, summary approve, decisions, decision show, taskify); MCP tool
`brainstorm_contribute`; slash verbs `/colab brainstorm start|contribute|summarize|decide|taskify|
pause|resume|close|show`, mounted on the Command Router's resource extension point
(`RESOURCE_HANDLERS`), which also lifts `brainstorm` out of the router's later-Phase gate.

## Error codes

`BRAINSTORM_NOT_FOUND`, `BRAINSTORM_NOT_OPEN`, `BRAINSTORM_NOT_PAUSED`, `BRAINSTORM_CLOSED`,
`BRAINSTORM_FACILITATOR_ONLY`, `BRAINSTORM_NOT_A_PARTICIPANT`, `BRAINSTORM_NOT_YOUR_TURN`,
`BRAINSTORM_TOPIC_REQUIRED`, `BRAINSTORM_BODY_REQUIRED`, `BRAINSTORM_CONTRIBUTION_TYPE_REQUIRED`,
`BRAINSTORM_CONTRIBUTION_TYPE_INVALID`, `BRAINSTORM_LIMITS_INVALID`, `BRAINSTORM_TARGET_REQUIRED`;
breaches `MAX_CONSECUTIVE_EXCEEDED`, `TURNS_PER_AGENT_EXCEEDED`, `TOTAL_TURNS_EXCEEDED`,
`BUDGET_EXCEEDED`, `TIME_LIMIT_EXCEEDED`; decisions `DECISION_NOT_FOUND`,
`DECISION_STATEMENT_REQUIRED`, `DECISION_ACTION_ITEMS_INVALID`,
`DECISION_ACTION_ITEM_CRITERIA_REQUIRED`, `DECISION_HAS_NO_ACTION_ITEMS`, `DECISION_VOTE_INVALID`,
`DECISION_REQUIRED`; summaries `SUMMARY_NOT_FOUND`, `SUMMARY_NOT_DRAFT`.

## What the documents package reads (V-P6-08)

A closed session emits `BRAINSTORM_CLOSED` (payload: `brainstorm_id`, `turn_count`,
`decision_count`). The document draft is built from `brainstorm_turns` (arguments, challenges and
alternatives, open questions — each row carries its `event_id`), `brainstorm_summaries` (the
approved summary and its artifact), `brainstorm_decisions` (statement, rationale, source Events,
vote) and `decision_tasks` (follow-up work). `server/brainstorm/taskify.py::provenance` resolves a
Task back to its Decision and session for the provenance section.
