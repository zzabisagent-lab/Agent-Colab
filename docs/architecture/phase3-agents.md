# Phase 3 — Generic Agents: module ownership

Foundation (this commit): migration `0008` (Agent registry runtime columns, heartbeats, rate
windows), placeholder migrations `0009`/`0010`/`0011` owned by the packages below, and the Adapter
contract `server/agents/adapters/contract.py` (§7.3; stable error codes; adapter-type registry with
`AGENT_COLAB_ADAPTER_PLUGINS` for V-P3-12).

| Package(s) | Modules | Migration | Tests |
|---|---|---|---|
| P3-01/02/08 registry, roles preview, limits | `server/agents/registry.py`, `server/agents/heartbeat.py`, `server/agents/limits.py`, `server/application/agents.py`, `server/application/roles.py`, `server/api/v1/agents.py`, `server/api/v1/roles.py` | `0011` | V-P3-01/02/08/09/11/15/16/17 |
| P3-03/10/15 + P3-05 conformance | `server/agents/adapters/{contract,mcp_client}.py`, `server/agents/transport_mcp.py`, `server/agents/conformance/*`, `server/usage/conformance.py` | — | V-P3-05/06/07/21/26 |
| P3-11/12/04 push transports | `server/agents/adapters/{webhook,mattermost_bot}.py`, `server/agents/webhook_delivery.py`, `server/api/v1/work.py`, `server/channels/work_messages.py` | `0009` | V-P3-22/23/12 |
| P3-06/09/13/14 orchestration | `server/agents/routing.py`, `server/tasks/graph.py`, `server/verification/assignment.py`, `server/agents/rerouting.py`, `server/application/orchestration.py` | `0010` | V-P3-03/04/10/14/18/19/20/24/25 |
| P3-07 Agent Admin UI | `web-admin/src/features/agents/*`, `web-admin/src/features/roles/*`, `tests/e2e/test_admin_agents_ui.py` | — | V-P3-13 |

Rules: the command bus stays the only write path; every adapter call goes through the contract;
secret handle values never appear in logs, Events, results or messages; routing ties break by
ascending `agent_id`.
