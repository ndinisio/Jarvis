"""Provider-agnostic model interface.

Nothing above this layer knows whether a response came from Ollama, an
OpenAI-compatible server or Anthropic. Adding a provider means implementing
three methods; it never means touching the router, the capabilities or the UI.
"""

from __future__ import annotations

import abc
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolCall:
    """A model asking for a tool to be run."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass(slots=True)
class ToolDef:
    """A tool as offered to a model: name, purpose and a JSON-schema input."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def as_openai(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


@dataclass(slots=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    #: Base64-encoded PNG/JPEG payloads for vision-capable models.
    images: list[str] = field(default_factory=list)
    #: On an assistant message: the tools it asked for.
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: On a tool message: which call this is the result of.
    tool_call_id: str = ""
    name: str = ""

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
    #: Tools the model asked for, when it was offered some.
    tool_calls: list[ToolCall] = field(default_factory=list)


class ModelProvider(abc.ABC):
    """A source of chat completions."""

    #: Stable identifier used in configuration (``ollama``, ``openai``, …).
    name: str = "provider"
    #: Whether this provider runs on the user's machine (affects privacy rules).
    local: bool = False
    #: Whether ``stream_chat`` takes the per-slot runtime options
    #: (``num_ctx``, ``keep_alive``) — true for Ollama, which manages its own
    #: model memory; hosted APIs have no such knobs.
    accepts_runtime_options: bool = False
    #: Whether :meth:`chat` is implemented natively (tool calls and
    #: schema-constrained output). The router emulates both over plain
    #: text for providers that can't.
    native_chat: bool = False

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        tools: list[ToolDef] | None = None,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 700,
        timeout_s: float = 60.0,
        think: bool | None = None,
        **runtime: Any,
    ) -> Completion:
        """One structured exchange: text, tool calls, or schema-shaped JSON.

        Only providers with ``native_chat`` implement this; the model router
        never calls it on any other.
        """
        raise NotImplementedError

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

    async def preload(self, model: str, **runtime: Any) -> bool:
        """Load *model* ahead of a request, without generating anything.
        Only a self-hosted server has anything to load; the default is a
        no-op."""
        return False

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class NativeUnsupported(Exception):
    """This provider/model can't do native tool calls or constrained output
    (e.g. an older model without tool support); the router emulates it."""


_THINK_BLOCK = re.compile(r"<think>.*?(?:</think>|$)", re.S | re.I)


def strip_thinking(text: str) -> str:
    """Remove a reasoning model's ``<think>…</think>`` preamble — it is the
    model talking to itself, never part of the answer."""
    return _THINK_BLOCK.sub("", text or "").strip()


class ThinkingFilter:
    """The streaming version of :func:`strip_thinking`: swallow deltas while
    inside a ``<think>`` block, pass everything else through."""

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False
        self._decided = False

    def feed(self, delta: str) -> str:
        if self._decided and not self._inside:
            return delta
        self._buffer += delta
        if not self._decided:
            stripped = self._buffer.lstrip()
            if len(stripped) < 7 and "<think>".startswith(stripped.lower()):
                return ""  # can't tell yet
            self._decided = True
            if not stripped.lower().startswith("<think>"):
                out, self._buffer = self._buffer, ""
                return out
            self._inside = True
        end = self._buffer.lower().find("</think>")
        if end < 0:
            return ""
        out = self._buffer[end + len("</think>"):].lstrip()
        self._buffer = ""
        self._inside = False
        return out

    def flush(self) -> str:
        out = "" if self._inside else self._buffer
        self._buffer = ""
        return out


def image_media_type(data: str) -> str:
    """The media type of a base64-encoded image, from its first bytes.

    Screenshots are re-encoded as JPEG when Pillow is available and stay PNG
    otherwise; a provider told the wrong type may reject the image outright.
    """
    head = (data or "").lstrip()[:16]
    if head.startswith("/9j/"):
        return "image/jpeg"
    if head.startswith("R0lGOD"):
        return "image/gif"
    if head.startswith("UklGR"):
        return "image/webp"
    return "image/png"


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
