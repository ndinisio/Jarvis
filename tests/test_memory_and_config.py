"""Memory and configuration: local, inspectable, editable."""

from __future__ import annotations

import json

from jarvis.core.config import Config, ConfigStore, load_config
from jarvis.core.context import ContextBuilder


async def test_preferences_round_trip(app):
    await app.memory.set_preference("preferred_name", "sir")
    assert app.memory.get_preference("preferred_name") == "sir"
    assert "preferred_name" in app.memory.preferences()
    assert await app.memory.forget_preference("preferred_name") is True
    assert app.memory.get_preference("preferred_name") is None


async def test_facts_are_deduplicated(app):
    first = await app.memory.remember("The Mac is a MacBook Pro M3")
    second = await app.memory.remember("the mac is a macbook pro m3")
    assert first == second


async def test_relevant_facts_are_retrieved_by_keyword(app):
    await app.memory.remember("The user's Mac is a MacBook Pro with an M3 chip", tags="hardware")
    await app.memory.remember("The user prefers tea to coffee", tags="preferences")
    hits = app.memory.relevant_facts("what mac do I have?")
    assert hits and "MacBook" in hits[0].text
    assert all("tea" not in fact.text for fact in hits)


async def test_forget_removes_matching_facts(app):
    await app.memory.remember("The deploy window is Friday evening")
    removed = await app.memory.forget("deploy window")
    assert removed and "deploy window" in removed[0]
    assert not app.memory.relevant_facts("deploy window")


async def test_conversation_history_is_bounded_in_context(app):
    for index in range(20):
        await app.memory.add_message("user", f"message {index}")
    recent = app.memory.recent_messages(6)
    assert len(recent) == 6
    assert recent[-1]["text"] == "message 19"


async def test_describe_reads_like_speech(app):
    await app.memory.set_preference("preferred_name", "sir")
    await app.memory.remember("The user works in London")
    description = app.memory.describe()
    assert "sir" in description and "London" in description


async def test_context_builder_stays_small(app):
    for index in range(40):
        await app.memory.remember(f"Fact number {index} about widgets")
    builder = ContextBuilder(app.memory, app.config)
    context = builder.build("tell me about widgets")
    assert len(context) < 2500
    assert context.count("Fact number") <= app.config.memory.max_facts_in_context


async def test_context_builder_carries_no_tool_results(app):
    """ContextBuilder used to carry a 'Results just gathered' layer, fed once
    per tool call and never cleared — a task's results could still be
    sitting in it, unrelated turns later. That layer is gone: recent actions
    live in ConversationState (bounded, fed by the registry observer, read
    only by the machinery that resolves references), not duplicated here
    with no lifecycle of its own. See the intelligence-context regression
    tests for the end-to-end version of this (V1.3 F1 fix)."""
    builder = ContextBuilder(app.memory, app.config)
    assert not hasattr(builder, "note_tool_result")
    assert not hasattr(builder, "tool_results")


def test_config_defaults_are_local_first():
    config = Config()
    assert config.models.providers["ollama"].enabled is True
    assert config.models.providers["openai"].enabled is False
    assert config.models.providers["anthropic"].enabled is False
    assert config.research.search_provider == "duckduckgo"
    assert config.security.always_confirm == ["high"]


def test_config_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("JARVIS_FAST_MODEL", "tiny-model:1b")
    monkeypatch.setenv("JARVIS_PORT", "9999")
    config = load_config(tmp_path / "missing.json")
    assert config.models.fast.model == "tiny-model:1b"
    assert config.server.port == 9999
    assert str(config.workspace_path).endswith("ws")


def test_config_persists_without_secrets(tmp_path):
    config = Config(workspace=str(tmp_path / "ws"))
    config.models.providers["openai"].api_key = "sk-secret-value"
    store = ConfigStore(config, tmp_path / "config.json")
    store.save()
    written = json.loads((tmp_path / "config.json").read_text())
    assert written["models"]["providers"]["openai"]["api_key"] == ""


def test_config_persists_without_the_imap_password(tmp_path):
    config = Config(workspace=str(tmp_path / "ws"))
    config.email.password = "hunter2"
    store = ConfigStore(config, tmp_path / "config.json")
    store.save()
    written = json.loads((tmp_path / "config.json").read_text())
    assert written["email"]["password"] == ""


def test_email_env_overrides_load_the_password_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_EMAIL_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("JARVIS_EMAIL_USERNAME", "me@example.com")
    monkeypatch.setenv("JARVIS_EMAIL_PASSWORD", "hunter2")
    config = load_config(tmp_path / "missing.json")
    assert config.email.imap_host == "imap.example.com"
    assert config.email.username == "me@example.com"
    assert config.email.password == "hunter2"


def test_a_numeric_or_boolean_looking_password_is_never_type_coerced(monkeypatch, tmp_path):
    """_env_overlay()'s generic type-coercion path (_coerce) turns "123456"
    into an int and "true" into a bool for ordinary settings — a password
    that happens to look numeric or boolean-ish must stay a literal string,
    or the wrong credential is sent to the server."""
    monkeypatch.setenv("JARVIS_EMAIL_PASSWORD", "123456")
    config = load_config(tmp_path / "missing.json")
    assert config.email.password == "123456"

    monkeypatch.setenv("JARVIS_EMAIL_USERNAME", "true")
    config = load_config(tmp_path / "missing.json")
    assert config.email.username == "true"


def test_config_update_is_deep_merged(config_store):
    config_store.update({"voice": {"wake_word": "computer"}})
    config = config_store.current
    assert config.voice.wake_word == "computer"
    # Untouched siblings survive the merge.
    assert config.voice.tts_engine in {"off", "macos", "browser"}
    assert config.models.fast.model


def test_config_change_listeners_fire(config_store):
    seen = []
    config_store.on_change(lambda c: seen.append(c.voice.wake_word))
    config_store.update({"voice": {"wake_word": "friday"}})
    assert seen == ["friday"]


def test_workspace_directories_are_created(config: Config):
    root = config.ensure_workspace()
    for directory in ("config", "memory", "logs", "tasks", "notes", "captures"):
        assert (root / directory).is_dir()
