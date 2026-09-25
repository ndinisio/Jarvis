"""Provider abstraction: slots, substitution and graceful failure."""

from __future__ import annotations

import pytest
from jarvis.core.errors import ModelUnavailable
from jarvis.models.base import ChatMessage, extract_json
from jarvis.models.registry import Slot, _heuristic_pick, _match


def test_json_extraction_survives_chatty_models():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure! ```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Here you go: {"capability": "files"} — hope that helps') == {
        "capability": "files"
    }
    assert extract_json("no json at all") is None


def test_model_matching_tolerates_tags():
    installed = ["llama3.2:1b", "qwen2.5:7b", "llava:latest"]
    assert _match("llama3.2:1b", installed) == "llama3.2:1b"
    assert _match("llava", installed) == "llava:latest"
    assert _match("mistral", installed) is None


def test_heuristic_picks_by_size_and_kind():
    installed = ["llama3.1:8b", "qwen2.5:1.5b", "llava:7b", "nomic-embed-text"]
    assert _heuristic_pick(Slot.FAST, installed) == "qwen2.5:1.5b"
    assert _heuristic_pick(Slot.GENERAL, installed) == "llama3.1:8b"
    assert _heuristic_pick(Slot.VISION, installed) == "llava:7b"
    assert _heuristic_pick(Slot.VISION, ["llama3.1:8b"]) is None


def test_heuristic_treats_screen_watch_as_a_vision_slot():
    """A misconfigured/uninstalled models.screen_watch.model must still fall
    back to a vision-capable model, not the chat-model heuristic — the same
    guarantee Slot.VISION itself gets."""
    installed = ["llama3.1:8b", "qwen2.5:1.5b", "llava:7b", "nomic-embed-text"]
    assert _heuristic_pick(Slot.SCREEN_WATCH, installed) == "llava:7b"
    assert _heuristic_pick(Slot.SCREEN_WATCH, ["llama3.1:8b"]) is None


def test_screen_watch_slot_defers_to_vision_when_unconfigured(app):
    assert app.config.models.screen_watch.model == ""
    assert app.models.effective_slot(Slot.SCREEN_WATCH) == Slot.VISION

    app.config.models.screen_watch.model = "moondream"
    assert app.models.effective_slot(Slot.SCREEN_WATCH) == Slot.SCREEN_WATCH


async def test_slot_resolution_substitutes_missing_models(app, fake_provider):
    fake_provider._models = ["qwen2.5:1.5b", "qwen2.5:7b"]
    app.models._catalog.clear()
    app.models._resolved.clear()
    app.config.models.fast.model = "not-installed:1b"
    resolution = await app.models.resolve(Slot.FAST)
    assert resolution.substituted is True
    assert resolution.model in {"qwen2.5:1.5b", "qwen2.5:7b"}


async def test_streaming_and_completion(app, fake_provider):
    fake_provider.responses.append("Good evening, sir.")
    chunks = []
    async for delta in app.models.stream(Slot.FAST, [ChatMessage("user", "hello")]):
        chunks.append(delta)
    assert "".join(chunks) == "Good evening, sir."
    assert len(chunks) > 1  # genuinely streamed


async def test_telemetry_records_time_to_first_token(app, fake_provider):
    fake_provider.responses.append("A measured reply.")
    await app.models.complete(Slot.FAST, [ChatMessage("user", "hi")])
    assert "model.ttft" in app.telemetry.summary()


async def test_unavailable_provider_raises_typed_error(app, fake_provider):
    fake_provider.fail = True
    with pytest.raises(ModelUnavailable):
        await app.models.complete(Slot.GENERAL, [ChatMessage("user", "hello")])


async def test_status_reports_readiness(app, fake_provider):
    status = await app.models.status()
    assert status["providers"]["ollama"]["available"] is True
    assert status["slots"]["fast"]["ready"] is True

    fake_provider.fail = True
    app.models._catalog.clear()
    app.models._resolved.clear()
    status = await app.models.status()
    assert status["slots"]["fast"]["ready"] is False
    assert "reason" in status["slots"]["fast"]


async def test_status_reports_the_operator_slot_on_its_own(app, fake_provider):
    """A live mismatch this was blind to: operator (unconfigured) deferring
    to general, whose model wasn't installed, substituted silently — status()
    only ever reported fast/general/vision, never operator's own resolution."""
    fake_provider._models = ["llama3.1:8b"]
    app.models._catalog.clear()
    app.models._resolved.clear()
    app.config.models.operator.model = "qwen3:8b"
    status = await app.models.status()
    assert status["slots"]["operator"]["configured"] == "qwen3:8b"
    assert status["slots"]["operator"]["substituted"] is True
    assert status["slots"]["operator"]["resolved"] == "llama3.1:8b"


async def test_router_reconfiguration_rebuilds_providers(app):
    app.models.reconfigure(app.config)
    assert "ollama" in app.models.providers


async def test_complete_json_returns_none_on_garbage(app, fake_provider):
    fake_provider.json_responses.append("absolutely not json")
    assert await app.models.complete_json(Slot.FAST, [ChatMessage("user", "x")]) is None


# --- v3.0 Phase 2: chains, resting, privacy, emulation --------------------------

class _Scripted:
    """A native-chat provider that answers (or fails) as told."""

    native_chat = True
    accepts_runtime_options = False

    def __init__(self, name, *, local, outcomes):
        self.name = name
        self.local = local
        self.outcomes = list(outcomes)
        self.calls = 0

    async def available(self):
        return True

    async def list_models(self):
        return ["m"]

    async def chat(self, messages, model, **kwargs):
        from jarvis.models.base import Completion

        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return Completion(text=f"{self.name}:{outcome}", model=model, provider=self.name)

    async def close(self):
        return None


def _install(app, providers, chain):
    from jarvis.core.config import ChainLink

    app.models._providers = dict(providers)
    app.models._catalog.clear()
    app.models._resolved.clear()
    app.models._resting.clear()
    conf = app.models._config.models
    conf.operator.model = ""
    conf.reasoning.model = ""
    conf.general.provider = "local"
    conf.general.model = "m"
    conf.general.chain = [ChainLink(provider=name, model="cloud-model") for name in chain]


async def test_a_slot_chain_tries_the_accelerator_first_and_local_last(app):
    from jarvis.core.errors import RateLimited

    cloud = _Scripted("groq", local=False, outcomes=[RateLimited(detail="429"), "ok"])
    local = _Scripted("local", local=True, outcomes=["ok", "ok"])
    _install(app, {"groq": cloud, "local": local}, chain=["groq"])

    first = await app.models.chat(Slot.OPERATOR, [ChatMessage("user", "go")])
    assert first.text == "local:ok", "a rate-limited accelerator falls through to the local model"
    second = await app.models.chat(Slot.OPERATOR, [ChatMessage("user", "go")])
    assert second.text == "local:ok"
    assert cloud.calls == 1, "a rate-limited provider is rested, not retried on the very next call"


async def test_sensitive_work_never_leaves_the_mac(app):
    cloud = _Scripted("groq", local=False, outcomes=["ok"])
    local = _Scripted("local", local=True, outcomes=["ok"])
    _install(app, {"groq": cloud, "local": local}, chain=["groq"])

    completion = await app.models.chat(Slot.OPERATOR, [ChatMessage("user", "read my mail")],
                                       allow_cloud=False)
    assert completion.text == "local:ok"
    assert cloud.calls == 0


async def test_tool_calls_are_emulated_for_providers_without_native_support(app, fake_provider):
    from jarvis.models.base import ToolDef

    fake_provider.responses.append('{"tool": "browse_to", "arguments": {"url": "https://x.example"}}')
    tool = ToolDef("browse_to", "Open a page", {"type": "object", "properties": {"url": {"type": "string"}}})
    completion = await app.models.chat(Slot.GENERAL, [ChatMessage("user", "open x.example")], tools=[tool])
    assert completion.tool_calls[0].name == "browse_to"
    assert completion.tool_calls[0].arguments == {"url": "https://x.example"}
    assert "browse_to" in fake_provider.calls[-1]["messages"][0].content


async def test_complete_json_with_a_schema_goes_through_the_structured_path(app, fake_provider):
    fake_provider.json_responses.append('{"mode": "act"}')
    data = await app.models.complete_json(Slot.REASONING, [ChatMessage("user", "x")],
                                          schema={"type": "object"})
    assert data == {"mode": "act"}
    assert fake_provider.calls[-1]["kwargs"]["json_mode"] is True


def test_the_operator_and_fast_slots_defer_to_one_resident_model(app):
    assert app.models.effective_slot(Slot.OPERATOR) == Slot.GENERAL
    assert app.models.effective_slot(Slot.FAST) == Slot.GENERAL


def test_a_free_provider_switches_on_with_its_key(monkeypatch, tmp_path):
    from jarvis.core.config import load_config

    monkeypatch.setenv("JARVIS_GROQ_API_KEY", "gsk_test")
    config = load_config(tmp_path / "none.json")
    assert config.models.providers["groq"].enabled is True
    assert config.models.providers["openrouter"].enabled is False
