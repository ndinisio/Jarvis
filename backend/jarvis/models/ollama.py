"""Ollama provider — the default, fully local backend."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..core.errors import ModelTimeout, ModelUnavailable
from ..core.logging import get_logger
from .base import (
    ChatMessage,
    Completion,
    ModelProvider,
    NativeUnsupported,
    ThinkingFilter,
    ToolCall,
    ToolDef,
    strip_thinking,
)

log = get_logger("jarvis.models.ollama")


class OllamaProvider(ModelProvider):
    name = "ollama"
    accepts_runtime_options = True
    native_chat = True
    local = True

    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout_s: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._client: httpx.AsyncClient | None = None
        #: Models that rejected the thinking switch (not reasoning models).
        self._no_think: set[str] = set()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout)
        return self._client

    async def available(self) -> bool:
        try:
            resp = await self._http().get("/api/tags", timeout=2.5)
            return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        try:
            resp = await self._http().get("/api/tags", timeout=5.0)
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
        except Exception as exc:
            log.debug("ollama list_models failed: %s", exc)
            return []

    async def show(self, model: str) -> dict[str, Any]:
        try:
            resp = await self._http().post("/api/show", json={"name": model}, timeout=8.0)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def preload(self, model: str, *, num_ctx: int = 0, keep_alive: str = "30m",
                      **_: Any) -> bool:
        """Load *model* into memory (a generate request with no prompt). The
        context size must match the chat calls', or the next one would load
        it all over again."""
        payload: dict[str, Any] = {"model": model, "keep_alive": keep_alive or "30m"}
        if num_ctx:
            payload["options"] = {"num_ctx": int(num_ctx)}
        try:
            resp = await self._http().post("/api/generate", json=payload, timeout=120.0)
            return resp.status_code < 400
        except Exception as exc:
            log.debug("preload %s failed: %s", model, exc)
            return False

    async def pull(self, model: str) -> bool:  # pragma: no cover - network side effect
        try:
            async with self._http().stream(
                "POST", "/api/pull", json={"name": model}, timeout=None
            ) as resp:
                resp.raise_for_status()
                async for _ in resp.aiter_lines():
                    pass
            return True
        except Exception as exc:
            log.warning("pull %s failed: %s", model, exc)
            return False

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float = 0.4,
        max_tokens: int = 700,
        stop: list[str] | None = None,
        json_mode: bool = False,
        timeout_s: float = 60.0,
        num_ctx: int = 0,
        keep_alive: str = "30m",
        think: bool | None = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(messages, model, temperature=temperature, max_tokens=max_tokens,
                                num_ctx=num_ctx, keep_alive=keep_alive, think=think, stream=True)
        if stop:
            payload["options"]["stop"] = stop
        if json_mode:
            payload["format"] = "json"

        visible = ThinkingFilter()
        for attempt in range(2):
            try:
                async with self._http().stream(
                    "POST", "/api/chat", json=payload, timeout=timeout_s
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        if attempt == 0 and self._drop_think(payload, model, body):
                            continue
                        raise _http_error(model, resp.status_code, body)
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            raise ModelUnavailable("The local model reported an error.",
                                                   detail=str(chunk["error"]))
                        piece = visible.feed((chunk.get("message") or {}).get("content", ""))
                        if piece:
                            yield piece
                        if chunk.get("done"):
                            break
                    rest = visible.flush()
                    if rest:
                        yield rest
                    return
            except httpx.TimeoutException as exc:
                raise ModelTimeout(detail=f"ollama timeout after {timeout_s}s") from exc
            except httpx.HTTPError as exc:
                raise ModelUnavailable(
                    "The local AI service isn't available.",
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc

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
        num_ctx: int = 0,
        keep_alive: str = "30m",
        **_: Any,
    ) -> Completion:
        """One non-streamed exchange with native tool calling and, when a
        *schema* is given, output constrained to it by Ollama's grammar —
        a malformed reply is then impossible rather than merely unlikely."""
        payload = self._payload(messages, model, temperature=temperature, max_tokens=max_tokens,
                                num_ctx=num_ctx, keep_alive=keep_alive, think=think, stream=False)
        if tools:
            payload["tools"] = [tool.as_openai() for tool in tools]
        if schema is not None:
            payload["format"] = schema
        started = time.perf_counter()
        for attempt in range(2):
            try:
                resp = await self._http().post("/api/chat", json=payload, timeout=timeout_s)
            except httpx.TimeoutException as exc:
                raise ModelTimeout(detail=f"ollama timeout after {timeout_s}s") from exc
            except httpx.HTTPError as exc:
                raise ModelUnavailable("The local AI service isn't available.",
                                       detail=f"{type(exc).__name__}: {exc}") from exc
            if resp.status_code < 400:
                break
            body = resp.text
            if attempt == 0 and self._drop_think(payload, model, body):
                continue
            if tools and "does not support tools" in body.lower():
                raise NativeUnsupported(f"{model} has no tool support")
            raise _http_error(model, resp.status_code, body)
        data = resp.json()
        if data.get("error"):
            raise ModelUnavailable("The local model reported an error.", detail=str(data["error"]))
        message = data.get("message") or {}
        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            if function.get("name"):
                calls.append(ToolCall(name=str(function["name"]), arguments=_arguments(function.get("arguments")),
                                      id=str(call.get("id") or f"call_{index}")))
        return Completion(
            text=strip_thinking(message.get("content") or ""),
            model=model, provider=self.name,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            finish_reason=str(data.get("done_reason") or "stop"),
            tool_calls=calls,
            usage={"prompt_tokens": int(data.get("prompt_eval_count") or 0),
                   "completion_tokens": int(data.get("eval_count") or 0),
                   "load_ms": round((data.get("load_duration") or 0) / 1e6, 1),
                   "prompt_ms": round((data.get("prompt_eval_duration") or 0) / 1e6, 1)},
        )

    # -- helpers ------------------------------------------------------------
    def _payload(self, messages, model, *, temperature, max_tokens, num_ctx, keep_alive, think,
                 stream) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "stream": stream,
            "messages": [_encode(m) for m in messages],
            "options": {"temperature": temperature, "num_predict": max_tokens},
            "keep_alive": keep_alive or "30m",
        }
        if num_ctx:
            payload["options"]["num_ctx"] = int(num_ctx)
        if think is not None and model not in self._no_think:
            payload["think"] = bool(think)
        return payload

    def _drop_think(self, payload: dict[str, Any], model: str, body: str) -> bool:
        """A model that doesn't do "thinking" rejects the switch outright;
        remember that and retry without it."""
        if "think" in payload and "think" in body.lower():
            self._no_think.add(model)
            payload.pop("think", None)
            return True
        return False

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


def _http_error(model: str, status: int, body: str) -> Exception:
    if status == 404:
        return ModelUnavailable(f"The model {model} isn't installed.",
                                detail="ollama returned 404; run `ollama pull " + model + "`")
    return ModelUnavailable("The local model reported an error.", detail=f"HTTP {status}: {body[:300]}")


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _encode(message: ChatMessage) -> dict[str, Any]:
    data: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.images:
        data["images"] = message.images
    if message.tool_calls:
        data["tool_calls"] = [{"function": {"name": call.name, "arguments": call.arguments}}
                              for call in message.tool_calls]
    if message.role == "tool" and message.name:
        data["tool_name"] = message.name
    return data
