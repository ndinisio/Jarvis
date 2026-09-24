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

if [ ! -f frontend/dist/index.html ]; then
  echo "The interface isn't built yet — building it now."
  (cd frontend && npm install --no-audit --no-fund --silent && npm run build --silent)
elif [ -n "$(find frontend/src frontend/index.html frontend/package.json -newer frontend/dist/index.html 2>/dev/null | head -1)" ]; then
  # After a `git pull` the interface's source is newer than its build: an old
  # build can't talk to a new backend, so rebuild before serving it.
  echo "The interface has changed since it was built — rebuilding it."
  (cd frontend && npm install --no-audit --no-fund --silent && npm run build --silent)
fi

exec ./.venv/bin/jarvis serve "$@"
