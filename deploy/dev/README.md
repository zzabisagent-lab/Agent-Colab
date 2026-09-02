# Development stack

`make compose-up` (= `docker compose --env-file deploy/dev/compose.env up -d --build --wait`) starts PostgreSQL 16, the Agent-Colab server, the web admin
(nginx serving the Vite build and proxying `/api` and `/setup`), and ClamAV. Only the server
(127.0.0.1:8080) and web admin (127.0.0.1:8081) are published, on loopback. PostgreSQL and
ClamAV are reachable from the Compose network only.

Health: `docker compose --env-file deploy/dev/compose.env ps` must show every service `healthy`.
Reset: `make compose-down`. Compose never reads the repository `.env` (deployment secrets).

The images are pinned by tag in `compose.yaml` and the Dockerfiles; Phase 7 replaces tags with
digests and signatures.
