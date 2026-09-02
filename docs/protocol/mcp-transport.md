# MCP transport (P3-10; development plan §7B.3, §7.4)

Endpoint: Streamable HTTP at `/mcp` (mounted at the root so the exact path is served).

## Authentication

* Bearer service token bound to the Agent Account (`ServiceTokenVerifier`); an invalid token is
  answered with 401 by the SDK auth middleware before any handler runs (zero side effects).
* mTLS: TLS terminates at the reverse proxy (deployed in Phase 5). The proxy forwards the
  verified client-certificate fingerprint in the header named by `AGENT_COLAB_MTLS_HEADER` and
  its shared secret in `X-Agent-Colab-Proxy-Auth` (`AGENT_COLAB_MTLS_PROXY_SECRET`).
  `MtlsProxyMiddleware` mints a one-time `Bearer mtls:<nonce>` that the verifier resolves against
  `agents.endpoint->>'mtls_fingerprint'`. Without the proxy secret the fingerprint header is
  ignored, so it cannot be forged by a direct client.

## Tools

| Tool | Command / query | Notes |
|---|---|---|
| `work_poll(agent_id, max_items=10, max_wait_s=0)` | `WorkPoll` | long-poll up to 30 s (answer leaves before the client's 30 s budget: 0.5 s safety margin); wakes on inbox change; one concurrent poll per MCP session → `MCP_POLL_IN_PROGRESS` (429); DELIVERED-but-unacked items are returned again (reconnect redelivery) |
| `work_ack`, `work_start`, `work_reject(reason_code)` | `WorkAck`/`WorkStart`/`WorkReject` | reason codes `CAPABILITY_UNSUPPORTED|CAPACITY|POLICY|OTHER` |
| `work_result(work_item_id, result)` | `WorkResult` | exactly once; duplicates → `DUPLICATE_RESULT_IGNORED` (replayed=true) + audit `work.duplicate_result_ignored`; usage recorded (§7C) |
| `task_get`, `document_get` | read queries | workspace scoped; not-found and forbidden are the same 404 |
| `usage_report`, `artifact_register`, `verification_submit`, `verification_evidence_submit` | `ReportUsage`, `RegisterArtifact`, `SubmitVerdict`, `SubmitEvidence` | same handlers as REST |
| core task tools (`task_create` … `approval_request`) | Phase 1/2 | unchanged |

Not yet available (documented gap): `brainstorm_contribute` (Phase 4 brainstorm engine) and the
management tools (`agent_register`, `principal_role_assign`, `channel_configure`,
`bridge_configure`, `secret_grant_create`) which the registry/admin packages expose behind the
separate admin capability.

## Resources

`colab://inbox/{agent_id}` (the caller's own inbox only; open items as §7B.1 envelopes),
`colab://task/{task_id}`, `colab://document/{document_id}`.

Change notifications: an inbox change (new/re-queued item) publishes `ResourceUpdated` on the
server's subscription bus (2026-07-28 `subscriptions/listen` clients) and
`notifications/resources/updated` to sessions that used the legacy `resources/subscribe`
(2025-11-25 clients). Notifications are not replayed; a client that connects later polls. Note for
Python clients: the standalone GET stream that carries server notifications needs the SDK's own
HTTP client type (`httpx2.AsyncClient`), not a plain `httpx.AsyncClient`.

## Redelivery rules (§7B.1)

No ACK within 60 s of DELIVERED → re-queued (at most 3 redeliveries, then EXPIRED) by the timeout
sweep; a disconnected session's un-acked items are returned by the next `work_poll` with the same
`delivery_no`; `work_result` is idempotent per `work_item_id`. Assignment items not accepted
within 120 s follow §7D.3 re-routing (orchestration package).
