#!/usr/bin/env bash
set -uo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: run-evidence.sh NAME COMMAND [ARG ...]" >&2
  exit 2
fi

name=$1
shift
out_dir=$(cd "$(dirname "$0")" && pwd)
log="$out_dir/${name}.log"

{
  echo "started_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "cwd: $(pwd)"
  echo "commit: $(git rev-parse HEAD)"
  printf 'command:'
  printf ' %q' "$@"
  echo
  echo "environment: $(uname -srm); python $(python3 --version 2>&1 | awk '{print $2}'); node $(node --version 2>/dev/null || echo unavailable); postgres $(pg16 psql --version 2>/dev/null | awk '{print $3}' || echo unavailable)"
  echo "--- output ---"
  "$@"
  rc=$?
  echo "--- result ---"
  echo "exit_code: $rc"
  echo "completed_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  exit "$rc"
} >"$log" 2>&1
