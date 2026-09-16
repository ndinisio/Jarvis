# Configuration

The file JARVIS actually reads is **`~/JARVIS/config/config.json`**, created on
first run. `config.example.json` here is a complete dump of the defaults, kept
for reference and diffing — editing it changes nothing.

Precedence, highest first:

1. environment variables (`JARVIS_*`, including a `.env` file) — see `../.env.example`
2. `~/JARVIS/config/config.json`
3. the defaults in `backend/jarvis/core/config.py`

Edit the live configuration from the Settings sheet in the interface, or by
hand; changes are applied without a restart. API keys are read from the
environment only and are never written to disk.
