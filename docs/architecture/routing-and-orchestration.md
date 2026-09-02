# Routing, orchestration, re-routing and Verifier assignment (Phase 3: P3-06/09/13/14)

## Routing (`server/agents/routing.py`, V-P3-03/10)

`candidates()` computes the eligible set as the intersection of active ∧ online Agents, channel
membership with `write`, the required capability, remaining capacity (`agents.capacity`, capped by
`limits.concurrent_tasks`, minus the Agent's non-terminal Tasks in `tasks_projection`), policy
allow (Policy Engine `authorize`; a denial is audited as `policy.deny`) and, when the Task needs
secret handles, adapter support (`supports_secret_handles`: the Mattermost bot adapter advertises
`unsupported`). Score = domain match (2) + inverse recent load (1); ties break by ascending
`agent_id`. `select_assignee()` stores the ordered candidate snapshot in `routing_decisions` and
audits `routing.select` (`SELECTED` / `NO_CANDIDATE` / `REROUTE_LIMIT`). No secret values.

## Task graph and joins (`server/tasks/graph.py`, `server/application/orchestration.py`, V-P3-18/19)

- Limits come from the channel template `limits` (`delegation_depth`, `max_fan_out`,
  `concurrent_subtasks`; defaults 4/8/8). `create_subtask` runs `check_subtask_creation` before
  any write: `TASK_GRAPH_CYCLE` (self/ancestor), `TASK_DEPTH_EXCEEDED`, `TASK_FANOUT_EXCEEDED`,
  `TASK_CONCURRENCY_EXCEEDED`, `TASK_WORKSPACE_MISMATCH` (defense in depth: the bus already scopes
  Task loads by Workspace, so a foreign parent surfaces as the §7.5-normalized `TASK_NOT_FOUND`).
  The DB trigger on `task_edges` remains the last line against cycles.
- Delegation/reassignment to an Agent Account enqueues one durable `task_assignment` /
  `subtask_assignment` work item per assignment revision (idempotency key
  `assign:<task>:<revision>`) carrying `resume_context`; the superseded item is cancelled.
- Join policy on the parent (`join_policy`): `{"mode": "ALL"|"ANY"|"QUORUM", "quorum": n,
  "required": [child ids]}`. A child counts only when VERIFIED/COMPLETED (an unverified or
  IMPLEMENTED sub-Task never counts; a cancelled required child blocks `ALL`). When the condition
  holds, `TASK_JOIN_SATISFIED` is appended once on the parent (payload `join_policy` is the string
  `ALL|ANY|QUORUM(n)` per the pinned schema) and `task_join_state` is updated. The completion
  prerequisite `parent_join_check` (`TASK_JOIN_UNSATISFIED`) keeps a parent from completing before
  its join is met. Annotation Events (`ANNOTATION_EVENTS`) are folded without a status change.

## Re-routing (`server/agents/rerouting.py`, V-P3-04/20/25)

Triggers: `WorkReject` (`CAPABILITY_UNSUPPORTED|CAPACITY|POLICY|OTHER`, via
`work.REJECTION_HOOKS`), the inbox sweep's 120-second accept timeout (`process_sweep`), registry
hooks `on_agent_unavailable(agent_id, AGENT_OFFLINE|AGENT_SUSPENDED|AGENT_REVOKED)` and
`on_budget_exceeded`. `reroute_task` assigns the next-scored eligible candidate once
(`TASK_REASSIGNED`, `task_assignments` revision `REROUTE_<reason>`, `resume_context` = started
flag, completed progress steps, Artifacts, last progress) excluding every previous assignee; when
the single re-route is used up or no candidate exists the Task goes `WAITING`
(`NO_CANDIDATE_<reason>`, notification rule `ntf-task-waiting` → delegator + channel). The
transition table gained the §7D.3 pairs (`DELEGATED|ACCEPTED|IMPLEMENTED → WAITING`,
`ACCEPTED|RUNNING|WAITING → DELEGATED` on `TASK_REASSIGNED`). Re-routing acts as the Workspace's
system service Account (`system_principal`), which must hold `task.reassign`/`task.progress`.

## Verifier assignment (`server/verification/assignment.py`, V-P3-14/24)

`CreateVerificationRun(auto_assign=True)` fills the Verifier from `eligible_verifiers()`:
`verification.submit` policy allow ∧ independence (`check_independence`: same Account, alias,
shared credential fingerprint, same Agent) ∧ Agents active/online with capacity and a capability
in the Task domain ∧ Human requirement (risk `HIGH`/`CRITICAL`, or the channel template
`risk_policy.requires_human_approval` containing `verification`). Score = domain match (2) +
inverse load (1) + Human preference (1 when required); ties by ascending `account_id`. The offer
is recorded in `verifier_assignments` (10-minute deadline) and Agents receive a
`verification_assignment` work item with criteria, evidence manifest, Artifact refs, target
commit/snapshot hash and read-only access references; Humans are reached by the
`VERIFIER_ASSIGNED` notification rule. `sweep_timeouts()` cancels a silent run, creates the next
run for the next candidate, and when none remains appends `VERIFIER_ASSIGNMENT_EXHAUSTED`
(rule `ntf-verifier-exhausted` → Administrators + delegator) and moves the Task to `WAITING`.

Migration `0010`: `routing_decisions`, `verifier_assignments`, `task_join_state`.
