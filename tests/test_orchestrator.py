"""End-to-end turns: the behaviour the product is actually judged on."""

from __future__ import annotations

import asyncio

from jarvis.core.events import EventType


async def test_greeting_never_touches_a_model(app, fake_provider):
    result = await app.ask("Hello.")
    assert result.text
    assert result.decision.path == "quick"
    assert fake_provider.calls == []


async def test_simple_requests_are_fast(app, fake_provider):
    for text in ["hello", "what time is it?", "how much storage do I have?", "what did I copy?"]:
        result = await app.ask(text)
        assert result.duration_ms < 800, f"{text} took {result.duration_ms:.0f} ms"
        assert fake_provider.calls == []


async def test_conversation_streams_through_the_model(app, fake_provider):
    fake_provider.responses.append("Lighthouses were automated over several decades, sir.")
    result = await app.ask("tell me something about lighthouses")
    assert "Lighthouses" in result.text
    deltas = [e for e in app.bus.history if e.type == EventType.ASSISTANT_DELTA]
    assert len(deltas) > 1
    assert fake_provider.calls


async def test_user_and_assistant_turns_are_remembered(app, fake_provider):
    fake_provider.responses.append("Noted.")
    await app.ask("this is a test message")
    messages = app.memory.recent_messages(4)
    roles = [m["role"] for m in messages]
    assert "user" in roles and "assistant" in roles


async def test_long_running_work_acknowledges_immediately(app, fake_provider, monkeypatch):
    """A long task must answer in well under a second and continue in the background."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handle(request):
        from jarvis.capabilities.base import Response

        started.set()
        await release.wait()
        return Response(text="Diagnostics complete. Everything looks healthy.")

    monkeypatch.setattr(app.capabilities["diagnostics"], "handle", slow_handle)

    result = await app.ask("why is my mac slow?")
    assert result.task_id is not None
    assert result.duration_ms < 500
    assert result.text  # an immediate acknowledgement
    await asyncio.wait_for(started.wait(), timeout=2)

    # The conversation is still responsive while the task runs.
    quick = await app.ask("what time is it?")
    assert quick.duration_ms < 800

    release.set()
    await asyncio.sleep(0.2)
    final = [e for e in app.bus.history
             if e.type == EventType.ASSISTANT_MESSAGE and "Diagnostics complete" in e.payload["text"]]
    assert final, "the background result was never delivered"


async def test_stop_cancels_the_running_task(app, monkeypatch):
    cancelled = asyncio.Event()

    async def slow_handle(request):
        from jarvis.capabilities.base import Response

        for _ in range(300):
            if request.ctx.cancelled():
                cancelled.set()
                return Response(text="stopped")
            await asyncio.sleep(0.01)
        return Response(text="completed")

    monkeypatch.setattr(app.capabilities["research"], "handle", slow_handle)
    result = await app.ask("research the history of the shipping forecast")
    assert result.task_id
    await asyncio.sleep(0.1)

    await app.ask("stop")
    await asyncio.sleep(0.2)
    assert cancelled.is_set()
    assert app.tasks.get(result.task_id).status == "cancelled"


async def test_tool_failure_is_explained_not_raised(app, monkeypatch):
    async def broken_open(name):
        from jarvis.tools.macos.controller import ShellResult

        return ShellResult(1, "", "Operation not permitted: denied")

    monkeypatch.setattr(app.controller, "open_app", broken_open)
    monkeypatch.setattr(app.apps, "resolve", lambda name: _resolved("Safari"))
    result = await app.ask("open Safari")
    assert "didn't open" in result.text
    assert "Traceback" not in result.text


async def test_arithmetic_is_computed_not_reasoned(app, fake_provider):
    result = await app.ask("what is 128 / 4")
    assert "32" in result.text
    assert fake_provider.calls == []


async def test_memory_commands_work_end_to_end(app):
    await app.ask("remember that I prefer short answers")
    recall = await app.ask("what do you remember about me")
    assert "short answers" in recall.text
    forget = await app.ask("forget that")
    assert "orgotten" in forget.text


async def test_confirmation_is_required_before_sending_email(app, monkeypatch):
    sent = {"value": False}

    async def fake_send(self, draft):
        sent["value"] = True
        return True

    from jarvis.tools.email.mail_app import AppleMailBackend

    monkeypatch.setattr(AppleMailBackend, "send", fake_send)
    app.config_store.update({"security": {"confirmation_timeout_s": 0.2}})

    ctx = app.deps.tool_context()
    result = await app.deps.registry.call(
        "send_email", {"to": ["someone@example.com"], "subject": "Hi", "body": "Hello"}, ctx
    )
    assert sent["value"] is False
    assert result.ok is False


async def test_routing_decisions_are_observable(app):
    await app.ask("what time is it?")
    routes = [e for e in app.bus.history if e.type == EventType.ROUTE]
    assert routes and routes[-1].payload["path"] == "quick"
    assert "turn.total" in app.telemetry.summary()


async def test_empty_input_is_ignored(app):
    result = await app.ask("   ")
    assert result.text == ""


def _resolved(name):
    async def _inner(*args, **kwargs):
        return name, 1.0

    return _inner()
