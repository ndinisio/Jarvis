#!/usr/bin/env bash
# One-time setup: Python environment, interface build, and a look at the models.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

say() { printf "\033[38;5;39m▸\033[0m %s\n" "$1"; }
warn() { printf "\033[38;5;214m▸\033[0m %s\n" "$1"; }

say "JARVIS setup — $ROOT"

# --- Python ------------------------------------------------------------------
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null; then
  warn "python3 was not found. Install it from python.org or with: brew install python@3.12"
  exit 1
fi

VERSION=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
say "Using Python $VERSION"

if [ ! -d .venv ]; then
  say "Creating the virtual environment"
  "$PYTHON" -m venv .venv
fi

say "Installing Python dependencies"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -e .

if [ "${WITH_VOICE:-1}" = "1" ]; then
  say "Installing local voice support (Whisper + wake word)"
  ./.venv/bin/pip install --quiet -e ".[voice]" || \
    warn "Voice extras failed to install — JARVIS will run without local speech input."
fi

if [ "${WITH_BROWSER:-1}" = "1" ]; then
  say "Installing JARVIS's own browser support (Playwright)"
  if ./.venv/bin/pip install --quiet -e ".[browser]"; then
    # JARVIS Chrome uses your installed Google Chrome; without it, Playwright's
    # own Chromium is downloaded instead.
    if [ ! -d "/Applications/Google Chrome.app" ]; then
      ./.venv/bin/python -m playwright install chromium || \
        warn "Chromium didn't download — web errands will use your everyday browser."
    fi
  else
    warn "Browser extras failed to install — web errands will use your everyday browser."
  fi
fi

./.venv/bin/pip install --quiet -e ".[dev]" || true

# --- Interface ---------------------------------------------------------------
if command -v npm >/dev/null; then
  say "Building the interface"
  (cd frontend && npm install --no-audit --no-fund --silent && npm run build --silent)
else
  warn "npm was not found — install Node 18+ to build the interface (brew install node)."
fi

# --- Models ------------------------------------------------------------------
if command -v ollama >/dev/null; then
  if curl -sf http://127.0.0.1:11434/api/tags >/dev/null; then
    say "Ollama is running. Installed models:"
    ollama list | sed 's/^/    /'
    printf "\n"
    say "Recommended for JARVIS:  ollama pull llama3.2:1b   (fast)"
    say "                         ollama pull llama3.1:8b   (general)"
    say "                         ollama pull llava:7b      (vision)"
  else
    warn "Ollama is installed but not running. Start it with: ollama serve"
  fi
else
  warn "Ollama was not found. Install it from https://ollama.com — JARVIS uses it for"
  warn "conversation and reasoning. Commands and system queries work without it."
fi

printf "\n"
say "Setup complete. Start JARVIS with:  ./scripts/start.sh"
say "Check the installation with:        ./.venv/bin/jarvis doctor"
