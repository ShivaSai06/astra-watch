#!/usr/bin/env bash
# Run from your Mac:  deploy/deploy.sh   -> builds, uploads, activates (auto-rollback on failed health check).
# Touches ONLY ~/pi-watcher and the infra-watcher user unit on the server.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Target server comes from the environment or an untracked deploy/local.env (see deploy/local.env.example).
[ -f "$(dirname "$0")/local.env" ] && . "$(dirname "$0")/local.env"
: "${WATCHER_HOST:?Set WATCHER_HOST=user@server (or create deploy/local.env, see local.env.example)}"
KEY="${WATCHER_KEY:-$HOME/.ssh/id_ed25519}"
HOST="$WATCHER_HOST"
SSH=(ssh -i "$KEY" -o BatchMode=yes); STAMP="$(date +%Y%m%d-%H%M%S)"
STAGE="$(mktemp -d)/$STAMP"; mkdir -p "$STAGE"/{backend,static,vendor,deploy}

echo "==> tests";   (cd "$ROOT/backend" && "$ROOT/.venv/bin/python" -m pytest -q tests --deselect tests/test_mac_agent.py 2>&1 | tail -2)
echo "==> frontend"; (cd "$ROOT/frontend" && npm run build >/dev/null) && cp -R "$ROOT/frontend/dist/." "$STAGE/static/"
echo "==> backend";  cp -R "$ROOT/backend/watcher" "$STAGE/backend/" && find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
echo "==> vendored dependencies (Linux x86_64, CPython 3.12)"
W="$(mktemp -d)"; "$ROOT/.venv/bin/pip" download -q --only-binary=:all: --python-version 3.12 --implementation cp \
  --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 \
  --abi cp312 --abi abi3 --abi none fastapi uvicorn -d "$W" 2>/dev/null
for f in "$W"/*.whl; do unzip -q -o "$f" -d "$STAGE/vendor"; done
cp "$ROOT"/deploy/{infra-watcher.service,remote_install.sh} "$STAGE/deploy/"
echo "==> upload ($(du -sh "$STAGE" | cut -f1))"
"${SSH[@]}" "$HOST" "mkdir -p ~/pi-watcher/releases"
COPYFILE_DISABLE=1 tar --no-xattrs -C "$STAGE" -cf - . | "${SSH[@]}" "$HOST" "mkdir -p ~/pi-watcher/releases/$STAMP && tar -C ~/pi-watcher/releases/$STAMP -xf -"
echo "==> activate"
"${SSH[@]}" "$HOST" "bash ~/pi-watcher/releases/$STAMP/deploy/remote_install.sh $STAMP"
