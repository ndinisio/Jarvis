#!/usr/bin/env bash
# Start JARVIS: backend, interface and voice.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "No virtual environment found. Run ./scripts/setup.sh first."
  exit 1
fi

# Make the backend importable even when the editable install doesn't register
# its path (seen on some setuptools/Python combinations).
export PYTHONPATH="$PWD/backend${PYTHONPATH:+:$PYTHONPATH}"

if [ ! -d frontend/dist ]; then
  echo "The interface isn't built yet — building it now."
  (cd frontend && npm install --no-audit --no-fund --silent && npm run build --silent)
fi

exec ./.venv/bin/jarvis serve "$@"
