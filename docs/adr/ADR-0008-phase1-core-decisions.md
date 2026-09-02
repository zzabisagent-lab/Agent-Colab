# ADR-0008: Phase 1 core decisions

- Status: Accepted (Phase 1)
- Date: 2026-09-02

## Decisions

1. **Command bus as the only write path.** REST, MCP, and (from Phase 2) Mattermost commands
   build a `CommandContext` and execute the same handler (`server/application/bus.py`). Handlers
   read state from Event streams, authorize through the Policy Engine, append exactly one Event
   with `expected_seq`, and update projections synchronously in the same transaction.
2. **Deterministic resource ids for creates.** A `CreateTask`/`RegisterArtifact` retry with the
   same Idempotency-Key must replay, so generated ids derive from
   `(workspace, actor, scope, idempotency key)`; callers may still pass explicit ids.
3. **Application roles.** Runtime and admin connections `SET ROLE agent_colab_runtime|admin`
   with no UPDATE/DELETE on authority tables; triggers add a second, role-independent guard.
4. **Hash chains.** Events chain per aggregate; audit rows, verification revisions and key
   tombstones chain per table with daily anchors in `audit_hash_anchors`.
5. **Envelope encryption.** One AES-256-GCM DEK per (workspace, aggregate type, id) wrapped by
   the instance master key; crypto-shredding removes the wrapped DEK and appends a tombstone.
6. **Workspace-scoped projections.** Rebuild and snapshot hashes are computed per Workspace
   (the instance is single-Workspace; verification data isolation needs it). Raw streams that
   bypassed handlers are skipped and reported by the projector, never fatal.
7. **Workspace from the credential.** The request Workspace is the principal's Account
   Workspace; nothing in the request may select it.
8. **Information disclosure.** Policy denials surface as normalized 404 `code` = deny reason at
   the API (development plan §7.5); MCP returns the same code and status in its error object.
9. **Verification verdicts** require the verifier's credential; implementer, alias, and shared
   fingerprints are rejected at the API and by DB CHECK constraints; revisions are immutable.
10. **Document two-stage lifecycle** is implemented by the deterministic skeleton builder
    (layer 1); narrative (layer 2) is Phase 6.
