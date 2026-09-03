# Phase 4 — Admin, Setup, Secrets: module ownership

Foundation (this commit): `server/secrets/provider.py` (§9.1 provider contract, stable
value-free error codes, provider registry), `server/security/reauth.py` (re-authentication seam:
`require_recent_mfa` fails closed until the MFA package installs a verifier), placeholder
migrations `0012`–`0015` owned by the packages below.

| Package(s) | Modules | Migration | Tests |
|---|---|---|---|
| P4-05/06/07 secrets, broker, injection | `server/secrets/{local_provider,broker,leases,ledger,injection}.py`, `server/api/v1/secrets.py`, `server/application/secrets.py` | `0012` | V-P4-10/11/12/13/14/15/17 |
| P4-12 secret sidecar | `sidecar/` package + OCI Dockerfile, `server/api/v1/secrets_sidecar.py` (resolve/revoke push) | — | V-P4-31 |
| P4-03/04/13 setup, settings, maintenance | `server/setup/{wizard,preflight,persist}.py`, `server/api/setup.py`, `server/settings/*`, `server/api/v1/settings.py`, `server/maintenance/*` | `0013` | V-P4-01/02/03/04/05/06/19/24/27/28/30/32 |
| P4-08/09/10/14 MFA, admin security, break-glass, approvals queue | `server/security/{mfa,csrf,ratelimit,breakglass}.py`, `server/api/v1/{mfa,breakglass,approvals_queue}.py` | `0014` | V-P4-02/08/09/20/21/33 |
| P4-01/02/11 account admin, dashboard, hard delete | `server/application/accounts.py`, `server/api/v1/{accounts,ops,audit}.py`, `server/ops/*`, `server/application/hard_delete.py` | `0015` | V-P4-07/16/22/23/25/26/29 |
| Console screens + accessibility (parent) | `web-admin/src/features/{setup,accounts,overview,secrets,settings,audit,approvals,maintenance}/*`, axe run, `tests/e2e/test_admin_phase4_ui.py` | — | V-P4-08 (UI half), V-P4-18 |

Rules: secret values never leave the Broker/sidecar boundary (no logs, Events, errors, lengths,
hashes); every critical action goes through `require_recent_mfa`; the command bus stays the only
write path; UI actions call the same REST endpoints as API clients.
