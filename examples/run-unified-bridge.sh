#!/usr/bin/env bash
# Foreground launcher for unified_bridge.py.
#
# Use this when you want to:
#   - Smoke-test a code change before promoting to systemd
#   - See live stdout/stderr while debugging
#   - Run on a host that has no systemd (a Mac, a container, etc.)
#
# What it does:
#   1. If a systemd unit named unified-discord-bridge.service is running,
#      stops it (so the bot tokens are free).
#   2. Loads your env file.
#   3. Runs unified_bridge.py in the foreground until you Ctrl+C.
#   4. On exit, restarts the systemd unit if it was running before.
#
# Edit ENV_FILE and BRIDGE_DIR to match your install.

set -euo pipefail

ENV_FILE="${UNIFIED_BRIDGE_ENV:-$HOME/.config/unified-discord-bridge/env}"
BRIDGE_DIR="${UNIFIED_BRIDGE_DIR:-$HOME/claude-discord-bridge}"
PROD_SERVICE="${UNIFIED_BRIDGE_SERVICE:-unified-discord-bridge.service}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: env file not found at $ENV_FILE" >&2
  echo "Set UNIFIED_BRIDGE_ENV or copy .env.example into place first." >&2
  exit 1
fi
if [[ ! -x "$BRIDGE_DIR/venv/bin/python" ]]; then
  echo "ERROR: venv Python not found at $BRIDGE_DIR/venv/bin/python" >&2
  echo "Run 'python -m venv venv && ./venv/bin/pip install -r requirements.txt' in $BRIDGE_DIR first." >&2
  exit 1
fi

restart_service=false
if command -v systemctl >/dev/null 2>&1 && systemctl --user is-active --quiet "$PROD_SERVICE" 2>/dev/null; then
  echo "=== stopping $PROD_SERVICE ==="
  systemctl --user stop "$PROD_SERVICE"
  restart_service=true
fi

cleanup() {
  echo
  if $restart_service; then
    echo "=== restarting $PROD_SERVICE ==="
    systemctl --user start "$PROD_SERVICE" || true
  fi
  echo "foreground session ended."
}
trap cleanup EXIT INT TERM

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [[ -n "${BRIDGE_ATTACHMENT_DIR:-}" ]]; then
  mkdir -p "$BRIDGE_ATTACHMENT_DIR"
fi

echo
echo "=== launching unified_bridge.py in foreground ==="
echo "Hit Ctrl+C to stop."
echo

cd "$BRIDGE_DIR"
exec ./venv/bin/python unified_bridge.py
