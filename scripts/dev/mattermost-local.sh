#!/usr/bin/env bash
# Local Mattermost Team Edition for spikes and integration tests (P0-10, Phase 2).
# Usage: scripts/dev/mattermost-local.sh start|stop|status|configure
# Requires: ~/.local/opt/mattermost (Team Edition tarball extracted) and the user-space
# PostgreSQL helper `pg16` (127.0.0.1:54329, user colab, database `mattermost`).
set -euo pipefail
MM_HOME="${MM_HOME:-$HOME/.local/opt/mattermost}"
MM_BIN="$MM_HOME/bin/mattermost"
MM_CONFIG="$MM_HOME/config/config.json"
MM_LOG_DIR="$MM_HOME/logs"
MM_PID="$MM_HOME/mattermost.pid"
MM_DSN="${MM_DSN:-postgres://colab@127.0.0.1:54329/mattermost?sslmode=disable&connect_timeout=10}"
MM_SITE_URL="${MM_SITE_URL:-http://127.0.0.1:8065}"

configure() {
  mkdir -p "$MM_LOG_DIR" "$MM_HOME/data"
  python3 - "$MM_CONFIG" "$MM_DSN" "$MM_SITE_URL" "$MM_HOME" <<'PY'
import json, sys
path, dsn, site, home = sys.argv[1:5]
cfg = json.load(open(path))
svc = cfg["ServiceSettings"]
svc.update({
    "SiteURL": site, "ListenAddress": ":8065", "EnableLocalMode": True,
    "LocalModeSocketLocation": home + "/mattermost_local.socket",
    "EnableCommands": True, "EnablePostUsernameOverride": True, "EnablePostIconOverride": True,
    "EnableBotAccountCreation": True, "EnableIncomingWebhooks": True, "EnableOutgoingWebhooks": False,
    "EnableDeveloper": False, "EnableTesting": False, "AllowedUntrustedInternalConnections": "127.0.0.1 localhost",
    "EnableUserAccessTokens": True,
})
cfg["SqlSettings"]["DriverName"] = "postgres"
cfg["SqlSettings"]["DataSource"] = dsn
cfg["TeamSettings"]["EnableOpenServer"] = True
cfg["TeamSettings"]["EnableUserCreation"] = True
cfg["FileSettings"]["Directory"] = home + "/data/"
cfg["LogSettings"]["FileLocation"] = home + "/logs"
cfg["LogSettings"]["EnableConsole"] = False
cfg["LogSettings"]["EnableFile"] = True
cfg["LogSettings"]["FileLevel"] = "INFO"
cfg["EmailSettings"]["SendEmailNotifications"] = False
cfg["EmailSettings"]["RequireEmailVerification"] = False
cfg["PluginSettings"]["Enable"] = False
cfg["PluginSettings"]["EnableUploads"] = False
cfg["MetricsSettings"]["Enable"] = False
cfg["LogSettings"]["EnableDiagnostics"] = False
json.dump(cfg, open(path, "w"), indent=2)
print("configured", path)
PY
}

start() {
  if status >/dev/null 2>&1; then echo "mattermost already running"; return 0; fi
  configure
  cd "$MM_HOME"
  nohup "$MM_BIN" server --config "$MM_CONFIG" >"$MM_LOG_DIR/server.out" 2>&1 &
  echo $! >"$MM_PID"
  for _ in $(seq 1 60); do
    if curl -sf "$MM_SITE_URL/api/v4/system/ping" >/dev/null 2>&1; then echo "mattermost up at $MM_SITE_URL"; return 0; fi
    sleep 1
  done
  echo "mattermost did not become ready; see $MM_LOG_DIR" >&2; return 1
}

stop() {
  if [ -f "$MM_PID" ] && kill -0 "$(cat "$MM_PID")" 2>/dev/null; then
    kill "$(cat "$MM_PID")"; sleep 2; rm -f "$MM_PID"; echo "mattermost stopped"
  else
    pkill -f "$MM_BIN server" 2>/dev/null && echo "mattermost stopped" || echo "mattermost not running"
    rm -f "$MM_PID"
  fi
}

status() {
  if curl -sf "$MM_SITE_URL/api/v4/system/ping" >/dev/null 2>&1; then echo "mattermost running at $MM_SITE_URL"; return 0; fi
  echo "mattermost not running"; return 1
}

case "${1:-}" in
  start) start;; stop) stop;; status) status;; configure) configure;;
  *) echo "usage: $0 start|stop|status|configure"; exit 2;;
esac
