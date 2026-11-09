# Screenshots

Every image here is a real render of the dashboard, taken in headless Chrome at 1440 px wide against `scripts/demo_server.py`.

The demo server uses **synthetic data only**: it creates a throw-away database in a temporary directory, generates fictional machines (`MacBook Air`, `Hetzner server`, containers named `demo-*`), uses a fake login (`demo@example.com`), stubs the Telegram call, and runs no collection loops. It also ignores `WATCHER_*`, `TELEGRAM_*` and similar environment variables and asserts that its database and secrets live in its temp directory, so a real installation cannot leak in. It never reads production data or contacts any real machine.

Regenerate (after `npm run build` in `frontend/`):

```bash
.venv/bin/python scripts/demo_server.py --port 8300 &
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --hide-scrollbars \
  --window-size=1440,1250 --virtual-time-budget=6000 \
  --screenshot=docs/assets/screenshots/01-overview.png "http://127.0.0.1:8300/#/"
```

Use `--mac stale` for `06-stale-machine.png`. The dates shown are the time of rendering.
