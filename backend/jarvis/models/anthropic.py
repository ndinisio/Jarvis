"""Anthropic-compatible provider (optional, never required for V1)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..core.errors import ModelTimeout, ModelUnavailable
from .base import ChatMessage, ModelProvider

API_VERSION = "2023-06-01"


class AnthropicProvider(ModelProvider):
    name = "anthropic"

    def __init__(self, base_url: str = "https://api.anthropic.com", api_key: str = "",
                 timeout_s: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout_s
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
            )
        return self._client

    async def available(self) -> bool:
        return bool(self.api_key)

    async def list_models(self) -> list[str]:
        if not self.api_key:
            return []
        try:
            resp = await self._http().get("/v1/models", timeout=8.0)
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
        if not self.api_key:
            raise ModelUnavailable("No Anthropic API key is configured.")
        system_parts = [m.content for m in messages if m.role == "system"]
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "messages": [_encode(m) for m in messages if m.role != "system"],
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if stop:
            payload["stop_sequences"] = stop
        if json_mode:
            payload["messages"].append({"role": "assistant", "content": "{"})

        try:
            async with self._http().stream(
                "POST", "/v1/messages", json=payload, timeout=timeout_s
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:400]
                    raise ModelUnavailable(
                        "Anthropic rejected the request.", detail=f"HTTP {resp.status_code}: {body}"
                    )
                if json_mode:
                    yield "{"
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        chunk = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("type") == "content_block_delta":
                        piece = (chunk.get("delta") or {}).get("text", "")
                        if piece:
                            yield piece
                    elif chunk.get("type") == "message_stop":
                        break
        except httpx.TimeoutException as exc:
            raise ModelTimeout(detail=f"anthropic timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                "Anthropic is unreachable.", detail=f"{type(exc).__name__}: {exc}"
            ) from exc

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


def _encode(message: ChatMessage) -> dict[str, Any]:
    if not message.images:
        return {"role": message.role, "content": message.content}
    content: list[dict[str, Any]] = []
    for image in message.images:
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": image},
            }
        )
    content.append({"type": "text", "text": message.content})
    return {"role": message.role, "content": content}
