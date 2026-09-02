# Identity core (P1-05)

Authority: development plan §3.1 (Identity, External Identity Link), §6.5, §7.1, §7A.5, §21.1;
spec §4.1, §9.1–9.2, §10.2, §15.2. Tests: V-P1-08, V-P1-23 (Phase 2 extends with V-P2-20~22, V-P2-27).

## Principals come from credentials only

`server/identity/principals.py` resolves a `Principal` from exactly one of:

| Credential | Storage | Resolution |
|---|---|---|
| service token (`Authorization: Bearer`) | `service_credentials.token_hash` = SHA-256 of a random 256-bit token; `fingerprint` = SHA-256 of the hash | active credential ∧ ACTIVE account |
| Human session (cookie `agent_colab_session`) | `account_sessions.session_token_hash`, `expires_at`, `revoked_at`, `mfa_verified_at` | not expired ∧ not revoked ∧ ACTIVE account; `mfa_verified`/`reauth_at` exposed for Phase 4 |
| external identity link | `external_identity_links` | only status `active`; `credential_kind=external_link` |

Rotation issues a new credential and revokes the old one in the same transaction; the old token is
rejected immediately (`resolve_service_token` returns None). Every issue/revoke is audited with the
fingerprint only.

**Spoof guard (V-P1-08).** Body keys `actor_account_id`, `on_behalf_of`, `actor`, `impersonate`,
`as_account` and headers `X-Colab-Actor`, `X-Actor-Account`, `X-On-Behalf-Of`, `X-Impersonate` are
never read for identity. `detect_actor_claims` lists their *names*; `assert_no_actor_claims` /
`current_principal` record an `identity.spoof_attempt` audit row (result `IGNORED`, claimed values
never stored) and return the credential principal unchanged. Legitimate parameters such as
`implementer_account_id` or `account_id` (targets of a command) are not claims.

## External identity links (V-P1-23)

`server/identity/external_links.py` (`ExternalLinkService` over an `IdentityRepository` and the
`EventStore` protocol):

- `link_id = "link-" + sha256(provider_instance_id | external_user_id)[:24]` — deterministic, so the
  aggregate exists from the first challenge Event.
- `start_challenge` → 8-digit code, SHA-256 at rest, TTL 10 min, single-use, returned once for DM
  delivery; Event `IDENTITY_LINK_CHALLENGED`. Refused with `EXTERNAL_IDENTITY_DUPLICATE` while a
  non-revoked link exists, or `EXTERNAL_IDENTITY_LOCKED` during a lockout.
- `confirm_challenge(path="web")` → `active`/`signed_challenge` + `IDENTITY_LINK_VERIFIED`;
  `path="command"` (no web session) → `pending_admin`/`admin_approval`, then
  `approve_pending_link` by an Administrator → `active` + `IDENTITY_LINK_VERIFIED`.
- Failures: wrong code `EXTERNAL_IDENTITY_CHALLENGE_INVALID` (counted); the 5th failure locks
  the (instance, user) for 15 minutes (`EXTERNAL_IDENTITY_LOCKED`, also blocks new challenges);
  expired `..._EXPIRED`; reused `..._USED`. Unlock is time-based via the injectable `Clock`.
- `suspend_link` (`active|pending*` → `suspended`, `IDENTITY_LINK_SUSPENDED`) and `revoke_link`
  (→ `revoked`, `IDENTITY_LINK_REVOKED`); other transitions `EXTERNAL_IDENTITY_TRANSITION_INVALID`.
  A revoked (instance, user) may be linked again, possibly to another Account; the row is reused
  (schema `UNIQUE(provider_instance_id, external_user_id)`), history lives in Events/audit.
- `resolve_command_principal` is read-only: only an `active` link with an ACTIVE account yields a
  principal; everything else is `EXTERNAL_IDENTITY_NOT_ACTIVE` with zero side effects.
- Isolation: the same external user id on another provider instance is a separate link; one
  Account may hold links on several instances.
- DB guarantees: `UNIQUE(provider_instance_id, external_user_id)` and the partial unique index on
  active rows; every transition also writes an audit row (`identity.link_*`).

## REST (Phase 1 minimum, `server/api/v1/identity.py`, prefix `/api/v1/identity`)

`GET /me`, `POST /links/challenge`, `POST /links/confirm`, `POST /links/{link_id}/suspend`,
`POST /links/{link_id}/revoke`. Writes require `Idempotency-Key`; all use the `current_principal`
dependency (`server/api/deps.py`) and Problem Details. The router is mounted by the parent; the
Event store is taken from `app.state.event_store` (InMemory fallback until P1-02 is wired).
