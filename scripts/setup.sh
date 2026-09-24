#!/usr/bin/env bash
# One-time setup: Python environment, interface build, and a look at the models.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

say() { printf "\033[38;5;39m▸\033[0m %s\n" "$1"; }
warn() { printf "\033[38;5;214m▸\033[0m %s\n" "$1"; }

say "JARVIS setup — $ROOT"

# --- Python ------------------------------------------------------------------
# JARVIS needs Python 3.10+. The python3 that ships with macOS is 3.9, so look
# for a newer one (Homebrew, python.org) before giving up.
python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }
python_version() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "none"; }

if [ -n "${PYTHON:-}" ]; then
  CANDIDATES="$PYTHON"
else
  CANDIDATES="python3.12 python3.11 python3.13 python3.10 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3"
fi
PYTHON=""
for candidate in $CANDIDATES; do
  if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  warn "JARVIS needs Python 3.10 or newer; this Mac's python3 is $(python_version python3)."
  warn "Install one with: brew install python@3.12  (or from python.org), then run this again."
  exit 1
fi
say "Using Python $(python_version "$PYTHON") ($(command -v "$PYTHON"))"

# An environment built on an older Python can't run this version: rebuild it.
if [ -x .venv/bin/python ] && ! python_ok .venv/bin/python; then
  say "Rebuilding the virtual environment (it was Python $(python_version .venv/bin/python))"
  rm -rf .venv
fi

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
  if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
    say "Installing speech recognition for the Apple Silicon GPU (Whisper large-v3-turbo)"
    ./.venv/bin/pip install --quiet -e ".[mlx]" || \
      warn "mlx-whisper failed to install — speech recognition will run on the CPU instead."
  fi
fi

if [ "$(uname -s)" = "Darwin" ]; then
  say "Installing Mac app control (Accessibility, genuine input, on-screen text)"
  ./.venv/bin/pip install --quiet -e ".[native]" || \
    warn "Native extras failed to install — app control will use AppleScript only."
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
    say "Recommended for JARVIS:  ./scripts/pull-models.sh --vision"
    say "                         (qwen3:8b for everything, qwen2.5vl:7b to see the screen)"
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
