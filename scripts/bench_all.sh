#!/usr/bin/env bash
# Run every evaluation suite with your own JARVIS configuration, then the
# v3.0 gate report. Extra arguments are passed to each runner, e.g.:
#   scripts/bench_all.sh --set models.general.model=qwen3:8b
set -uo pipefail
cd "$(dirname "$0")/.."

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3
export PYTHONPATH=backend

echo "▸ Understanding corpus"
$PY -m evals.run_understanding --model real --quiet "$@"

echo "▸ Web tasks"
$PY -m evals.run_web --model real "$@"

if [ "$(uname)" = "Darwin" ]; then
  echo "▸ Native Mac tasks"
  $PY -m evals.run_mac "$@"
else
  echo "▸ Native Mac tasks skipped (not on macOS)"
fi

echo "▸ Gate report"
$PY -m evals.report
