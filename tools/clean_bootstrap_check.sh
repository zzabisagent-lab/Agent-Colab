#!/usr/bin/env bash
# V-P0-03: clone the repository fresh and run the documented bootstrap/lint/test/build procedure.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
REF="${1:-HEAD}"
SRC="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/agent-colab-clean-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
echo "clean checkout of $REF into $TMP"
git clone -q --no-local "$SRC" "$TMP/repo"
git -C "$TMP/repo" checkout -q "$(git -C "$SRC" rev-parse "$REF")"
cd "$TMP/repo"
export AGENT_COLAB_TEST_DATABASE_URL="${AGENT_COLAB_TEST_DATABASE_URL:-postgresql://colab@127.0.0.1:54329/colab_test}"
time make bootstrap
time make lint
time make test
time make check-docs
time make build
echo "CLEAN_BOOTSTRAP_OK $(git rev-parse HEAD)"
