# Secret sidecar (`sidecar/`, package `agent-colab-sidecar`)

Development plan §9.4; validation plan V-P4-31. The sidecar runs on the Agent host, next to the
Agent process, and is the only component outside the Broker that ever holds a secret value.

## Deployment

- Python package `agent-colab-sidecar` (stdlib + httpx) or the OCI image built from
  `sidecar/Dockerfile` (non-root user, `--read-only`, tmpfs runtime dir).
- Authentication to the Broker: the Agent Account's service token
  (`AGENT_COLAB_SIDECAR_TOKEN`) or a client certificate (`AGENT_COLAB_SIDECAR_CLIENT_CERT`/`_KEY`)
  verified by the Broker's reverse proxy (mTLS).
- Runtime directory (`AGENT_COLAB_SIDECAR_RUNTIME_DIR`, default `$XDG_RUNTIME_DIR/agent-colab-sidecar`,
  mode 0700): holds only the owner-only `instance-id` file and Unix socket inodes. It must be
  tmpfs; nothing else is ever written by the sidecar.

## Broker API used (docs/protocol/secret-sidecar-api.md)

| Call | Purpose |
|---|---|
| `POST /api/v1/secrets/resolve {handle, sidecar_instance_id, work_item_id?, task_id?, action?, purpose}` | one-time resolve → `{lease_id, secret_b64}` (an optional `expires_at` is honoured; otherwise the §9.3 default of 300 s applies locally); 403 `{code}` on denial |
| `GET /api/v1/secrets/revocations/stream?since=` (SSE `event: revocation`, `id: <seq>`) | revoke push; items carry `kind`, `target_id`, `lease_ids[]`, `reason` |
| `GET /api/v1/secrets/revocations?since=&max_wait_s=` (long-poll ≤ 5 s) → `{items, next_since}` | fallback when the stream is unavailable or closes |
| `POST /api/v1/secrets/leases/{lease_id}/ack-cleanup` → `{lease_id, acknowledged}` | cleanup acknowledgement after a revocation |

Denial codes surface unchanged: `SECRET_NOT_FOUND`, `SECRET_SCOPE_DENIED`,
`SECRET_LEASE_EXPIRED`, `SECRET_HANDLE_USED`, `SECRET_HANDLE_REVOKED`,
`SECRET_HANDLE_HOST_MISMATCH`, `SECRET_EXPOSURE_APPROVAL_REQUIRED`; transport failures map to
`BROKER_UNAVAILABLE` / `BROKER_AUTH_FAILED` / `BROKER_BAD_RESPONSE`.

## Host binding

Every resolve carries the sidecar instance id (`sc-<hex>`, stable per host through the
runtime-dir file, in-memory only when no runtime dir exists). The Broker binds the handle to the
instance that the work item was delivered for; a resolve from another instance is refused with
`SECRET_HANDLE_HOST_MISMATCH` and no bytes are returned.

## Injection modes

| Mode | Mechanism | Invalidation |
|---|---|---|
| `fd` (default) | sealed `memfd` (pipe fallback) inherited by the child, fd number in `AGENT_COLAB_SECRET_FD` | child terminated (SIGTERM, then SIGKILL after 1 s), memfd contents zeroed, fd closed |
| `env` | child spawned with the value in one variable (`--env-name`; base64 with `<NAME>_ENCODING=base64` for non-UTF-8 values) | child terminated; with `--respawn-without` started again without the variable |
| `socket` | Unix domain socket (mode 0600) serving the value **once** to a connecting process of the same uid (`SO_PEERCRED`) | listener closed, socket inode removed |

## Revocation timing

The watcher keeps an SSE stream open; whenever the stream closes or is unavailable it issues one
long-poll (`max_wait_s` = `AGENT_COLAB_SIDECAR_POLL_INTERVAL_S`, ≤ 5 s, default 5) before
reconnecting, so nothing is missed between streams. At shutdown the blocking stream read is
aborted by closing the HTTP client, so the process exits promptly after a revocation. On a revocation or on lease expiry the
value buffer is zeroed and released, every injector is invalidated as above, and the cleanup is
acknowledged to the Broker. The tests measure the wall time from `revoke` to "value zeroed and
child gone" under both push and poll and require it to stay under 5 s.

## What is never logged or written

Log lines carry handle ids, lease ids, pids and outcome codes only. `SafeLogFilter` additionally
redacts token-like strings, `key=value` pairs for secret-ish keys, lengths and long hex digests
in any log line (including third-party ones). Values are never written to disk: the store is a
set of in-memory `bytearray`s that refuses pickling; memfds are memory-backed; socket files are
inodes without content. The Mattermost bot adapter advertises `secret_handles: unsupported` and
never receives handles, so no sidecar is deployed for bots.

## Exit codes

`0` child finished / value served · `3` revoked or expired while in use · `4` denied by the
Broker · `5` Broker unavailable · `6` configuration error.
