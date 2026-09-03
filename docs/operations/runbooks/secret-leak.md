# RB-SECRET-LEAK — a secret value may have escaped the Broker

- **Id:** `RB-SECRET-LEAK`
- **Trigger:** alert `SECRET_CANARY_FOUND`, a `secret.resolve_denied` spike, or any report that a
  value appeared in a channel, log, Event or document.
- **Severity:** critical. Treat every suspected leak as a real one until the scan says otherwise.

## Detection

1. Scan every persisted and emitted surface for registered canaries and for the suspected value:
   `uv run python -c "from server.secrets.canary import scan; ..."` or, in an incident shell, the
   same helper the tests use (`server/secrets/canary.py::scan`), which covers Events, audit
   metadata, Tasks, Schedule Runs, attempts, notices and versions, the delivery outbox, channel
   posts, work items and receipts, usage records, budget alerts, notifications, document
   manifests and the artifact and document roots.
2. Read the access trail: `GET /api/v1/audit?action=secret.resolved&from=<iso>&to=<iso>` and
   `GET /api/v1/audit?action=secret.resolve_denied&from=<iso>`.
3. Identify the grant and the leases involved: `GET /api/v1/secrets/{secret_ref}` (metadata only —
   the value is never returned) and `GET /api/v1/secrets/grants/{grant_id}`.

## Isolation

1. Revoke the grant, which invalidates every outstanding handle:
   `POST /api/v1/secrets/grants/{grant_id}/revoke`.
2. If the blast radius is wider, revoke by target:
   `POST /api/v1/secrets/revoke?target_id=<agent_id|task_id|secret_ref>` with
   `{"kind": "agent|task|secret", "reason_code": "LEAK"}`.
3. Confirm new resolves fail: a `POST /api/v1/secrets/resolve` with an outstanding handle must
   answer 403 `SECRET_HANDLE_REVOKED`. Sidecars clear their memory within 5 seconds of the
   revocation reaching them (`GET /api/v1/secrets/revocations`).

## Recovery

1. Rotate the secret: `POST /api/v1/secrets/{secret_ref}/rotate` with the new value. Older
   versions' leases are revoked by the rotation.
2. Re-grant only what is still needed: `POST /api/v1/secrets/{secret_ref}/grants`.
3. If the value reached a published document, republish the corrected version through
   `POST /api/v1/publishing/...` so the destination no longer serves the leaked text.

## Post-verification

1. Re-run the canary and value scan from Detection; it must return zero hits.
2. Confirm the rotation and every revocation are in the audit trail
   (`GET /api/v1/audit?action=secret.revoked`), and that no plaintext exists at rest
   (`SELECT count(*) FROM secret_versions WHERE ciphertext IS NULL` must be 0).
3. Record a residual-risk entry if any consumer could not be rotated in the window.

## Evidence to capture

The scan output (locations only, never values), the audit rows for revoke and rotate, the
revocation feed entries, and the timestamps of detection, isolation and recovery.
