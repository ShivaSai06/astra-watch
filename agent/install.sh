#!/usr/bin/env bash
# Installs the Mac agent for your user only. No sudo. Does NOT start it and does NOT enable auto-start.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/infra-watcher"
CONF="$HOME/.config/infra-watcher"
BIN="$HOME/.local/bin"
SERVICE="infra-watcher-agent"

mkdir -p "$APP" "$CONF" "$BIN" "$HOME/.local/state/infra-watcher"
chmod 700 "$CONF"
install -m 755 "$SRC/mac_agent.py" "$APP/mac_agent.py"
install -m 755 "$SRC/watcher-agent" "$BIN/watcher-agent"

read -r -p "Dashboard URL (e.g. https://monitor.example.com): " URL
case "$URL" in https://*) ;; *) echo "The URL must start with https://"; exit 1;; esac

if security find-generic-password -s "$SERVICE" >/dev/null 2>&1; then
  read -r -p "A token is already stored in your Keychain. Replace it? [y/N] " R
  [ "${R:-n}" = "y" ] || { echo "Keeping the existing token."; TOKEN=""; }
fi
if [ "${TOKEN-unset}" = "unset" ]; then
  read -r -s -p "Paste the agent token from 'watcherctl pair-agent' (hidden): " TOKEN; echo
  [ -n "$TOKEN" ] || { echo "Empty token."; exit 1; }
  security add-generic-password -a "$USER" -s "$SERVICE" -w "$TOKEN" -U
  unset TOKEN
fi

cat > "$CONF/agent.json" <<EOF
{ "url": "$URL", "interval": 30, "scan_projects": "~/Workspace" }
EOF
chmod 600 "$CONF/agent.json"

echo
echo "Installed. The token is in your macOS Keychain (service: $SERVICE)."
echo "  Start now:            watcher-agent start      (make sure $BIN is on your PATH)"
echo "  Stop:                 watcher-agent stop"
echo "  Status / logs:        watcher-agent status | watcher-agent logs"
echo "  Start at login:       watcher-agent enable     (optional; you decide)"
echo "  Remove everything:    watcher-agent uninstall"
case ":$PATH:" in *":$BIN:"*) ;; *) echo; echo "Note: add  export PATH=\"$BIN:\$PATH\"  to your ~/.zshrc";; esac
