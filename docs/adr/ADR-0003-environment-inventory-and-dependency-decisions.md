# ADR-0003: Environment inventory and dependency decisions (development plan §25)

- Status: Accepted (Phase 0); open items tracked in `PROGRESS.md`
- Date: 2026-09-02

## Inventory of the build/verification host

| Item | State |
|---|---|
| OS / CPU / RAM / disk | Ubuntu 24.04.4, 24 cores, 125 GB RAM, 93 GB free |
| Python / Node | 3.12.3 / 22.23 (uv 0.12, pnpm 11.25) |
| Container runtime | **absent** (no Docker, no root; rootless Docker blocked by missing `uidmap` and AppArmor userns restriction) |
| PostgreSQL 16 | user-space instance (apt package extracted, no root), `127.0.0.1:54329`, used for `db`-marked tests |
| Mattermost | Team Edition 11.10.1 binary available locally for the P0-10 spike and Phase 2 test team |
| Telegram bot / test chats | **absent** from `.env` and environment |
| Codex CLI (Verifier) | 0.152.0, authenticated, model `GPT-5.6-Codex`, run via `codex exec` |
| GitHub | SSH push access as the implementer account, repository `zzabisagent-lab/Agent-Colab` |
| Deployment target | `.env` provides server name, IP, login id and password (SSH); no container runtime inventory yet |

## Decisions per §25 row

| Dependency | Decision |
|---|---|
| Deployment host/container runtime/inventory | Target host from `.env` (SSH). Container runtime on the target is confirmed during Phase 7 install; Compose is the deployment method (§23). |
| Git repo and protected branch permissions | Confirmed; phase branches `phase-<n>`, merge to `main` only after Verifier PASS, tags `phase-<n>-passed`. |
| Load profile and RPO/RTO | §21.1 defaults adopted unchanged (ADR-0004). |
| Initial pricing.yaml rate table | Implementer-provided default table (`policy/pricing.yaml`, version `pricing-v1`) with a default rate for unknown models; editable in Admin Settings (Phase 4). Owner may revise; recorded as residual item RI-001. |
| Mattermost administrator and test team | Self-provisioned: a local Mattermost Team Edition instance with a test team, bot account, slash command and two channels is created by the implementer for spikes and Phase 2 verification. |
| Slash command / override_username permission | Determined by the P0-10 spike against the local instance; result recorded in `docs/protocol/mattermost-spike.md`. |
| Telegram bot and test chat/topic | **Cannot be created by the implementer.** Blocker raised to the user for P0-13 and Phase 2 (bot token + two chats/topics). |
| Test users and Account link approver | Local Mattermost test users; approver is the test Administrator account. |
| SMTP (optional) | Not configured; mail notifications remain optional and unverified unless provided. |
| Distinct verification Agent identity | Codex (`agent-codex`) with its own ChatGPT credential (ADR-0005). |
| 3 Agent Adapter test endpoints | Implemented in-repo as test doubles per adapter type (Phase 3), not product names. |
| System Owner / OIDC / MFA | TOTP MFA mandatory for Owner/Administrator; OIDC adapter interface only (spec §6.1). |
| Secret master key custody/provider | Encrypted local provider mandatory; master key file with owner-only permission outside DB/backups (Phase 4). |
| Sidecar execution host | The build host (separate process) for V-P4-31. |
| KMS/key tombstone custody | Append-only signed tombstone ledger on a separate path/Git record (Phase 4/7). |
| IANA tzdb / cron fixtures | `tzdata` wheel pinned; fixtures under `tests/fixtures/schedule`. |
| Scheduler multi-instance staging | Two runner processes against one DB on the build host (Phase 5). |
| NAS/object/Git publisher destination | Local filesystem path + local bare Git repository (Phase 6). |
| Agent with brainstorm.summarize/document.narrate | Test-double Agent in Phase 6; skeleton-only acceptance where unavailable. |
| ClamAV image | Part of `compose.yaml`; requires the container runtime (see blocker). |
| DNS/TLS/firewall | Deployment-target decision at Phase 7 install; Setup stays loopback-bound. |
| Backup destination/recovery operator | Local path on the build host for rehearsals; production path chosen at deployment. |

## Blockers raised to the user

1. **Container runtime**: Docker Engine + Compose plugin (root install) on the build host — needed
   for V-P0-04 (Compose health), ClamAV, and later Compose-based tests.
2. **Telegram**: a bot token and two test chats/topics — needed for P0-13/V-P0-19 and Phase 2.

All other Phase 0 work proceeds; the two Tests above stay `NOT_RUN` until the inputs exist.
