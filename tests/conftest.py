"""Shared fixtures.

Every test builds a real JARVIS against a temporary workspace: real event bus,
real router, real tool registry, real SQLite memory. Only the *outside world*
is mocked — models, the network and macOS automation — which is where the
boundaries actually are.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from jarvis.core.app import JarvisApp
from jarvis.core.config import Config, ConfigStore
from jarvis.models.base import ModelProvider


class FakeProvider(ModelProvider):
    """A model provider that returns whatever the test tells it to."""

    name = "fake"
    local = True

    #: Returned for ordinary (prose) completions, in order.
    #: JSON-mode calls — routing and planning — draw from ``json_responses``
    #: instead, so a test can script a classification without disturbing the
    #: conversational reply.
    def __init__(self, responses: list[str] | None = None, models: list[str] | None = None):
        self.responses = list(responses or [])
        self.json_responses: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self._models = models or ["fake-fast:1b", "fake-general:8b", "fake-vision:7b"]
        self.fail = False

    async def available(self) -> bool:
        return not self.fail

    async def list_models(self) -> list[str]:
        return list(self._models)

    async def stream_chat(self, messages, model, **kwargs):
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        if self.fail:
            from jarvis.core.errors import ModelUnavailable

            raise ModelUnavailable("test provider is offline")
        if kwargs.get("json_mode"):
            text = (self.json_responses.pop(0) if self.json_responses
                    else '{"capability": "conversation", "confidence": 0.5}')
        else:
            text = self.responses.pop(0) if self.responses else "Understood."
        for chunk in _chunks(text):
            await asyncio.sleep(0)
            yield chunk


def _chunks(text: str, size: int = 12):
    for index in range(0, len(text), size):
        yield text[index : index + size]


@pytest.fixture
def config(tmp_path: Path) -> Config:
    conf = Config(workspace=str(tmp_path / "JARVIS"))
    conf.voice.enabled = False
    conf.voice.tts_engine = "off"
    conf.ui.developer_mode = True
    conf.ensure_workspace()
    return conf


@pytest.fixture
def config_store(config: Config, tmp_path: Path) -> ConfigStore:
    return ConfigStore(config, tmp_path / "config.json")


@pytest.fixture
def app(config_store: ConfigStore) -> JarvisApp:
    instance = JarvisApp(config_store, enable_voice=False)
    return instance


@pytest.fixture
def fake_provider(app: JarvisApp) -> FakeProvider:
    """Replace every model slot with a controllable fake.

    A configuration change rebuilds the real providers (that is the point of
    reconfiguration), so the fake re-installs itself whenever that happens.
    """
    provider = FakeProvider()

    def install(*_args) -> None:
        app.models._providers = {"ollama": provider}
        app.models._catalog.clear()
        app.models._resolved.clear()
        for slot in ("fast", "general", "vision"):
            getattr(app.models._config.models, slot).model = "fake-fast:1b"

    install()
    app.config_store.on_change(install)
    return provider


@pytest.fixture
def ctx(app: JarvisApp):
    return app.deps.tool_context()


def collect(bus, types: set[str] | None = None) -> list:
    """Snapshot the events on the bus, optionally filtered by type."""
    return [e for e in bus.history if types is None or e.type in types]
