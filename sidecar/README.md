# agent-colab-sidecar

Runs on the Agent host. It authenticates to the Agent-Colab Secret Broker with the Agent
Account's service token (`AGENT_COLAB_SIDECAR_TOKEN`) or client certificate
(`AGENT_COLAB_SIDECAR_CLIENT_CERT` / `_CLIENT_KEY`, mTLS terminated at the Broker's proxy),
resolves the one-time secret handle carried by a work item and injects the value into a local
process. Values live only in memory; revocations are applied within 5 seconds.

```
agent-colab-sidecar run --handle sh-… --mode fd     -- /path/to/agent      # fd injection (memfd)
agent-colab-sidecar run --handle sh-… --mode env --env-name API_KEY -- agent   # environment
agent-colab-sidecar run --handle sh-… --mode socket --socket-path /run/agent-colab-sidecar/s.sock
agent-colab-sidecar resolve --handle sh-…            # serve once over the runtime-dir socket
agent-colab-sidecar status                           # redacted configuration (no token)
```

Environment: `AGENT_COLAB_SIDECAR_BROKER_URL` (required), `AGENT_COLAB_SIDECAR_TOKEN` or the
cert pair, `AGENT_COLAB_SIDECAR_RUNTIME_DIR` (defaults to `$XDG_RUNTIME_DIR/agent-colab-sidecar`,
must be tmpfs; holds the owner-only instance-id file and socket inodes only),
`AGENT_COLAB_SIDECAR_POLL_INTERVAL_S` (≤ 5), `AGENT_COLAB_SIDECAR_PREFER_SSE` (default 1).

Exit codes: 0 done, 3 revoked/expired while in use, 4 denied by the Broker, 5 Broker unavailable,
6 configuration error. Details: `docs/protocol/secret-sidecar.md`.

Tests (from the repository root): `uv run pytest sidecar/tests -q`.
