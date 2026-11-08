#!/usr/bin/env bash
# Runs ON THE SERVER. Activates a prepared release, restarts only the watcher unit, auto-rolls back on failure.
set -euo pipefail
BASE="$HOME/pi-watcher"; REL="$BASE/releases/$1"
[ -d "$REL" ] || { echo "release $1 missing"; exit 1; }
mkdir -p "$BASE"/{config,data,bin,backups} "$HOME/.config/systemd/user"
chmod 700 "$BASE/config" "$BASE/data" "$BASE/backups"

# wrappers
cat > "$BASE/bin/watcherctl" <<'W'
#!/usr/bin/env bash
export PYTHONPATH="$HOME/pi-watcher/current/vendor:$HOME/pi-watcher/current/backend" WATCHER_HOME="$HOME/pi-watcher" PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 -m watcher.ctl "$@"
W
chmod 755 "$BASE/bin/watcherctl"

# units (only our own two files are touched)
install -m 644 "$REL/deploy/infra-watcher.service" "$HOME/.config/systemd/user/infra-watcher.service"
install -m 644 "$REL/deploy/infra-cloudflared.service" "$HOME/.config/systemd/user/infra-cloudflared.service"

PREV="$(readlink -e "$BASE/current" 2>/dev/null || true)"
ln -sfn "$REL" "$BASE/current.new" && mv -T "$BASE/current.new" "$BASE/current"
[ -n "$PREV" ] && echo "$PREV" > "$BASE/previous"

systemctl --user daemon-reload
systemctl --user enable infra-watcher.service >/dev/null 2>&1
systemctl --user restart infra-watcher.service

ok=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8200/healthz >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" != 1 ]; then
  echo "Health check FAILED - rolling back"
  journalctl --user -u infra-watcher -n 20 --no-pager || true
  if [ -n "$PREV" ] && [ -d "$PREV" ]; then
    ln -sfn "$PREV" "$BASE/current.new" && mv -T "$BASE/current.new" "$BASE/current"
    systemctl --user restart infra-watcher.service
  fi
  exit 1
fi
echo "Release $1 is live: $(curl -fsS http://127.0.0.1:8200/healthz)"
# keep the 4 newest releases
ls -1dt "$BASE"/releases/* | tail -n +5 | xargs -r rm -rf
