#!/usr/bin/env bash
# Run the whole test suite: Python tests, then the interface typecheck and build.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "▸ Python tests"
PYTHONPATH=backend ./.venv/bin/python -m pytest tests/ "$@"

if command -v npm >/dev/null && [ -d frontend/node_modules ]; then
  echo "▸ Interface typecheck"
  (cd frontend && npx tsc --noEmit)
  echo "▸ Interface build"
  (cd frontend && npm run build --silent)
fi
