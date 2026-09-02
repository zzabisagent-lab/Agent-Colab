# ADR-0011: Phase 4 admin, setup and secret decisions

- Status: Accepted (Phase 4)
- Date: 2026-09-02

## Decisions

1. **One provider contract, values stay inside the Broker boundary.** `server/secrets/provider.py`
   defines `put/lease/resolve/revoke/rotate/health` with value-free stable error codes. No log,
   Event, audit entry, error or API response carries a secret value, its length or a hash of it.
2. **Fail-closed re-authentication seam.** `server/security/reauth.require_recent_mfa` guards
   every critical action (setup reconfiguration, HIGH+ approval decisions, break-glass, hard delete,
   maintenance mode). Until the MFA package installs its verifier the check always fails.
3. **Package-owned migration slots 0012–0015** so five parallel packages never conflict on
   revision ids; each downgrade list is owned by the package that fills the slot.
4. **Sidecar is a separate package with an HTTP contract.** `docs/protocol/secret-sidecar-api.md`
   fixes resolve/revocation-feed/ack shapes; the sidecar binds handles to its instance id, keeps
   values in memory only and reacts to revocation within 5 s (SSE push, 5-second poll fallback).
5. **Setup persists in the mandated order** (DB → key provider → Owner/TOTP/recovery → integrations
   → atomic CONFIGURED/LOCKED); pre-DB state lives only in the sealed local store and in-memory
   handles; `/setup` is loopback-only unless HTTPS/TLS proxy, client mTLS, allowlist and a valid
   token all hold.
6. **Hard delete never rewrites Events.** Only DEK destruction, a display-redaction marker and a
   signed tombstone ledger entry happen; restores reconcile the ledger before the service opens.
7. **Console parity and accessibility are tested, not assumed.** Every screen calls the same REST
   endpoints as API clients; an axe WCAG 2.1 AA scan runs on every route and critical flows are
   exercised by keyboard (V-P4-18).

## Consequences

- Secret handling can be audited by scanning for canary values across chat, Events, logs and
  Documents (V-P4-14); a leak is a test failure.
- Adding an external secret provider is a registration plus the conformance of the same tests.
