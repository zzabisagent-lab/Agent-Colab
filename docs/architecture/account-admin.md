# Account administration (P4-01)

`server/application/accounts.py` puts every Account change on the command bus; `server/api/v1/accounts.py`
exposes it under `/api/v1/accounts` (permission `admin.accounts`).

| Endpoint | Command | Event | Audit action |
|---|---|---|---|
| `POST /api/v1/accounts` | `CreateAccount` (human \| service; agents → `/api/v1/agents`) | `ACCOUNT_CREATED` | `account.create` |
| `PATCH /{id}` | `UpdateAccount` | `ACCOUNT_UPDATED` | `account.update` |
| `POST /{id}/suspend` | `SuspendAccount` (`force` overrides open-Task references) | `ACCOUNT_SUSPENDED` | `account.suspend` |
| `POST /{id}/reinstate` | `ReinstateAccount` | `ACCOUNT_REINSTATED` | `account.reinstate` |
| `POST /{id}/deletion-request` | `RequestAccountDeletion` → hard-delete workflow | `ACCOUNT_DELETION_REQUESTED` | `account.deletion_request` |
| `DELETE /{id}` | — | — | 405 `HARD_DELETE_WORKFLOW_REQUIRED` |
| `POST /{id}/credentials`, `.../rotate`, `.../revoke` | `IssueCredential`, `RotateCredential`, `RevokeCredential` | — | `identity.service_token_*` |
| `GET /{id}`, `GET /`, `GET /{id}/roles` | reads | — | — |

Rules: a token is returned exactly once (only its hash is stored; audit carries the fingerprint);
suspension and deletion report live references (`ACCOUNT_HAS_REFERENCES`: open Tasks, channel
memberships, Agent, Roles) and never remove rows; a deletion candidate is suspended immediately;
the common principal model (spec §4.3) is preserved — Roles are assigned through
`/api/v1/roles` for Humans, Agents and services alike and `GET /{id}/roles` shows the effective
permissions computed by the Phase 3 engine. Stable errors: `ACCOUNT_ID_INVALID`,
`ACCOUNT_TYPE_INVALID`, `ACCOUNT_TYPE_AGENT_VIA_REGISTRY`, `ACCOUNT_ALREADY_EXISTS`,
`ACCOUNT_NOT_FOUND`, `ACCOUNT_STATUS_INVALID`, `ACCOUNT_SELF_SUSPEND`, `ACCOUNT_SELF_DELETE`,
`ACCOUNT_HAS_REFERENCES`, `ACCOUNT_DELETION_PENDING`, `ACCOUNT_NO_CHANGES`, `CREDENTIAL_NOT_FOUND`.
