#!/usr/bin/env bash
set -euo pipefail
cd /Users/jackkim/Projects/discord-agent-bridge
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export INFISICAL_API_URL="http://100.89.64.26:8080"
export PYTHONUNBUFFERED=1

PROJECT_ID="1fe5725b-5e3e-4dff-b648-bbc8e1ea09ea"
SERVICE_ID="claude-discord-bridge-infisical-client-id"
SERVICE_SECRET="claude-discord-bridge-infisical-client-secret"
ACCOUNT="jackkim"

INFISICAL_CLIENT_ID="$(security find-generic-password -a "$ACCOUNT" -s "$SERVICE_ID" -w 2>/dev/null || true)"
INFISICAL_CLIENT_SECRET="$(security find-generic-password -a "$ACCOUNT" -s "$SERVICE_SECRET" -w 2>/dev/null || true)"

if [[ -z "$INFISICAL_CLIENT_ID" || -z "$INFISICAL_CLIENT_SECRET" ]]; then
  echo "Missing Infisical machine identity in Keychain. Run store-machine-auth-keychain once." >&2
  exit 78
fi

export INFISICAL_TOKEN="$($HOME/.local/bin/infisical login \
  --method=universal-auth \
  --client-id="$INFISICAL_CLIENT_ID" \
  --client-secret="$INFISICAL_CLIENT_SECRET" \
  --silent \
  --plain)"

ROOT_JSON="$(mktemp)"
CODEX_JSON="$(mktemp)"
cleanup() { rm -f "$ROOT_JSON" "$CODEX_JSON"; }
trap cleanup EXIT

"$HOME/.local/bin/infisical" export --silent --env dev --projectId "$PROJECT_ID" --path / --format=json --output-file="$ROOT_JSON"
"$HOME/.local/bin/infisical" export --silent --env dev --projectId "$PROJECT_ID" --path /codex --format=json --output-file="$CODEX_JSON"

eval "$(/usr/bin/python3 - "$ROOT_JSON" "$CODEX_JSON" <<'PY'
import json
import shlex
import sys
from pathlib import Path


def load(path):
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):
        out = {}
        for item in data:
            if isinstance(item, dict) and "key" in item:
                out[str(item["key"])] = str(item.get("value", ""))
        return out
    return {}

root = load(sys.argv[1])
codex = load(sys.argv[2])

def first(*vals):
    for val in vals:
        if val:
            return val
    return ""

exports = {
    "BRIDGE_AGENTS": "claude_agent,codex_agent",
    "BRIDGE_ATTACHMENT_DIR": "/Users/jackkim/.discord-agent-bridge/attachments",
    "CLAUDE_AGENT_TOKEN": first(root.get("CLAUDE_AGENT_TOKEN"), root.get("BRIDGE_DISCORD_BOT_TOKEN")),
    "CLAUDE_AGENT_BACKEND": "claude",
    "CLAUDE_AGENT_ALLOWED_USER_ID": first(root.get("CLAUDE_AGENT_ALLOWED_USER_ID"), root.get("BRIDGE_ALLOWED_USER_ID")),
    "CLAUDE_AGENT_WORKDIR": first(root.get("CLAUDE_AGENT_WORKDIR"), root.get("BRIDGE_WORKDIR"), "/Users/jackkim/Projects"),
    "CLAUDE_AGENT_CLAUDE_BIN": first(root.get("CLAUDE_AGENT_CLAUDE_BIN"), root.get("BRIDGE_CLAUDE_BIN"), "/Users/jackkim/.local/bin/claude"),
    "CLAUDE_AGENT_DEFAULT_MODEL": first(root.get("CLAUDE_AGENT_DEFAULT_MODEL"), root.get("BRIDGE_DEFAULT_MODEL")),
    "CLAUDE_AGENT_DEFAULT_EFFORT": first(root.get("CLAUDE_AGENT_DEFAULT_EFFORT"), root.get("BRIDGE_DEFAULT_EFFORT")),
    "CLAUDE_AGENT_CLAUDE_PERMISSION_MODE": first(root.get("CLAUDE_AGENT_CLAUDE_PERMISSION_MODE"), root.get("BRIDGE_CLAUDE_PERMISSION_MODE"), "bypassPermissions"),
    "CODEX_AGENT_TOKEN": first(codex.get("CODEX_AGENT_TOKEN"), codex.get("BRIDGE_DISCORD_BOT_TOKEN")),
    "CODEX_AGENT_BACKEND": "codex",
    "CODEX_AGENT_ALLOWED_USER_ID": first(codex.get("CODEX_AGENT_ALLOWED_USER_ID"), codex.get("BRIDGE_ALLOWED_USER_ID"), root.get("BRIDGE_ALLOWED_USER_ID")),
    "CODEX_AGENT_WORKDIR": first(codex.get("CODEX_AGENT_WORKDIR"), codex.get("BRIDGE_WORKDIR"), "/Users/jackkim/Projects"),
    "CODEX_AGENT_CODEX_BIN": first(codex.get("CODEX_AGENT_CODEX_BIN"), codex.get("BRIDGE_CODEX_BIN"), "/Applications/Codex.app/Contents/Resources/codex"),
    "CODEX_AGENT_DEFAULT_MODEL": first(codex.get("CODEX_AGENT_DEFAULT_MODEL"), codex.get("BRIDGE_DEFAULT_MODEL")),
    "CODEX_AGENT_DEFAULT_EFFORT": first(codex.get("CODEX_AGENT_DEFAULT_EFFORT"), codex.get("BRIDGE_DEFAULT_EFFORT")),
    "CODEX_AGENT_DEFAULT_SANDBOX": first(codex.get("CODEX_AGENT_DEFAULT_SANDBOX"), codex.get("BRIDGE_DEFAULT_SANDBOX"), "workspace-write"),
    "CODEX_AGENT_DEFAULT_SEARCH": first(codex.get("CODEX_AGENT_DEFAULT_SEARCH"), codex.get("BRIDGE_DEFAULT_SEARCH"), "off"),
}
for key, value in exports.items():
    if value:
        print(f"export {key}={shlex.quote(value)}")
PY
)"

exec ./venv/bin/python unified_bridge.py
