"""Wire-level provider tests.

These cover the code that actually talks to Ollama, an OpenAI-compatible server
and Anthropic — NDJSON, SSE, error shapes and image encoding — using mock
transports rather than a live service, so they run anywhere and need no keys.
"""

from __future__ import annotations

import json

import httpx
import pytest
from jarvis.core.errors import ModelTimeout, ModelUnavailable
from jarvis.models.anthropic import AnthropicProvider
from jarvis.models.base import ChatMessage
from jarvis.models.ollama import OllamaProvider
from jarvis.models.openai_compat import OpenAICompatibleProvider


def _mount(provider, handler):
    """Give a provider a client backed by a mock transport."""
    transport = httpx.MockTransport(handler)
    provider._client = httpx.AsyncClient(
        base_url=provider.base_url, transport=transport,
        headers=getattr(provider, "_headers", None) or {},
    )
    return provider


# --- Ollama -----------------------------------------------------------------

def _ollama_chat_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "llama3.2:1b"},
                                                    {"name": "llava:7b"}]})
    if request.url.path == "/api/chat":
        body = json.loads(request.content)
        assert body["stream"] is True
        lines = [
            json.dumps({"message": {"content": "Good "}, "done": False}),
            json.dumps({"message": {"content": "evening, "}, "done": False}),
            json.dumps({"message": {"content": "sir."}, "done": True}),
        ]
        return httpx.Response(200, text="\n".join(lines))
    return httpx.Response(404)


async def test_ollama_streams_ndjson():
    provider = _mount(OllamaProvider("http://ollama.test"), _ollama_chat_handler)
    chunks = [c async for c in provider.stream_chat([ChatMessage("user", "hi")], "llama3.2:1b")]
    assert "".join(chunks) == "Good evening, sir."
    assert len(chunks) == 3
    await provider.close()


async def test_ollama_lists_models_and_availability():
    provider = _mount(OllamaProvider("http://ollama.test"), _ollama_chat_handler)
    assert await provider.available() is True
    assert await provider.list_models() == ["llama3.2:1b", "llava:7b"]
    await provider.close()


async def test_ollama_sends_images_for_vision():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=json.dumps({"message": {"content": "ok"}, "done": True}))

    provider = _mount(OllamaProvider("http://ollama.test"), handler)
    message = ChatMessage("user", "what is this?", images=["BASE64DATA"])
    _ = [c async for c in provider.stream_chat([message], "llava:7b")]
    assert seen["messages"][0]["images"] == ["BASE64DATA"]
    await provider.close()


async def test_ollama_missing_model_is_explained():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found")

    provider = _mount(OllamaProvider("http://ollama.test"), handler)
    with pytest.raises(ModelUnavailable) as caught:
        _ = [c async for c in provider.stream_chat([ChatMessage("user", "hi")], "absent:1b")]
    assert "isn't installed" in caught.value.user_message
    assert "ollama pull" in (caught.value.detail or "")
    await provider.close()


async def test_ollama_error_payload_is_surfaced():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"error": "out of memory"}))

    provider = _mount(OllamaProvider("http://ollama.test"), handler)
    with pytest.raises(ModelUnavailable) as caught:
        _ = [c async for c in provider.stream_chat([ChatMessage("user", "hi")], "llama3.2:1b")]
    assert "out of memory" in (caught.value.detail or "")
    await provider.close()


async def test_ollama_timeout_is_typed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    provider = _mount(OllamaProvider("http://ollama.test"), handler)
    with pytest.raises(ModelTimeout):
        _ = [c async for c in provider.stream_chat([ChatMessage("user", "hi")], "llama3.2:1b")]
    await provider.close()


async def test_ollama_unreachable_is_typed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _mount(OllamaProvider("http://ollama.test"), handler)
    assert await provider.available() is False
    with pytest.raises(ModelUnavailable) as caught:
        _ = [c async for c in provider.stream_chat([ChatMessage("user", "hi")], "llama3.2:1b")]
    assert "isn't available" in caught.value.user_message
    await provider.close()


# --- OpenAI-compatible ------------------------------------------------------

def _sse(*payloads: dict) -> str:
    lines = [f"data: {json.dumps(p)}" for p in payloads]
    lines.append("data: [DONE]")
    return "\n\n".join(lines)


async def test_openai_compatible_streams_sse():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, text=_sse(
            {"choices": [{"delta": {"content": "Good "}}]},
            {"choices": [{"delta": {"content": "evening."}}]},
            {"choices": [{"delta": {}}]},
        ))

    provider = OpenAICompatibleProvider("https://api.test/v1", "test-key")
    provider._client = httpx.AsyncClient(
        base_url=provider.base_url, transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-key"},
    )
    chunks = [c async for c in provider.stream_chat([ChatMessage("user", "hi")], "gpt-4o-mini")]
    assert "".join(chunks) == "Good evening."
    await provider.close()


async def test_openai_compatible_encodes_images_as_data_urls():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=_sse({"choices": [{"delta": {"content": "ok"}}]}))

    provider = OpenAICompatibleProvider("https://api.test/v1", "k")
    provider._client = httpx.AsyncClient(base_url=provider.base_url,
                                         transport=httpx.MockTransport(handler))
    message = ChatMessage("user", "describe", images=["AAA"])
    _ = [c async for c in provider.stream_chat([message], "gpt-4o")]
    parts = seen["messages"][0]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,AAA")
    await provider.close()


async def test_openai_compatible_error_body_is_captured():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text='{"error": {"message": "rate limited"}}')

    provider = OpenAICompatibleProvider("https://api.test/v1", "k")
    provider._client = httpx.AsyncClient(base_url=provider.base_url,
                                         transport=httpx.MockTransport(handler))
    with pytest.raises(ModelUnavailable) as caught:
        _ = [c async for c in provider.stream_chat([ChatMessage("user", "hi")], "gpt-4o")]
    assert "429" in (caught.value.detail or "")
    assert "rate limited" in (caught.value.detail or "")
    await provider.close()


# --- Anthropic --------------------------------------------------------------

async def test_anthropic_streams_content_deltas():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "sk-test"
        body = json.loads(request.content)
        assert body["system"] == "You are JARVIS."
        return httpx.Response(200, text="\n".join([
            f'data: {json.dumps({"type": "content_block_delta", "delta": {"text": "Quite "}})}',
            f'data: {json.dumps({"type": "content_block_delta", "delta": {"text": "so."}})}',
            f'data: {json.dumps({"type": "message_stop"})}',
        ]))

    provider = AnthropicProvider("https://api.anthropic.test", "sk-test")
    provider._client = httpx.AsyncClient(
        base_url=provider.base_url, transport=httpx.MockTransport(handler),
        headers={"x-api-key": "sk-test", "anthropic-version": "2023-06-01"},
    )
    messages = [ChatMessage("system", "You are JARVIS."), ChatMessage("user", "hello")]
    chunks = [c async for c in provider.stream_chat(messages, "claude-sonnet-4-5")]
    assert "".join(chunks) == "Quite so."
    await provider.close()


async def test_anthropic_without_a_key_refuses_cleanly():
    provider = AnthropicProvider("https://api.anthropic.test", "")
    assert await provider.available() is False
    with pytest.raises(ModelUnavailable):
        _ = [c async for c in provider.stream_chat([ChatMessage("user", "hi")], "claude")]
