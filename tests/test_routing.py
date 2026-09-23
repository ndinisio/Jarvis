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
    # V1.3 F3: a domain-shaped "open" target is a browser destination, not
    # an application name — must resolve to browse_to, not open_application.
    ("Open BBC.co.uk.", RouteKind.TOOL, "browse_to"),
    ("Open https://bbc.co.uk", RouteKind.TOOL, "browse_to"),
    ("Open www.bbc.co.uk.", RouteKind.TOOL, "browse_to"),
    ("what's on today?", RouteKind.TOOL, "read_calendar"),
    ("mute", RouteKind.TOOL, "set_volume"),
    ("set volume to 40", RouteKind.TOOL, "set_volume"),
    ("check my emails", RouteKind.CAPABILITY, "email"),
    ("why is my mac slow?", RouteKind.CAPABILITY, "diagnostics"),
    ("research the best local AI models", RouteKind.CAPABILITY, "research"),
    ("what do you remember about me", RouteKind.CAPABILITY, "memory"),
    ("remember that I take my coffee black", RouteKind.CAPABILITY, "memory"),
]

#: V1.3 F2: a bare reference to something in context is not a deterministic
#: application name, whatever verb it follows — it must fall through to
#: IntentTriage and reference resolution, never a literal quick-matched
#: open/close/activate attempt.
NOT_QUICK_MATCHED = [
    "Open the second one.",
    "Open the first one.",
    "Open that one.",
    "Open this one.",
    "Open it.",
    "Open the previous one.",
    "Open those.",
    "Open the other one.",
    "Close the second one.",
    "Switch to the second one.",
    "Search it.",
    "Do that.",
    "Who are you?",
    "Safari is really slow today.",
    "I hate dealing with email.",
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


@pytest.mark.parametrize("text", NOT_QUICK_MATCHED)
def test_contextual_and_conversational_requests_are_not_quick_matched(text):
    """V1.3 F2/F3: a syntactic match is not a safe one. None of these may be
    quick-routed — they must reach IntentTriage, where a bare reference gets
    resolved against real state (or asked about) instead of being treated as
    a literal, deterministic argument."""
    decision = QuickCommands().match(text)
    assert decision is None, f"{text!r} should not be a quick match, got {decision}"


def test_open_application_never_receives_a_bare_reference_or_web_destination():
    """Direct check on the safety gate itself, independent of which pattern
    ends up matching: open/close/activate must never build an argument that
    is a reference word or looks like a domain/URL."""
    quick = QuickCommands()
    for phrase in ("the second one", "the first one", "that one", "it", "those"):
        decision = quick.match(f"open {phrase}")
        assert decision is None or decision.name != "open_application"
    for destination in ("bbc.co.uk", "www.bbc.co.uk", "https://bbc.co.uk"):
        decision = quick.match(f"open {destination}")
        assert decision is not None and decision.name == "browse_to"


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
        "I'd love a comparison of the reviews and prices for these headphones online"
    )
    assert decision.path in {RoutePath.HEURISTIC, RoutePath.MODEL, RoutePath.FALLBACK}
    assert decision.kind == RouteKind.CAPABILITY


async def test_router_falls_back_without_a_model(app, fake_provider):
    fake_provider.fail = True
    decision = await app.router.route("tell me something interesting about lighthouses")
    assert decision.name == "conversation"
    assert decision.path in {RoutePath.MODEL, RoutePath.FALLBACK}


async def test_classify_hint_is_retained_but_route_never_calls_it(app, fake_provider):
    """V1.3 §3: the fast-model classifier is no longer authoritative for
    chat vs. action — IntentTriage is. ``_classify`` still exists, callable
    directly, as instrumentation/a hint; ``route()`` itself must not touch
    the model for anything past quick/arithmetic."""
    fake_provider.json_responses.append('{"capability": "files", "confidence": 0.9}')
    hint = await app.router._classify("tuck that away somewhere sensible for later")
    assert hint is not None and hint.name == "files"
    # Classification, when used at all, must go to the fast slot, never general.
    assert fake_provider.calls[-1]["kwargs"]["max_tokens"] <= 60

    calls_before = len(fake_provider.calls)
    decision = await app.router.route("tuck that away somewhere sensible for later")
    assert decision.path == RoutePath.FALLBACK
    assert len(fake_provider.calls) == calls_before, \
        "route() must not consult any model past quick/arithmetic"


async def test_classify_hint_degrades_gracefully_on_nonsense(app, fake_provider):
    fake_provider.json_responses.append("I think you want the FILE capability maybe?")
    hint = await app.router._classify("do the thing with the stuff")
    assert hint is None


# -- v3.0: one request, one clause -------------------------------------------

def test_the_whole_understanding_corpus_routes_correctly_on_the_fast_path():
    """The deterministic gate of the v3.0 evaluation (evals/utterances.yaml):
    every fast-path expectation in the 300+ utterance corpus holds."""
    from evals.run_understanding import check_quick
    from evals.suites import load_utterances

    failures = [(r.text, r.quick_expected, r.quick_actual)
                for r in map(check_quick, load_utterances()) if r.quick_ok is False]
    assert failures == []


@pytest.mark.parametrize("text", [
    "search for AirPods on Amazon and add them to my basket",
    "open Safari and go to bbc.co.uk",
    "check my email and then reply to Sarah",
    "take a screenshot, then describe it",
    "research the best laptops and add the top one to my basket",
    "what's my battery level and how much storage do I have",
])
def test_a_second_instruction_keeps_a_request_off_the_fast_path(text):
    assert QuickCommands().match(text) is None


@pytest.mark.parametrize("text,name", [
    ("could you open safari please", "open_application"),
    ("any chance you could open Mail real quick", "open_application"),
    ("can you take a screenshot for me", "capture_screen"),
    ("Go to github.com, please", "browse_to"),
    ("remember that I like tea and biscuits", "memory"),
    ("search for salt and pepper grinders", "browse_to"),
])
def test_politeness_and_lists_still_get_the_fast_answer(text, name):
    decision = QuickCommands().match(text)
    assert decision is not None and decision.name == name


@pytest.mark.parametrize("text", [
    "remove the kettle from my amazon basket",
    "delete the second email",
    "remember to buy milk",
    "open a new tab",
    "open the downloads folder",
    "search for usb cables on amazon",
    "how much storage does the iPhone 16 have",
    "what's the battery life of the MacBook Air",
])
def test_lookalike_requests_are_not_misrouted(text):
    assert QuickCommands().match(text) is None
