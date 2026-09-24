#!/usr/bin/env bash
# Development mode: Python with reload, Vite with hot module replacement.
#
#   backend  → http://127.0.0.1:8765
#   frontend → http://127.0.0.1:5173   (open the URL printed below; it proxies /api and /ws)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "No virtual environment found. Run ./scripts/setup.sh first."
  exit 1
fi

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# One session token for this dev run (see backend/jarvis/core/auth.py): the
# backend reloads on every change, and a fixed token keeps the open tab working.
JARVIS_SESSION_TOKEN="$(./.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export JARVIS_SESSION_TOKEN

PYTHONPATH=backend ./.venv/bin/python -m uvicorn jarvis.server:create_app \
  --factory --reload --reload-dir backend --host 127.0.0.1 --port 8765 &

(cd frontend && npm run dev) &

printf "\n  Open: http://127.0.0.1:5173/?token=%s\n\n" "$JARVIS_SESSION_TOKEN"

wait
