"""The session token's rules (core/auth.py) and the one-JARVIS-at-a-time lock (cli.py)."""

from __future__ import annotations

import json

import pytest
from jarvis.cli import _claim_single_instance, main
from jarvis.core import auth


def test_tokens_are_fresh_and_long():
    first, second = auth.generate_token(), auth.generate_token()
    assert first != second
    assert len(first) >= 40


@pytest.mark.parametrize(("given", "ok"), [("right", True), ("wrong", False), ("", False), (None, False),
                                           ("righ", False), ("right ", False)])
def test_only_the_exact_token_matches(given, ok):
    assert auth.tokens_match(given, "right") is ok


@pytest.mark.parametrize(("origin", "ok"), [
    (None, True),                              # not a browser: the token alone decides
    ("", True),
    ("http://127.0.0.1:8765", True),           # the interface this server serves
    ("http://localhost:8765", True),
    ("http://127.0.0.1:5173", True),           # trusted dev server
    ("http://127.0.0.1:3000", False),          # some other app on this Mac
    ("http://localhost", False),               # port 80 isn't this server
    ("https://evil.example", False),
    ("https://127.0.0.1.evil.example:8765", False),
    ("null", False),                           # sandboxed iframes / file:// pages
])
def test_origin_rules(origin, ok):
    assert auth.origin_allowed(origin, host="127.0.0.1", port=8765,
                               trusted=("http://127.0.0.1:5173",)) is ok


def test_a_launcher_can_supply_the_token(monkeypatch):
    supplied = "x" * 43
    monkeypatch.setenv(auth.TOKEN_ENV, supplied)
    assert auth.session_token() == supplied


def test_a_guessable_supplied_token_is_ignored(monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, "hunter2")
    token = auth.session_token()
    assert token != "hunter2" and len(token) >= 40


def test_without_a_launcher_each_run_gets_its_own_token(monkeypatch):
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    assert auth.session_token() != auth.session_token()


# -- one JARVIS at a time ----------------------------------------------------

def test_only_one_instance_holds_the_workspace(tmp_path):
    first, _ = _claim_single_instance(tmp_path)
    assert first is not None
    second, holder = _claim_single_instance(tmp_path)
    assert second is None
    assert holder.isdigit(), "the refusal names the process that holds it"
    first.close()
    # Released as soon as the holder goes — however it goes.
    third, _ = _claim_single_instance(tmp_path)
    assert third is not None
    third.close()


def test_a_second_serve_explains_itself_instead_of_crashing(tmp_path, capsys):
    workspace = tmp_path / "JARVIS"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"workspace": str(workspace)}), encoding="utf-8")
    held, _ = _claim_single_instance(workspace)
    try:
        assert main(["--config", str(config_path), "serve", "--no-browser", "--no-voice"]) == 1
    finally:
        held.close()
    assert "already running" in capsys.readouterr().err


def test_connection_log_lines_never_carry_the_token():
    import logging

    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1,
                               '%s - "WebSocket %s" [accepted]',
                               ("127.0.0.1:5000", "/ws?token=abcDEF123-_x&other=1"), None)
    assert auth.RedactToken().filter(record) is True  # redacted, never dropped
    line = record.getMessage()
    assert "abcDEF123" not in line
    assert '"WebSocket /ws?token=[redacted]&other=1"' in line


def test_redaction_is_installed_once_on_uvicorns_logger():
    import logging

    logger = logging.getLogger("uvicorn.error")
    auth.redact_server_logs()
    auth.redact_server_logs()
    assert sum(isinstance(f, auth.RedactToken) for f in logger.filters) == 1
