"""OpenAI-compatible provider.

Works with the OpenAI API itself, LM Studio, llama.cpp's server, MLX's
server, vLLM, Ollama's ``/v1`` shim, and the free hosted tiers that speak the
same dialect — Groq, OpenRouter's ``:free`` models, Cerebras and Google's
Gemini compatibility endpoint. A 429 is raised as :class:`RateLimited` so the
model router can move on to the next provider in a slot's chain.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..core.errors import ModelTimeout, ModelUnavailable, RateLimited
from .base import (
    ChatMessage,
    Completion,
    ModelProvider,
    NativeUnsupported,
    ThinkingFilter,
    ToolCall,
    ToolDef,
    image_media_type,
    strip_thinking,
)


class OpenAICompatibleProvider(ModelProvider):
    name = "openai"
    native_chat = True

    def __init__(self, base_url: str, api_key: str = "", timeout_s: float = 120.0,
                 name: str | None = None, local: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout_s
        self.local = local or "localhost" in base_url or "127.0.0.1" in base_url
        if name:
            self.name = name
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=self._timeout, headers=headers
            )
        return self._client

    async def available(self) -> bool:
        try:
            resp = await self._http().get("/models", timeout=4.0)
            return resp.status_code < 500
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        try:
            resp = await self._http().get("/models", timeout=8.0)
            resp.raise_for_status()
            return [m["id"] for m in resp.json().get("data", [])]
        except Exception:
            return []

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
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": model,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [_encode(m) for m in messages],
        }
        if stop:
            payload["stop"] = stop
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        visible = ThinkingFilter()
        try:
            async with self._http().stream(
                "POST", "/chat/completions", json=payload, timeout=timeout_s
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:400]
                    raise _http_error(self.name, resp.status_code, body)
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = visible.feed(delta.get("content") or "")
                    if piece:
                        yield piece
                rest = visible.flush()
                if rest:
                    yield rest
        except httpx.TimeoutException as exc:
            raise ModelTimeout(detail=f"{self.name} timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                "The model provider is unreachable.", detail=f"{type(exc).__name__}: {exc}"
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
        **_: Any,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [_encode(m) for m in messages],
        }
        if tools:
            payload["tools"] = [tool.as_openai() for tool in tools]
            payload["tool_choice"] = "auto"
        if schema is not None:
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "reply", "schema": schema}}
        started = time.perf_counter()
        for attempt in range(2):
            try:
                resp = await self._http().post("/chat/completions", json=payload, timeout=timeout_s)
            except httpx.TimeoutException as exc:
                raise ModelTimeout(detail=f"{self.name} timeout after {timeout_s}s") from exc
            except httpx.HTTPError as exc:
                raise ModelUnavailable("The model provider is unreachable.",
                                       detail=f"{type(exc).__name__}: {exc}") from exc
            if resp.status_code < 400:
                break
            body = resp.text[:600]
            # Not every compatible server takes a JSON schema; plain JSON
            # mode plus the caller's own validation is the next best thing.
            if attempt == 0 and schema is not None and resp.status_code == 400 \
                    and "response_format" in body.lower():
                payload["response_format"] = {"type": "json_object"}
                continue
            if tools and resp.status_code == 400 and "tool" in body.lower() and "support" in body.lower():
                raise NativeUnsupported(f"{self.name}/{model} rejected tool calling")
            raise _http_error(self.name, resp.status_code, body)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            if function.get("name"):
                calls.append(ToolCall(name=str(function["name"]), arguments=_arguments(function.get("arguments")),
                                      id=str(call.get("id") or f"call_{index}")))
        usage = data.get("usage") or {}
        return Completion(
            text=strip_thinking(message.get("content") or ""),
            model=model, provider=self.name,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            finish_reason=str(choice.get("finish_reason") or "stop"),
            tool_calls=calls,
            usage={"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                   "completion_tokens": int(usage.get("completion_tokens") or 0)},
        )

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


def _http_error(name: str, status: int, body: str) -> Exception:
    if status == 429 or "rate limit" in body.lower() or "quota" in body.lower():
        return RateLimited(detail=f"{name}: HTTP {status}: {body[:200]}")
    return ModelUnavailable("The remote model provider rejected the request.",
                            detail=f"HTTP {status}: {body[:300]}")


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _encode(message: ChatMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id or "call_0",
                "content": message.content}
    if message.tool_calls:
        return {"role": "assistant", "content": message.content or None,
                "tool_calls": [{"id": call.id or f"call_{index}", "type": "function",
                                "function": {"name": call.name, "arguments": json.dumps(call.arguments)}}
                               for index, call in enumerate(message.tool_calls)]}
    if not message.images:
        return {"role": message.role, "content": message.content}
    parts: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
    for image in message.images:
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:{image_media_type(image)};base64,{image}"}}
        )
    return {"role": message.role, "content": parts}
