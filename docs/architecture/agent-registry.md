# Agent Registry, Roles and Limits (P3-01 / P3-02 / P3-08)

## Registry (`server/agents/registry.py`, `server/application/agents.py`)

- An Agent is an `agents` authority row plus an Account (`account_type = agent`,
  `acct-<agent_id>`) with one service credential. `RegisterAgent` returns the Agent's service
  token exactly once in the command result; only its hash is stored and the audit row carries
  the fingerprint. `endpoint` must not carry secret values (keys that look like secrets are
  rejected unless they end in `_ref`; known secret prefixes are rejected); `credential_ref` is a
  Secret Broker reference.
- Commands and Events: `RegisterAgent` → `AGENT_REGISTERED`, `UpdateAgent` → `AGENT_UPDATED`,
  `TestAgentConnection` (probe through the §7.3 Adapter contract; stores `capabilities_snapshot`,
  audit only), `ActivateAgent` → `AGENT_ACTIVATED` (needs a stored or supplied probe),
  `SuspendAgent` → `AGENT_SUSPENDED`, `RevokeAgent` → `AGENT_REVOKED`, `RecordHeartbeat` →
  `AGENT_HEARTBEAT_RECORDED`, `MarkOffline`/`SweepOffline` → `AGENT_MARKED_OFFLINE`.
- Runtime columns (`status`, `online`, `capacity`, `last_heartbeat_at`, `missed_heartbeats`,
  `lifecycle_hash`, `last_event_id`, `last_aggregate_seq`) are a projection: every handler folds
  the Agent's stream with `registry.fold` and writes the result, and `registry.rebuild` replays
  the same fold, so live state and rebuilt state agree by construction (V-P3-17). The lifecycle
  hash is a SHA-256 chain over `(event_id, type, aggregate_seq, occurred_at)`.
- Blocking is immediate: suspend/revoke set the Account to `SUSPENDED` (the Authorizer denies
  `PRINCIPAL_INACTIVE` on the very next request) and revoke also revokes the credentials.
  `security_revoke=true` is recorded in the Event for the re-routing package to act on in-flight
  Tasks; otherwise in-flight Tasks keep their policy snapshot.
- Heartbeats every 30 s carry health, capacity, and §7C usage or a `usage_unavailable` reason
  (`ADAPTER_NO_METERING | MODEL_UNKNOWN | ERROR`); usage is stored through `record_usage`. The
  sweep marks an Agent `offline` after 3 misses or 90 s; a returning heartbeat re-confirms
  capabilities and restores `active`/`online` at once.

REST: `POST/GET /api/v1/agents`, `GET/PATCH /api/v1/agents/{id}`, `POST .../test-connection |
activate | suspend | revoke | heartbeat`, `POST /api/v1/agents/sweep-offline`,
`GET .../lifecycle`. Permissions: `agent.manage` (administration), `agent.self` (an Agent's own
heartbeat). Views never include credential material.

## Roles (`server/application/roles.py`, `server/api/v1/roles.py`)

- `CreateRole`/`CommitRoleVersion` append `ROLE_VERSION_CREATED` *before* inserting the immutable
  `role_versions` row that references it; `AssignRole`/`RevokeRole` append
  `PRINCIPAL_ROLE_ASSIGNED`/`PRINCIPAL_ROLE_REVOKED` on the `account` aggregate. Permission
  patterns are validated against the policy vocabulary. Permission: `admin.accounts`.
- The Policy Engine reads `roles.current_version` at decision time, so the first authorization
  after a commit follows the new version (V-P3-02); explicit deny wins across Roles (V-P3-09).
- `GET /api/v1/roles/effective?account_id=&permission=` previews the effective Roles and explains
  the decision (`explicit deny > scope restriction > allow`) without auditing.

## Limits (`server/agents/limits.py`)

- `enforce_limits(session, agent_id, kind, clock, ...)` runs before a side effect for Agent
  actors: `work_poll`/`work_result` (`request`) and `AcceptTask` (`task_accept`). Checked limits:
  `requests_per_minute` (`agent_rate_windows`), `concurrent_tasks` (accepted, non-terminal Tasks
  of the Agent's Account), `brainstorm_turns` (today's `brainstorm_turn` work items),
  `daily_cost_units`/`per_task_cost_units` (`usage_for` on the `agent_daily`/`agent_task` budget
  scopes), `per_task_wall_ms` (summed `usage_records.wall_ms`).
- Exceeding raises `AGENT_LIMIT_EXCEEDED` (HTTP 429, `extra = {limit, configured, current}`)
  and writes an `agent.limit_exceeded` audit row in its own transaction, so the rejection is
  audited although the command rolls back; no Event and no side effect are produced (V-P3-15).
