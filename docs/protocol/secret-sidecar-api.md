# Secret Broker API for Adapters and the Secret Sidecar (P4-06/P4-07/P4-12)

Base path `/api/v1/secrets`. Authentication: the Agent Account's service token
(`Authorization: Bearer <token>`) or the mTLS-derived principal (`Bearer mtls:<nonce>` issued by
the reverse proxy contract of P3-10). The caller must be a registered Agent; the Broker binds
every handle to that Agent (`agent_id` from the `agents` row of the Account).

Values are returned **exactly once** and only from `POST /resolve`. No response, error, Event,
audit row or log ever contains a value, its length or a hash of it.

## Stable error codes

| HTTP | code | meaning |
|---|---|---|
| 403 | `SECRET_NOT_FOUND` | unknown handle / lease / secret (indistinguishable from forbidden) |
| 403 | `SECRET_SCOPE_DENIED` | handle scope (Agent, Task, action, work item) does not match the caller |
| 403 | `SECRET_LEASE_EXPIRED` | lease or grant TTL elapsed |
| 403 | `SECRET_HANDLE_USED` | single-use handle already resolved |
| 403 | `SECRET_HANDLE_REVOKED` | lease/grant revoked (Task ended, Agent revoked, admin, rotation) |
| 403 | `SECRET_HANDLE_HOST_MISMATCH` | handle bound to another `sidecar_instance_id` |
| 403 | `SECRET_EXPOSURE_APPROVAL_REQUIRED` | `purpose=llm_context` without an APPROVED Human approval |
| 503 | `SECRET_PROVIDER_UNAVAILABLE` | master key / provider not available |

Every denial produces exactly one redacted `secret.resolve_denied` (or `secret.lease_denied`)
audit entry with the code as `error_code`.

## Agent / sidecar endpoints

### `POST /api/v1/secrets/{secret_ref}/leases` → 201

Issue a one-time handle under a matching grant (permission `secret.lease`).

```json
{"task_id": "task-…", "action": "deploy", "work_item_id": "wi-…", "sidecar_instance_id": "sc-host-1", "ttl_seconds": 300}
```
```json
{"lease_id": "lease-…", "handle": "sh-<32 hex>", "secret_ref": "sec-…", "expires_at": "2026-…Z", "single_use": true}
```
The handle appears only in this response. TTL defaults to the grant's (300 s), maximum 3600 s.

### `POST /api/v1/secrets/resolve` → 200

```json
{"handle": "sh-<32 hex>", "sidecar_instance_id": "sc-host-1", "work_item_id": "wi-…", "task_id": "task-…", "action": "deploy", "purpose": "adapter"}
```
```json
{"lease_id": "lease-…", "secret_b64": "<base64 value>"}
```
`purpose` is `adapter` (default) or `llm_context` (needs the grant's exposure approval). All
scope fields that the lease carries must match; a lease issued with `sidecar_instance_id` can
only be resolved with the same id (host binding, §9.4). Single-use handles answer
`SECRET_HANDLE_USED` on the second call.

### `POST /api/v1/secrets/leases/{lease_id}/ack-cleanup` → 200

`{"lease_id": "lease-…", "acknowledged": true}` — the sidecar confirms that memory and child
environments were cleared after a revocation (metrics for the ≤ 5 s rule).

### `GET /api/v1/secrets/revocations?since=<seq>&max_wait_s=5` → 200

Long-poll (≤ 5 s) for revocations after `since`; only revocations touching the caller's Agent.
```json
{"items": [{"seq": 12, "kind": "task", "target_id": "task-…", "lease_ids": ["lease-…"], "reason": "TASK_ENDED", "occurred_at": "…"}], "next_since": 12}
```
`kind` ∈ `grant|lease|task|agent|secret`. A sidecar must wipe every listed lease immediately.

### `GET /api/v1/secrets/revocations/stream?since=<seq>` (SSE)

`event: revocation`, `id: <seq>`, `data: <same JSON as one item>`. Reconnect with
`since=<last id>`; fall back to the 5-second poll when the stream is unavailable.

## Administrator endpoints (permissions in parentheses)

| Method/path | body → response |
|---|---|
| `POST /api/v1/secrets` (`secret.register`) | `{"name","value_b64","metadata"}` → 201 `{"secret_ref","version":1,"event_id"}` |
| `GET /api/v1/secrets` (`secret.register`) | `{"items":[{secret_ref,name,provider,current_version,metadata,status,created_at,rotated_at}]}` — metadata only |
| `GET /api/v1/secrets/{secret_ref}` | one metadata view |
| `POST /api/v1/secrets/{secret_ref}/rotate` (`secret.register`) | `{"value_b64"}` → `{"secret_ref","version"}`; leases of older versions are revoked (`SECRET_ROTATED`) |
| `POST /api/v1/secrets/{secret_ref}/grants` (`secret.grant`) | `{"agent_id","task_id?","action?","ttl_seconds":300,"single_use":true,"valid_for_seconds?"}` → 201 grant view |
| `GET /api/v1/secrets/grants/{grant_id}` | grant view |
| `POST /api/v1/secrets/grants/{grant_id}/revoke` | `{"reason_code"}` → `{"grant_id","revoked_leases":[…]}` |
| `POST /api/v1/secrets/revoke?target_id=…` | `{"kind":"task|agent|lease|secret","reason_code"}` → revoked leases |
| `POST /api/v1/secrets/{secret_ref}/exposure-requests` (`secret.grant`) | `{"grant_id","task_id","reason"}` → 201 `{"approval_id","grant_id","status":"PENDING"}` — a Human approves through the Phase 1 approvals API; only an APPROVED, unexpired decision unlocks `purpose=llm_context` |

Idempotency: every write needs `Idempotency-Key`. All writes are audited; grant creation,
access and revocation are Events (`SECRET_GRANT_CREATED`, `SECRET_ACCESSED`,
`SECRET_GRANT_REVOKED`); registration/rotation append `SECRET_REGISTERED`.
