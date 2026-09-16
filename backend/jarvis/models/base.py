"""Provider-agnostic model interface.

Nothing above this layer knows whether a response came from Ollama, an
OpenAI-compatible server or Anthropic. Adding a provider means implementing
three methods; it never means touching the router, the capabilities or the UI.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str
    #: Base64-encoded PNG/JPEG payloads for vision-capable models.
    images: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class Completion:
    text: str
    model: str
    provider: str
    latency_ms: float = 0.0
    ttft_ms: float | None = None
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)


class ModelProvider(abc.ABC):
    """A source of chat completions."""

    #: Stable identifier used in configuration (``ollama``, ``openai``, …).
    name: str = "provider"
    #: Whether this provider runs on the user's machine (affects privacy rules).
    local: bool = False

    @abc.abstractmethod
    async def available(self) -> bool:
        """Cheap reachability probe. Must never raise."""

    @abc.abstractmethod
    async def list_models(self) -> list[str]:
        """Model identifiers this provider can serve right now."""

    @abc.abstractmethod
    def stream_chat(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float = 0.4,
        max_tokens: int = 700,
        stop: list[str] | None = None,
        json_mode: bool = False,
        timeout_s: float = 60.0,
    ) -> AsyncIterator[str]:
        """Yield text deltas. Implementations are async generators."""

    async def complete(
        self,
        messages: list[ChatMessage],
        model: str,
        **kwargs: Any,
    ) -> Completion:
        """Collect a full response from :meth:`stream_chat`."""
        import time

        t0 = time.perf_counter()
        ttft: float | None = None
        parts: list[str] = []
        async for delta in self.stream_chat(messages, model, **kwargs):
            if ttft is None:
                ttft = (time.perf_counter() - t0) * 1000.0
            parts.append(delta)
        return Completion(
            text="".join(parts).strip(),
            model=model,
            provider=self.name,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            ttft_ms=ttft,
        )

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model response.

    Small local models habitually wrap JSON in prose or code fences, so the
    router cannot rely on strict decoding.
    """
    import json
    import re

    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    value = json.loads(text[start : i + 1])
                    return value if isinstance(value, dict) else None
                except json.JSONDecodeError:
                    start = -1
    return None
