#!/usr/bin/env bash
# Development mode: Python with reload, Vite with hot module replacement.
#
#   backend  → http://127.0.0.1:8765
#   frontend → http://127.0.0.1:5173   (open this one; it proxies /api and /ws)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "No virtual environment found. Run ./scripts/setup.sh first."
  exit 1
fi

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

PYTHONPATH=backend ./.venv/bin/python -m uvicorn jarvis.server:create_app \
  --factory --reload --reload-dir backend --host 127.0.0.1 --port 8765 &

(cd frontend && npm run dev) &

wait
