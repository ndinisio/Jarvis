#!/usr/bin/env bash
# Pull a sensible set of local models for JARVIS.
#
#   ./scripts/pull-models.sh           fast + general  (~6 GB)
#   ./scripts/pull-models.sh --vision  also the vision model (~4.5 GB more)
#   ./scripts/pull-models.sh --small   a lighter pair for 8 GB machines
set -euo pipefail

if ! command -v ollama >/dev/null; then
  echo "Ollama is not installed. Get it from https://ollama.com"
  exit 1
fi

FAST="llama3.2:1b"
GENERAL="llama3.1:8b"
VISION=""

for arg in "$@"; do
  case "$arg" in
    --vision) VISION="llava:7b" ;;
    --small) FAST="qwen2.5:1.5b"; GENERAL="llama3.2:3b" ;;
  esac
done

for model in "$FAST" "$GENERAL" $VISION; do
  echo "▸ Pulling $model"
  ollama pull "$model"
done

echo "▸ Done. Point JARVIS at them in Settings, or:"
echo "    JARVIS_FAST_MODEL=$FAST JARVIS_GENERAL_MODEL=$GENERAL ./scripts/start.sh"
