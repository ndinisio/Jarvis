"""Routing: the deterministic engine must catch everyday requests without a model."""

from __future__ import annotations

import pytest
from jarvis.router.quick import QuickCommands
from jarvis.router.schema import RouteKind, RoutePath

QUICK_CASES = [
    ("Hello.", RouteKind.CONTROL, "greeting"),
    ("hey jarvis", RouteKind.CONTROL, "wake"),
    ("Thanks", RouteKind.CONTROL, "thanks"),
    ("stop", RouteKind.CONTROL, "cancel"),
    ("What time is it?", RouteKind.TOOL, "get_time"),
    ("what's today's date", RouteKind.TOOL, "get_time"),
    ("Open Safari", RouteKind.TOOL, "open_application"),
    ("launch visual studio code", RouteKind.TOOL, "open_application"),
    ("close Spotify", RouteKind.TOOL, "close_application"),
    ("How much storage is left?", RouteKind.TOOL, "get_storage"),
    ("what's my battery percentage", RouteKind.TOOL, "get_battery"),
    ("how much ram do I have", RouteKind.TOOL, "get_memory"),
    ("what macOS version am I running", RouteKind.TOOL, "get_system_info"),
    ("what chip does this mac have", RouteKind.TOOL, "get_system_info"),
    ("what did I copy?", RouteKind.TOOL, "read_clipboard"),
    ("copy hello world to my clipboard", RouteKind.TOOL, "write_clipboard"),
    ("take a screenshot", RouteKind.TOOL, "capture_screen"),
    ("what's on my screen?", RouteKind.TOOL, "analyse_screen"),
    ("go to apple.com", RouteKind.TOOL, "browse_to"),
    ("search the web for tide times", RouteKind.TOOL, "browse_to"),
    ("what's on today?", RouteKind.TOOL, "read_calendar"),
    ("mute", RouteKind.TOOL, "set_volume"),
    ("set volume to 40", RouteKind.TOOL, "set_volume"),
    ("check my emails", RouteKind.CAPABILITY, "email"),
    ("why is my mac slow?", RouteKind.CAPABILITY, "diagnostics"),
    ("research the best local AI models", RouteKind.CAPABILITY, "research"),
    ("what do you remember about me", RouteKind.CAPABILITY, "memory"),
    ("remember that I take my coffee black", RouteKind.CAPABILITY, "memory"),
]


@pytest.mark.parametrize("text,kind,name", QUICK_CASES)
def test_quick_commands_match(text, kind, name):
    decision = QuickCommands().match(text)
    assert decision is not None, f"no quick match for {text!r}"
    assert (decision.kind, decision.name) == (kind, name)


def test_quick_commands_are_fast():
    quick = QuickCommands()
    for text, _, _ in QUICK_CASES:
        decision = quick.match(text)
        assert decision.latency_ms < 5.0


def test_quick_extracts_arguments():
    quick = QuickCommands()
    assert quick.match("open vs code").args["name"] == "vs code"
    assert quick.match("set volume to 35").args["level"] == 35
    assert quick.match("copy the token to my clipboard").args["text"] == "the token"
    assert quick.match("remember that I prefer short answers").args["text"] == "I prefer short answers"


def test_unmatched_requests_fall_through():
    quick = QuickCommands()
    for text in ["what is the meaning of life", "write me a haiku about rain",
                 "explain how APFS snapshots work"]:
        assert quick.match(text) is None


async def test_router_prefers_quick_path(app):
    decision = await app.router.route("what time is it?")
    assert decision.path == RoutePath.QUICK
    assert decision.name == "get_time"


async def test_router_arithmetic_is_deterministic(app):
    decision = await app.router.route("what is 24 * 7")
    assert decision.kind == RouteKind.CONTROL
    assert decision.args["value"] == 168


async def test_router_heuristic_stage(app):
    decision = await app.router.route(
        "can you compare the reviews and prices for these headphones online"
    )
    assert decision.path in {RoutePath.HEURISTIC, RoutePath.MODEL, RoutePath.FALLBACK}
    assert decision.kind == RouteKind.CAPABILITY


async def test_router_falls_back_without_a_model(app, fake_provider):
    fake_provider.fail = True
    decision = await app.router.route("tell me something interesting about lighthouses")
    assert decision.name == "conversation"
    assert decision.path in {RoutePath.MODEL, RoutePath.FALLBACK}


async def test_router_uses_fast_model_classification(app, fake_provider):
    fake_provider.json_responses.append('{"capability": "files", "confidence": 0.9}')
    decision = await app.router.route("tuck that away somewhere sensible for later")
    assert decision.name == "files"
    assert decision.path == RoutePath.MODEL
    # Classification must go to the fast slot, never the general one.
    assert fake_provider.calls[-1]["kwargs"]["max_tokens"] <= 60


async def test_router_recovers_from_nonsense_classification(app, fake_provider):
    fake_provider.json_responses.append("I think you want the FILE capability maybe?")
    decision = await app.router.route("do the thing with the stuff")
    assert decision.kind == RouteKind.CAPABILITY
