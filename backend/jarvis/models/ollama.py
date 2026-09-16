"""Ollama provider — the default, fully local backend."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..core.errors import ModelTimeout, ModelUnavailable
from ..core.logging import get_logger
from .base import ChatMessage, ModelProvider

log = get_logger("jarvis.models.ollama")


class OllamaProvider(ModelProvider):
    name = "ollama"
    local = True

    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout_s: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._client: httpx.AsyncClient | None = None

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
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": model,
            "stream": True,
            "messages": [_encode(m) for m in messages],
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
            "keep_alive": "30m",
        }
        if stop:
            payload["options"]["stop"] = stop
        if json_mode:
            payload["format"] = "json"

        try:
            async with self._http().stream(
                "POST", "/api/chat", json=payload, timeout=timeout_s
            ) as resp:
                if resp.status_code == 404:
                    raise ModelUnavailable(
                        f"The model {model} isn't installed.",
                        detail="ollama returned 404; run `ollama pull " + model + "`",
                    )
                resp.raise_for_status()
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
                    piece = (chunk.get("message") or {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except httpx.TimeoutException as exc:
            raise ModelTimeout(detail=f"ollama timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                "The local AI service isn't available.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


def _encode(message: ChatMessage) -> dict[str, Any]:
    data: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.images:
        data["images"] = message.images
    return data
