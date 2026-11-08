#!/usr/bin/env bash
# Run from your Mac:  deploy/rollback.sh   -> switches the server back to the previous release.
set -euo pipefail
# Target server comes from the environment or an untracked deploy/local.env (see deploy/local.env.example).
[ -f "$(dirname "$0")/local.env" ] && . "$(dirname "$0")/local.env"
: "${WATCHER_HOST:?Set WATCHER_HOST=user@server (or create deploy/local.env, see local.env.example)}"
KEY="${WATCHER_KEY:-$HOME/.ssh/id_ed25519}"
HOST="$WATCHER_HOST"
ssh -i "$KEY" -o BatchMode=yes "$HOST" 'set -e; B=$HOME/pi-watcher; P=$(cat $B/previous); [ -d "$P" ] || { echo "no previous release"; exit 1; }
  C=$(readlink -f $B/current); ln -sfn "$P" $B/current.new && mv -T $B/current.new $B/current; echo "$C" > $B/previous
  systemctl --user restart infra-watcher.service; sleep 3; curl -fsS http://127.0.0.1:8200/healthz; echo; echo "rolled back to $P"'
