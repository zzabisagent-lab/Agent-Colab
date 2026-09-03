# RB-CREDENTIAL-ROTATION — rotate Agent, provider or administrator credentials

- **Id:** `RB-CREDENTIAL-ROTATION`
- **Trigger:** scheduled rotation, a suspected exposure (see `RB-SECRET-LEAK`), or an operator
  leaving. Also the recovery step of several other runbooks.
- **Severity:** high. Done in the wrong order it locks the instance out of its own providers.

## Detection

1. Inventory what is in scope: `GET /api/v1/agents` for Agent service credentials,
   `GET /api/v1/accounts/{id}` for human and service credentials, provider instances for the
   Mattermost and Telegram bot tokens, and `GET /api/v1/secrets` for Broker-held references.
2. Note which credentials are referenced rather than stored: destination and adapter
   configuration holds Secret Broker references only, never values.

## Isolation

Rotation is additive, so isolation means bounding the blast radius rather than stopping traffic:

1. Rotate one credential class at a time (Agent, then Mattermost, then Telegram, then
   administrator) and confirm each before starting the next.
2. Announce the window in the ops channel; do not enter maintenance mode unless a provider
   rotation requires a restart.

## Recovery

1. **Agent:** `POST /api/v1/accounts/{account_id}/credentials/rotate` — the new token is returned
   once. Update the Agent's deployment, confirm a work poll or heartbeat succeeds with the new
   token, then `POST /api/v1/accounts/{account_id}/credentials/revoke` for the old one.
2. **Mattermost / Telegram:** create the new bot token in the provider, store it through the
   Broker (`POST /api/v1/secrets/{secret_ref}/rotate`), update the provider instance
   configuration, confirm one post and one relay succeed, then invalidate the old token in the
   provider.
3. **Administrator:** re-enroll MFA (`POST /api/v1/auth/mfa/enroll` and `/confirm`) and issue a new
   recovery code; the previous recovery code becomes single-use spent.

## Post-verification

1. Old credentials are rejected within 60 seconds of the new ones being confirmed: an
   authentication attempt with the old token returns 401 and is audited.
2. Zero message or Task loss across the window: compare Event and message counts before and after,
   and confirm the outbox drained with no dead letters.
3. Every issue, rotate and revoke appears in the audit trail with the operator's account.

## Evidence to capture

The rotation audit rows, the successful call with the new credential, the rejected call with the
old one and its timestamp delta, and the before/after Event and message counts.
