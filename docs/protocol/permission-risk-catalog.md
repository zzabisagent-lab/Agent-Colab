# Permission and risk catalog (P0-12)

Authority: development plan §6.9, §7E, §7.4, §7A.2; spec §4.3, §8.4, §15. Machine-readable
form under `policy/`, validated by `schemas/api/policy/*.v1.schema.json`, loaded by
`server/policy/catalog.py`, and linted by `python -m tools.policy_lint` (V-P0-18).

| File | Content |
|---|---|
| `policy/permissions.yaml` | the permission vocabulary (53 entries; the 33 of §6.9 are `minimum: true`). A Role may hold only listed permissions or `<group>.*` over a listed group. |
| `policy/risk-rules.yaml` | 7 action classes with the §6.9 risk/approval defaults, §7E quorum/decision-path/expiry defaults, and the full action map: 29 MCP tools (`tool:`), 45 `/colab` commands (`command:`), 88 API write actions (`api:`), each with its class and required permission. |
| `policy/default-roles.yaml` | 9 default Roles (System Owner, Administrator, Operator, Human Member, Agent Worker, Agent Verifier, Auditor, Scheduler service, Documentation Agent) with explicit denies where the baseline demands them. |
| `policy/capabilities.yaml` | 32 Capabilities: §7.4 core, specialist (`brainstorm.summarize`, `document.narrate`, `secret.lease`), hidden management and schedule tools. |
| `policy/verification-rules.yaml` | §6.4/§7D.2 independence, specialist verifier classes, revision-only verdicts, assignment scoring, closure gate. |

## Rules enforced by the lint

- Every permission used by roles, capabilities, actions and the policy test fixture is in the vocabulary.
- Every action in the union of §7.4 tools and §7A.2 commands is classified (zero unclassified);
  unknown actions at runtime fall back to `HIGH` with `unclassified=True` so they can be audited.
- Class → risk/approval equals §6.9 exactly; quorum equals §7E and `server.domain.defaults`.
- HIGH and above are Human-only and decided in the web console after MFA re-authentication;
  CRITICAL needs two different Humans; self-approval is `SELF_APPROVAL_FORBIDDEN`.
- A Capability `side_effect` conflict raises the risk (higher risk wins).

## Classification choices worth noting

- Task/Run cancellation and re-routing are `delegation_routing` (MEDIUM, channel policy), not
  `destructive`, so ordinary operators can cancel Runs (V-P5-31) while deletion/revocation stays HIGH.
- `doc.publish`/`api:document_publish` are `external_send` (HIGH, Human 1) because publishing
  leaves the Bridge boundary (spec §14.2 requires review before `PUBLISHED`).
- Secret grant creation within scope is `production_change`; scope expansion and `llm_exposure`
  are `secret_exposure` (CRITICAL, two Humans).
- The Scheduler service role denies every Task write permission (spec §15 rule 15); Runs execute
  with the intersection of the service role and the designated execution principal.
