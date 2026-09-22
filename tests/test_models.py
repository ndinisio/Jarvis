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


async def test_router_reconfiguration_rebuilds_providers(app):
    app.models.reconfigure(app.config)
    assert "ollama" in app.models.providers


async def test_complete_json_returns_none_on_garbage(app, fake_provider):
    fake_provider.json_responses.append("absolutely not json")
    assert await app.models.complete_json(Slot.FAST, [ChatMessage("user", "x")]) is None
