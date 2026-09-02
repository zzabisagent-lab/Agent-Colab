# ADR-0010: Phase 3 generic-Agent decisions

- Status: Accepted (Phase 3)
- Date: 2026-09-02

## Decisions

1. **Adapter types are registered, not hard-coded.** `server/agents/adapters/contract.py` defines
   the §7.3 contract (`probe/deliver/invoke/cancel/heartbeat/normalize_error`) with the stable
   error codes `ADAPTER_TIMEOUT|ADAPTER_UNREACHABLE|ADAPTER_AUTH_FAILED|ADAPTER_BAD_RESPONSE|
   ADAPTER_RATE_LIMITED|CAPABILITY_UNSUPPORTED|ADAPTER_CANCELLED|ADAPTER_INTERNAL`. Built-in types
   (`mcp`, `webhook`, `mattermost_bot`) register themselves; external types register through
   `AGENT_COLAB_ADAPTER_PLUGINS` (`module:attribute`) and must pass the same conformance suite
   (V-P3-12).
2. **Registry runtime state lives on `agents`.** Migration 0008 adds runtime columns (status,
   online, capacity, limits, delivery modes, capability snapshot, heartbeat bookkeeping, lifecycle
   hash). Credential material is a Secret Broker reference (`credential_ref`); endpoint config
   rejects secret-looking values.
3. **Package-owned migration slots.** Migrations 0009 (delivery), 0010 (orchestration) and 0011
   (registry extras) are pre-chained so that parallel packages never conflict on revision ids.
4. **Lifecycle history is hashed.** Agent lifecycle Events form a SHA-256 chain stored as
   `lifecycle_hash`; a projection rebuild must reproduce the same state and hash (V-P3-17).
5. **Deterministic routing.** Eligible set = active ∧ online ∧ channel membership ∧ capability ∧
   capacity ∧ policy allow (∧ secret-handle support when the Task needs handles); ties break by
   ascending `agent_id`; decisions are audited with the candidate snapshot.
6. **Limits are enforced server-side before side effects.** Concurrent Tasks, requests per minute,
   brainstorm turns, cost_units (via budget reservations) and wall time reject with
   `AGENT_LIMIT_EXCEEDED` plus an audit entry and zero Events.
7. **Console parity.** Every console action calls the same REST endpoints as API clients; the
   V-P3-13 test compares the audit trail of a console-driven lifecycle with an API-driven one.

## Consequences

- Adding an adapter type never touches the core: contract + registration + conformance report.
- Verifier assignment and re-routing reuse the same eligibility primitives as routing, so a
  revoked or offline Agent disappears from all three at once.
