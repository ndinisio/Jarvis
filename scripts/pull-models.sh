#!/usr/bin/env bash
# Pull a sensible set of local models for JARVIS.
#
#   ./scripts/pull-models.sh           the general model   (~5 GB)
#   ./scripts/pull-models.sh --vision  also the vision model (~6 GB more)
#   ./scripts/pull-models.sh --small   a lighter model, for Macs that want headroom
#
# On a 16 GB Mac one text model serves every slot (fast, reasoning and
# operator defer to general), so nothing takes turns being loaded.
# `python -m evals.bake_off --pull` compares candidates on your own Mac.
set -euo pipefail

if ! command -v ollama >/dev/null; then
  echo "Ollama is not installed. Get it from https://ollama.com"
  exit 1
fi

GENERAL="qwen3:8b"
VISION=""

for arg in "$@"; do
  case "$arg" in
    --vision) VISION="qwen3-vl:8b" ;;
    --small) GENERAL="qwen3:4b" ;;
  esac
done

for model in "$GENERAL" $VISION; do
  echo "▸ Pulling $model"
  ollama pull "$model"
done

echo "▸ Done. Point JARVIS at them in Settings, or:"
echo "    JARVIS_GENERAL_MODEL=$GENERAL ./scripts/start.sh"
