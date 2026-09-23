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


# --- v3.0: runtime options and honest image types ------------------------------

async def test_ollama_receives_the_context_window_and_keep_alive():
    """Ollama's default context is small enough to silently cut the front
    off an agent prompt carrying a page's elements; the slot's num_ctx must
    reach the server."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=json.dumps({"message": {"content": "ok"}, "done": True}))

    provider = _mount(OllamaProvider("http://ollama.test"), handler)
    chunks = [c async for c in provider.stream_chat([ChatMessage("user", "hi")], "qwen3:8b",
                                                    num_ctx=8192, keep_alive="1h")]
    assert chunks == ["ok"]
    assert seen["options"]["num_ctx"] == 8192
    assert seen["keep_alive"] == "1h"
    await provider.close()


def test_image_media_type_is_read_from_the_bytes():
    from jarvis.models.base import image_media_type

    assert image_media_type("/9j/4AAQSkZJRgABAQ") == "image/jpeg"
    assert image_media_type("iVBORw0KGgoAAAANSU") == "image/png"
    assert image_media_type("R0lGODlhAQABAIAAAP") == "image/gif"


async def test_the_router_passes_slot_runtime_options_only_to_providers_that_take_them(app, fake_provider):
    """FakeProvider (like the hosted APIs) has no num_ctx knob — passing it
    one must not happen; Ollama must get it."""
    app.models._resolved.clear()
    await app.models.complete("general", [ChatMessage("user", "hi")])
    assert "num_ctx" not in fake_provider.calls[-1]["kwargs"]


# --- v3.0 Phase 2: native tool calls and constrained output ---------------------

async def test_ollama_chat_offers_tools_and_reads_back_the_call():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "message": {"content": "", "tool_calls": [
                {"function": {"name": "click_page_element", "arguments": {"handle": "jv9"}}}]},
            "done": True, "done_reason": "stop", "prompt_eval_count": 812, "eval_count": 21,
        })

    from jarvis.models.base import ToolDef

    provider = _mount(OllamaProvider("http://ollama.test"), handler)
    tool = ToolDef("click_page_element", "Click an element",
                   {"type": "object", "properties": {"handle": {"type": "string"}}})
    completion = await provider.chat([ChatMessage("user", "click it")], "qwen3:8b", tools=[tool],
                                     think=False, num_ctx=12288)
    assert seen["stream"] is False
    assert seen["tools"][0]["function"]["name"] == "click_page_element"
    assert seen["think"] is False and seen["options"]["num_ctx"] == 12288
    assert completion.tool_calls[0].name == "click_page_element"
    assert completion.tool_calls[0].arguments == {"handle": "jv9"}
    assert completion.usage["prompt_tokens"] == 812
    await provider.close()


async def test_ollama_chat_constrains_output_to_a_schema():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"mode": "act"}'}, "done": True})

    schema = {"type": "object", "properties": {"mode": {"enum": ["act", "chat"]}}, "required": ["mode"]}
    provider = _mount(OllamaProvider("http://ollama.test"), handler)
    completion = await provider.chat([ChatMessage("user", "x")], "qwen3:8b", schema=schema)
    assert seen["format"] == schema
    assert json.loads(completion.text) == {"mode": "act"}
    await provider.close()


async def test_ollama_retries_without_the_thinking_switch_for_models_that_dont_think():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "think" in body:
            return httpx.Response(400, json={"error": '"llama3.1:8b" does not support thinking'})
        return httpx.Response(200, json={"message": {"content": "hello"}, "done": True})

    provider = _mount(OllamaProvider("http://ollama.test"), handler)
    completion = await provider.chat([ChatMessage("user", "hi")], "llama3.1:8b", think=False)
    assert completion.text == "hello"
    assert len(bodies) == 2 and "think" not in bodies[1]
    # Remembered: the next call doesn't pay for the failed attempt again.
    await provider.chat([ChatMessage("user", "hi")], "llama3.1:8b", think=False)
    assert len(bodies) == 3
    await provider.close()


async def test_ollama_strips_a_reasoning_preamble_from_streamed_text():
    def handler(request: httpx.Request) -> httpx.Response:
        lines = [json.dumps({"message": {"content": c}, "done": False})
                 for c in ["<th", "ink>\nlet me", " think</think>\n\n", "Good ", "evening."]]
        lines.append(json.dumps({"message": {"content": ""}, "done": True}))
        return httpx.Response(200, text="\n".join(lines))

    provider = _mount(OllamaProvider("http://ollama.test"), handler)
    text = "".join([c async for c in provider.stream_chat([ChatMessage("user", "hi")], "qwen3:8b")])
    assert text == "Good evening."
    await provider.close()


async def test_openai_compatible_chat_parses_tool_calls_and_types_rate_limits():
    from jarvis.core.errors import RateLimited
    from jarvis.models.base import ToolDef

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "Rate limit reached"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_x", "type": "function",
             "function": {"name": "browse_to", "arguments": '{"url": "https://example.com"}'}}]},
            "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 90, "completion_tokens": 12}})

    provider = _mount(OpenAICompatibleProvider("http://groq.test/v1", "key", name="groq"), handler)
    tool = ToolDef("browse_to", "Open a page", {"type": "object", "properties": {"url": {"type": "string"}}})
    with pytest.raises(RateLimited):
        await provider.chat([ChatMessage("user", "go")], "llama", tools=[tool])
    completion = await provider.chat([ChatMessage("user", "go")], "llama", tools=[tool])
    assert completion.tool_calls[0].arguments == {"url": "https://example.com"}
    assert completion.tool_calls[0].id == "call_x"
    await provider.close()


async def test_openai_compatible_falls_back_to_json_mode_when_schemas_are_unsupported():
    formats: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        formats.append(body.get("response_format"))
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema unsupported"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"mode": "chat"}'}}]})

    provider = _mount(OpenAICompatibleProvider("http://x.test/v1", "k"), handler)
    completion = await provider.chat([ChatMessage("user", "x")], "m", schema={"type": "object"})
    assert [f["type"] for f in formats] == ["json_schema", "json_object"]
    assert completion.text == '{"mode": "chat"}'
    await provider.close()


def test_thinking_filter_handles_split_tags_and_plain_text():
    from jarvis.models.base import ThinkingFilter, strip_thinking

    assert strip_thinking("<think>hmm</think>\nAnswer") == "Answer"
    assert strip_thinking("<think>never closed") == ""
    plain = ThinkingFilter()
    assert plain.feed("Hel") + plain.feed("lo") + plain.flush() == "Hello"
