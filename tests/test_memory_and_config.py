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


def test_every_free_provider_switches_on_with_its_own_key(monkeypatch, tmp_path):
    """The free-tier providers (Groq, OpenRouter, Cerebras, Gemini, NVIDIA)
    all follow the same rule: off by default, on the moment their own key
    is set — none of them needs the others."""
    from jarvis.core import config as config_module

    for name, suffix in (("groq", "GROQ"), ("openrouter", "OPENROUTER"), ("cerebras", "CEREBRAS"),
                         ("gemini", "GEMINI"), ("nvidia", "NVIDIA")):
        monkeypatch.setenv(f"JARVIS_{suffix}_API_KEY", f"key-{name}")
    config = config_module.load_config(tmp_path / "missing.json")
    for name in ("groq", "openrouter", "cerebras", "gemini", "nvidia"):
        assert config.models.providers[name].enabled, name
        assert config.models.providers[name].api_key == f"key-{name}", name


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
    assert config.models.general.model


def test_config_change_listeners_fire(config_store):
    seen = []
    config_store.on_change(lambda c: seen.append(c.voice.wake_word))
    config_store.update({"voice": {"wake_word": "friday"}})
    assert seen == ["friday"]


def test_workspace_directories_are_created(config: Config):
    root = config.ensure_workspace()
    for directory in ("config", "memory", "logs", "tasks", "notes", "captures"):
        assert (root / directory).is_dir()


# -- upgrading a settings file written by an older JARVIS --------------------------

def _v13_settings_file(tmp_path, **overrides):
    """What JARVIS 1.3 wrote on first run: every setting, at its 1.3 default."""
    saved = Config().model_dump()
    del saved["config_version"]
    saved["models"]["fast"]["model"] = "llama3.2:1b"
    saved["models"]["general"]["model"] = "llama3.1:8b"
    saved["models"]["vision"]["model"] = "llava:7b"
    saved["models"]["fallbacks"]["general"] = [
        "llama3.1:8b", "qwen2.5:7b", "gemma3:4b", "mistral:7b", "llama3.2:3b", "qwen2.5:3b"]
    saved["voice"]["stt_engine"] = "faster-whisper"
    saved["voice"]["stt_model"] = "base.en"
    for dotted, value in overrides.items():
        cursor = saved
        *parents, last = dotted.split(".")
        for part in parents:
            cursor = cursor[part]
        cursor[last] = value
    path = tmp_path / "config" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(saved), encoding="utf-8")
    return path


def test_old_defaults_move_to_the_new_ones_and_choices_are_kept(tmp_path, monkeypatch):
    from jarvis.core.config import CONFIG_VERSION, create_store

    for name in ("JARVIS_GENERAL_MODEL", "JARVIS_FAST_MODEL", "JARVIS_VISION_MODEL", "JARVIS_STT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    path = _v13_settings_file(tmp_path, **{"models.vision.model": "llama3.2-vision:11b",
                                           "personality.address_user_as": "boss",
                                           "workspace": str(tmp_path / "JARVIS")})
    config = create_store(path).current
    assert config.models.general.model == "qwen3:8b"
    assert config.models.fast.model == "", "the separate 1B model is dropped; fast uses general"
    assert config.models.fallbacks["general"][0] == "qwen3:8b"
    assert (config.voice.stt_engine, config.voice.stt_model) == ("auto", "")
    # What the user chose is theirs.
    assert config.models.vision.model == "llama3.2-vision:11b"
    assert config.personality.address_user_as == "boss"
    # Written back once, so it doesn't happen again.
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["config_version"] == CONFIG_VERSION
    assert saved["models"]["general"]["model"] == "qwen3:8b"


def test_a_model_chosen_after_the_upgrade_is_never_changed_back(tmp_path, monkeypatch):
    from jarvis.core.config import create_store

    monkeypatch.delenv("JARVIS_GENERAL_MODEL", raising=False)
    path = _v13_settings_file(tmp_path, workspace=str(tmp_path / "JARVIS"))
    store = create_store(path)
    store.update({"models": {"general": {"model": "llama3.1:8b"}}})
    assert create_store(path).current.models.general.model == "llama3.1:8b"


def test_the_environment_still_overrides_an_upgraded_file(tmp_path, monkeypatch):
    path = _v13_settings_file(tmp_path, workspace=str(tmp_path / "JARVIS"))
    monkeypatch.setenv("JARVIS_GENERAL_MODEL", "mistral:7b")
    assert load_config(path).models.general.model == "mistral:7b"


def test_the_memory_database_uses_write_ahead_logging(tmp_path):
    import sqlite3

    from jarvis.memory.store import MemoryStore

    path = tmp_path / "memory" / "jarvis.db"
    store = MemoryStore(path)
    store.close()
    # WAL is a property of the file itself: any later reader sees it.
    other = sqlite3.connect(str(path))
    try:
        assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        other.close()


def test_settings_and_keys_are_read_from_a_dotenv_file(tmp_path, monkeypatch):
    """What .env.example promises: keys in .env are used (an app opened from
    Finder sees no shell profile), and the real environment still wins."""
    from jarvis.core import config as config_module

    monkeypatch.undo()                      # the autouse fixture turned .env off
    monkeypatch.chdir(tmp_path)
    for name in ("JARVIS_GENERAL_MODEL", "JARVIS_GROQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("JARVIS_GENERAL_MODEL=from-dotenv\nJARVIS_GROQ_API_KEY=gsk_test\n"
                                   "UNRELATED=ignored\n", encoding="utf-8")
    config = config_module.load_config(tmp_path / "missing.json")
    assert config.models.general.model == "from-dotenv"
    assert config.models.providers["groq"].enabled and config.models.providers["groq"].api_key == "gsk_test"
    monkeypatch.setenv("JARVIS_GENERAL_MODEL", "from-shell")
    assert config_module.load_config(tmp_path / "missing.json").models.general.model == "from-shell"


def test_dotenv_is_found_in_the_workspace_even_when_cwd_differs(tmp_path, monkeypatch):
    """The case a bare cwd check misses: a packaged app (or anything else
    launched from outside the workspace) still finds the workspace's own
    .env, because JARVIS_WORKSPACE is checked, not just the process cwd."""
    from jarvis.core import config as config_module

    monkeypatch.undo()
    workspace, elsewhere = tmp_path / "workspace", tmp_path / "elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("JARVIS_WORKSPACE", str(workspace))
    monkeypatch.delenv("JARVIS_GENERAL_MODEL", raising=False)
    (workspace / ".env").write_text("JARVIS_GENERAL_MODEL=from-workspace-dotenv\n", encoding="utf-8")
    config = config_module.load_config(tmp_path / "missing.json")
    assert config.models.general.model == "from-workspace-dotenv"
