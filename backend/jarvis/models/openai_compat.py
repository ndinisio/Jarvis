"""OpenAI-compatible provider.

Works with the OpenAI API itself, LM Studio, llama.cpp's server, vLLM,
Ollama's ``/v1`` shim, OpenRouter and anything else speaking the same dialect.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..core.errors import ModelTimeout, ModelUnavailable
from .base import ChatMessage, ModelProvider, image_media_type


class OpenAICompatibleProvider(ModelProvider):
    name = "openai"

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

        try:
            async with self._http().stream(
                "POST", "/chat/completions", json=payload, timeout=timeout_s
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:400]
                    raise ModelUnavailable(
                        "The remote model provider rejected the request.",
                        detail=f"HTTP {resp.status_code}: {body}",
                    )
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
                    piece = delta.get("content") or ""
                    if piece:
                        yield piece
        except httpx.TimeoutException as exc:
            raise ModelTimeout(detail=f"{self.name} timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                "The model provider is unreachable.", detail=f"{type(exc).__name__}: {exc}"
            ) from exc

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


def _encode(message: ChatMessage) -> dict[str, Any]:
    if not message.images:
        return {"role": message.role, "content": message.content}
    parts: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
    for image in message.images:
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:{image_media_type(image)};base64,{image}"}}
        )
    return {"role": message.role, "content": parts}
