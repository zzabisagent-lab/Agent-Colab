# Policy Engine (P1-03)

Authority: spec §4.3, §15; development plan §3.1, §6.7, §6.9; catalog `policy/*.yaml` (P0-12).

## Data sources (committed rows only)

| Question | Table(s) |
|---|---|
| Who is the principal? | `accounts` (+ `agents` for the agent id) — always from the credential (P1-05), never from the body |
| Which roles apply now? | `principal_role_assignments` (not revoked, `valid_from <= now < valid_to`) → `roles` (active) → `role_versions` at `roles.current_version` |
| Which capabilities? | `agent_capabilities` → `capabilities` |
| Channel membership? | `channel_members` (active) joined to `channels` by id or `channel_id` |
| Risk / approval? | `policy/risk-rules.yaml` via `PolicyCatalog.risk_for(action, side_effect)` |

Projections (`tasks_projection`, `approvals_projection`) are never consulted for permission
decisions (spec §2 principle 16).

## Evaluation order (`Authorizer.authorize`)

1. Principal lookup: unknown → `PRINCIPAL_UNKNOWN`, not `ACTIVE` → `PRINCIPAL_INACTIVE`.
2. `PolicyEngine.evaluate` over the committed roles with the vocabulary of `permissions.yaml`:
   unknown permission → `UNKNOWN_PERMISSION`; any explicit deny (`deny:` patterns) →
   `EXPLICIT_DENY`; scope constraints (`SCOPE_DOMAIN`, `SCOPE_SIDE_EFFECT`, `SCOPE_CHANNEL`,
   `SCOPE_RESOURCE`); no grant → `DEFAULT_DENY`. Precedence is `explicit deny > scope
   restriction > allow`, deny by default, independent of role order.
3. Required capability must be granted to the Agent → `CAPABILITY_MISSING`.
4. A channel-scoped request requires active membership → `CHANNEL_NOT_MEMBER`.
5. Risk classification from the catalog (unknown action → HIGH, flagged); a side effect raises a
   LOW action to MEDIUM. At least one granting role must have `max_risk >= risk` →
   otherwise `ROLE_MAX_RISK_EXCEEDED`.
6. `approval_required` is true when the catalog approval is `human_1`/`human_2` (HIGH+) or a
   role constraint lists the permission in `requires_human_approval`; `human_only` follows §7E.

Every DENY appends one `audit_events` row (`action=policy.deny`, `result=DENY`, `error_code`
= reason) with **redacted** metadata: permission, action, reason, role ids, channel/domain,
required capability. Request bodies, tokens, or values are never written (`redact_metadata`).

## Snapshot

`PolicySnapshot` (role `(role_id, version)` pairs, capability ids, SHA-256) is returned with
every ALLOW and by `Authorizer.snapshot`; Tasks pin it at creation (spec §4.2). Role changes
create a new `role_versions` row (`commit_role_version`, `FOR UPDATE` on the role) and advance
`roles.current_version` atomically; the next authorization uses the new version (V-P3-02: zero
stale allows). `role_versions` rows are immutable (trigger).

## Public API

```python
Authorizer(repository=None, catalog=None, clock=None, audit_sink=None)
Authorizer.authorize(session, principal_account_id, AuthorizationRequest(...)) -> Authorization
Authorizer.require(session, principal_account_id, request) -> Authorization  # raises AuthorizationDenied
Authorizer.snapshot(session, principal_account_id) -> PolicySnapshot | None
PostgresPolicyRepository.create_role / commit_role_version / assign_role / revoke_role / grant_capability
```
