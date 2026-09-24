"""The intelligence evaluation suite (V1.2).

These tests are about *behaviour*, not coverage. Each one asks whether JARVIS
understood, chose, checked or asked correctly — the twelve things section 22 of
the V1.2 brief names: intent understanding, entity resolution, follow-up
understanding, tool selection, argument extraction, multi-step planning, result
interpretation, verification, recovery, ambiguity handling, confirmation
handling and context retention.

The reasoning model is scripted, because a 1B model on a CI box would make
these tests measure the model rather than the architecture. It is scripted *by
purpose* rather than by call order (see :class:`Brain`): the agent's number of
model calls depends on what the tools actually return, which is the point, so a
positional script would be testing the wrong thing.

Nothing here special-cases a phrase inside the product. Every expectation is
met by the general machinery — context absorption, reference resolution,
shortlisting, verification, recovery — or it is a genuine failure.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from jarvis.intelligence.catalog import ToolCatalog
from jarvis.intelligence.entities import ReferenceResolver
from jarvis.intelligence.schema import Complexity, Confidence, Objective
from jarvis.intelligence.state import ConversationState
from jarvis.intelligence.verify import Verifier
from jarvis.tools.base import ToolResult
from jarvis.tools.email.mail_app import AppleMailBackend, MailMessage
from jarvis.tools.macos.controller import ShellResult

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# a scripted reasoning model
# ---------------------------------------------------------------------------
#: Marker text → the purpose of the prompt carrying it. Taken from the real
#: prompts, so a prompt rewrite that changes meaning fails these tests loudly
#: instead of silently scripting the wrong stage.
_PURPOSES = (
    ("Classify the user's request into exactly one capability", "classify"),
    ("You decide what the user wants. Two modes only", "triage"),
    ("You work out what the user wants", "understand"),
    ("You operate this Mac for the user", "decide"),
    ("Give the user the answer", "final"),
)


def _purpose(messages) -> str:
    text = " ".join(m.content for m in messages)
    for marker, name in _PURPOSES:
        if marker in text:
            return name
    return "chat"


class Brain:
    """Scripts the reasoning model one stage at a time.

    Each stage holds a queue; the last entry is reused once the queue empties,
    so a test only scripts the turns it actually cares about. ``asked`` records
    every prompt, which is what lets a test assert on what the model was *shown*
    — the shortlist, the context — rather than only on what it replied.
    """

    def __init__(self, provider):
        self.queues: dict[str, list[str]] = {}
        self.asked: list[tuple[str, str]] = []
        provider.router = self._reply

    def script(self, purpose: str, *replies) -> Brain:
        self.queues.setdefault(purpose, []).extend(
            json.dumps(r) if isinstance(r, dict) else str(r) for r in replies
        )
        return self

    def understand(self, **fields) -> Brain:
        base = {"goal": "", "kind": "general", "targets": [], "constraints": [],
                "references": [], "needs_tools": True, "complexity": Complexity.SIMPLE,
                "confidence": Confidence.CONFIDENT, "refines_previous": False,
                "is_correction": False, "missing": []}
        return self.script("understand", {**base, **fields})

    def triage(self, **fields) -> Brain:
        base = {"mode": "action", "confidence": 0.9, "action_evidence": ["scripted"],
                "objective": None, "requires_tools": True, "reason": ""}
        return self.script("triage", {**base, **fields})

    def decide(self, **fields) -> Brain:
        """One operator reply, written as the decision it stands for:
        ``action="tool_call"`` (``tool``, ``arguments``), ``"respond"``
        (``content``: finish with that answer), ``"complete"`` (finish and
        let the answer be composed from what was found), ``"clarify"``
        (``question``) or ``"give_up"`` (``reason``)."""
        return self.script("decide", _operator_reply(fields))

    def prompts(self, purpose: str) -> list[str]:
        return [text for name, text in self.asked if name == purpose]

    def _reply(self, messages, kwargs) -> str | None:
        purpose = _purpose(messages)
        self.asked.append((purpose, " ".join(m.content for m in messages)))
        queue = self.queues.get(purpose)
        if queue:
            return queue.pop(0)
        # Nothing left to say. Prose falls through to the provider's own reply;
        # every decision stage gets a default that *ends* the turn, so a test
        # that under-scripts fails with a short answer rather than a loop.
        return None if purpose in {"final", "chat", "classify"} else _DEFAULTS[purpose]


def _operator_reply(fields: dict) -> dict:
    """A decision as the operator's (emulated) tool call."""
    action = fields.get("action")
    if action == "tool_call":
        return {"tool": fields.get("tool"), "arguments": fields.get("arguments") or {}}
    if action == "respond":
        return {"tool": "finish", "arguments": {"summary": fields.get("content") or "",
                                                "evidence": list(fields.get("evidence") or [])}}
    if action == "complete":
        return {"tool": "finish", "arguments": {"summary": ""}}
    if action == "clarify":
        return {"tool": "ask_user", "arguments": {"question": fields.get("question") or ""}}
    if action == "give_up":
        return {"tool": "give_up", "arguments": {"reason": fields.get("reason") or ""}}
    raise AssertionError(f"not a decision: {fields}")


_DEFAULTS = {
    # Nothing scripted: escalate to Understanding (below), which itself
    # defaults to chat — so an unscripted turn can never wander off and touch
    # a tool. objective=None guarantees the escalation (V1.3 §5/§6): triage
    # alone never has enough to be "sufficient".
    "triage": json.dumps({"mode": "action", "confidence": 0.5,
                          "action_evidence": ["unscripted"], "objective": None,
                          "requires_tools": True, "reason": "unscripted default"}),
    "understand": json.dumps({"goal": "unscripted", "needs_tools": False,
                              "complexity": Complexity.TRIVIAL,
                              "confidence": Confidence.CONFIDENT}),
    # Nothing scripted: finish, composing the answer from whatever was found.
    "decide": json.dumps({"tool": "finish", "arguments": {"summary": ""}}),
}


@pytest.fixture
def brain(app, fake_provider) -> Brain:
    app.config_store.update({"security": {"auto_approve": ["low", "medium"],
                                          "confirmation_timeout_s": 0.2}})
    scripted = Brain(fake_provider)
    yield scripted
    # A reply nobody asked for means the script drifted out of step with the
    # turns — usually because the quick path claimed a turn the test thought it
    # was scripting. That produces a test which passes for the wrong reason, so
    # it fails here instead.
    leftover = {purpose: queue for purpose, queue in scripted.queues.items() if queue}
    assert not leftover, f"scripted replies were never used: {leftover}"


# ---------------------------------------------------------------------------
# world fixtures — only the outside world is faked
# ---------------------------------------------------------------------------
@pytest.fixture
def inbox(app, monkeypatch):
    messages = [
        MailMessage(id="1", subject="Sunday lunch", sender="Tom Blake <tom@blake.example>",
                    date="Monday", preview="Are you coming on Sunday?"),
        MailMessage(id="2", subject="Invoice 88 overdue", sender="billing@acme.example",
                    date="Monday", preview="Payment is now overdue."),
        MailMessage(id="3", subject="Re: the roof", sender="Ada Blake <ada@blake.example>",
                    date="Tuesday", preview="The builder can come Thursday."),
    ]

    async def unread_count(self):
        return len(messages)

    async def recent(self, limit=8, unread_only=True):
        return list(messages)

    async def body(self, message_id):
        return f"Body of message {message_id}."

    monkeypatch.setattr(AppleMailBackend, "unread_count", unread_count)
    monkeypatch.setattr(AppleMailBackend, "recent", recent)
    monkeypatch.setattr(AppleMailBackend, "body", body)
    for name in ("check_email", "read_email", "search_email", "draft_email"):
        monkeypatch.setattr(app.deps.registry.get(name).spec, "requires_macos", False)
    return messages


@pytest.fixture
def desktop(app, monkeypatch):
    """A fake Mac: applications launch, and we can see what was launched."""
    opened: list[str] = []
    running: set[str] = set()
    urls: list[str] = []

    async def open_app(self, name):
        opened.append(name)
        running.add(name.lower())
        return ShellResult(0, "", "")

    async def is_app_running(self, name):
        return name.lower() in running

    async def open_url(self, url, browser=None):
        urls.append(url)
        return ShellResult(0, "", "")

    async def frontmost_app(self):
        return opened[-1] if opened else ""

    async def installed(self, refresh=False):
        return ["Safari", "Mail", "Calendar", "Notes", "Terminal", "Xcode"]

    controller = type(app.deps.controller)
    monkeypatch.setattr(type(app.deps.apps), "apps", installed)
    monkeypatch.setattr(controller, "open_app", open_app)
    monkeypatch.setattr(controller, "is_app_running", is_app_running)
    monkeypatch.setattr(controller, "open_url", open_url)
    monkeypatch.setattr(controller, "frontmost_app", frontmost_app)
    for name in ("open_application", "activate_application", "get_current_page",
                 "analyse_screen", "click_element", "type_text", "press_key",
                 "get_frontmost_app"):
        tool = app.deps.registry.get(name)
        if tool is not None:
            monkeypatch.setattr(tool.spec, "requires_macos", False)
    return {"opened": opened, "urls": urls, "running": running}


async def settle(app, timeout: float = 5.0) -> None:
    """Wait for the background task a turn may have started.

    Long work is acknowledged immediately and finished asynchronously — that is
    the design — so a test that cares about the *result* has to wait for it.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not [t for t in app.tasks.all() if t.cancellable]:
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.02)
    raise AssertionError("a background task never finished")


def _stub_tool(app, monkeypatch, name, result: ToolResult):
    """Replace one tool's execution, leaving validation and permissions intact."""
    calls: list[dict] = []
    tool = app.deps.registry.get(name)

    async def run(args, ctx):
        calls.append(dict(args))
        return result

    monkeypatch.setattr(tool, "run", run)
    monkeypatch.setattr(tool.spec, "requires_macos", False)
    return calls


# ===========================================================================
# Examples A–F from the brief
# ===========================================================================
async def test_example_a_email_context_survives_the_follow_up(app, brain, inbox):
    """"Check my emails." → "Anything from my brother?" keeps the inbox.

    The first turn is a quick match answered by the V1.1 email capability and
    the second is the agent, so this fails if context only flows through the
    agent's own calls.
    """
    await app.ask("check my emails")
    await settle(app)

    state = app.orchestrator.state
    assert len(state.email.messages) == 3, "the inbox should be in context"
    assert state.email.unread == 3

    # Second turn: the follow-up is answered from context, not from a new fetch.
    brain.understand(goal="find mail from the user's brother", kind="read email",
                     targets=["brother"], refines_previous=True,
                     references=[{"text": "my brother", "kind": "person"}],
                     needs_tools=True)
    brain.decide(action="respond", content="Tom wrote about Sunday lunch, sir.",
                 reason="the messages are already in context")
    result = await app.ask("anything from my brother?")
    await settle(app)

    assert result.text == "Tom wrote about Sunday lunch, sir."
    # And it was answered from context, not by fetching the mail again.
    fetches = [e for e in app.bus.history
               if e.type == "tool.call" and e.payload["tool"] == "check_email"]
    assert len(fetches) == 1, "the inbox should not have been re-read"
    # What matters is that the model was *shown* the inbox on the second turn.
    assert "Sunday lunch" in brain.prompts("understand")[-1]


async def test_example_b_browser_context_survives_the_follow_up(app, brain, desktop):
    """"Open Safari." → "Go to the BBC." knows which browser to use."""
    await app.ask("open Safari")                     # quick path
    assert desktop["opened"] == ["Safari"]
    assert app.orchestrator.state.browser.app == "Safari"

    brain.understand(goal="go to the BBC", kind="navigate", targets=["BBC"],
                     refines_previous=True)
    brain.decide(action="tool_call", tool="browse_to",
                 arguments={"url": "bbc.co.uk", "browser": "Safari"},
                 reason="navigate in the browser already open")
    await app.ask("now go to the BBC")

    assert desktop["urls"] == ["https://bbc.co.uk"]
    assert "bbc.co.uk" in app.orchestrator.state.browser.url
    assert "Safari" in brain.prompts("decide")[-1], "the open browser should be context"


async def test_example_c_research_context_survives_the_follow_up(app, brain, monkeypatch):
    """"Research specialised cells." → "Which ones are used in medicine?".

    The first turn is a quick match and stays on V1.1's research capability;
    the follow-up is not, so it goes to the agent — which must still be able to
    see what the investigation found. That crossing is the point: context is
    absorbed from whatever machinery produced the result.
    """
    from jarvis.capabilities.base import Response

    sources = [
        {"index": 1, "title": "Stem cells in therapy", "url": "https://a.example",
         "domain": "a.example", "snippet": "Used in bone-marrow transplants."},
        {"index": 2, "title": "Neurons", "url": "https://b.example",
         "domain": "b.example", "snippet": "Signal conduction."},
    ]

    async def investigation(request):
        return Response(text="Cells specialise…", spoken="Two sources compared.",
                        data={"sources": sources})

    monkeypatch.setattr(app.capabilities["research"], "handle", investigation)

    await app.ask("research specialised cells")
    await settle(app)

    state = app.orchestrator.state
    assert len(state.research.sources) == 2

    brain.understand(goal="research specialised cells", kind="research",
                     constraints=["used in medicine"], refines_previous=True,
                     references=[{"text": "which ones", "kind": "result"}])
    brain.decide(action="respond", content="Stem cells, sir — used in transplants.",
                 reason="the sources are already gathered")
    result = await app.ask("which ones are used in medicine?")
    await settle(app)

    assert "Stem cells" in result.text
    assert "Stem cells in therapy" in brain.prompts("decide")[-1]


async def test_example_d_screen_context_makes_the_screen_actionable(app, brain, desktop,
                                                                   monkeypatch):
    """"What is on my screen?" → "Click the search bar." acts on what was seen."""
    _stub_tool(app, monkeypatch, "analyse_screen", ToolResult(
        data={"answer": "A browser window with a search bar and a Sign in button.",
              "path": "/tmp/shot.png"},
        summary="A browser window with a search bar and a Sign in button."))
    clicks = _stub_tool(app, monkeypatch, "click_element",
                        ToolResult(data={"label": "Search", "matched": "Search"},
                                   summary="Clicked Search."))

    brain.understand(goal="describe the screen", kind="inspect screen")
    brain.decide(action="tool_call", tool="analyse_screen",
                 arguments={"question": "Describe what is on this screen."},
                 reason="the user asked about the screen")
    await app.ask("what is on my screen right now?")
    await settle(app)

    state = app.orchestrator.state
    assert state.screen.fresh
    assert "search bar" in " ".join(state.screen.elements).lower()

    brain.understand(goal="click the search bar", kind="click",
                     targets=["search bar"],
                     references=[{"text": "the search bar", "kind": "screen_element"}])
    brain.decide(action="tool_call", tool="click_element", arguments={"label": "search bar"},
                 reason="act on the element that was seen")
    await app.ask("click the search bar")
    await settle(app)

    assert clicks == [{"label": "search bar", "app": ""}]
    assert "search bar" in brain.prompts("decide")[-1].lower()


async def test_example_e_correction_recovers_rather_than_starting_over(app, brain, monkeypatch,
                                                                      desktop):
    """"Open the BBC." → a 404 → "That's not right." is a correction, not a new task."""
    _stub_tool(app, monkeypatch, "browse_to", ToolResult(
        data={"url": "https://bbc.example/missing", "title": "404 — Page not found"},
        summary="Opening bbc.example."))

    brain.understand(goal="open the BBC", kind="navigate", targets=["BBC"])
    brain.decide(action="tool_call", tool="browse_to", arguments={"url": "bbc.example/missing"},
                 reason="navigate to the BBC")
    await app.ask("open the BBC please")
    await settle(app)

    # Verification, not the tool's own optimism, decides whether that worked —
    # and the model deciding what to do next is told.
    verify = [e for e in app.bus.history
              if e.type == "intelligence.trace" and e.payload.get("stage") == "verify"]
    assert verify and verify[-1].payload["verified"] is False
    assert "not-found" in verify[-1].payload["problem"]
    assert "this did not work" in brain.prompts("decide")[1]

    # The correction inherits the previous objective rather than becoming a new one.
    brain.understand(goal="open the BBC", kind="navigate", targets=["BBC"],
                     is_correction=True, refines_previous=True)
    brain.decide(action="respond", content="Trying bbc.co.uk instead, sir.",
                 reason="retry with the real address")
    await app.ask("that's not right")
    await settle(app)

    intents = [e.payload for e in app.bus.history
               if e.type == "intelligence.trace" and e.payload.get("stage") == "intent"]
    assert intents[-1]["is_correction"] is True
    assert intents[-1]["goal"] == "open the BBC", "a correction keeps the objective"


async def test_example_f_ambiguous_recipient_is_a_question_not_a_guess(app, brain, inbox):
    """"Email him." with three senders in context must ask which one."""
    await app.ask("check my emails")          # quick path; still fills the context
    await settle(app)
    assert len(app.orchestrator.state.email.messages) == 3

    brain.understand(goal="email him", kind="send email",
                     references=[{"text": "him", "kind": "person"}],
                     confidence=Confidence.AMBIGUOUS, missing=["recipient"])
    result = await app.ask("email him")
    await settle(app)

    assert result.text.rstrip().endswith("?"), f"expected a question, got: {result.text}"
    assert app.orchestrator.state.pending_clarification is not None
    # And it must not have guessed a recipient in the meantime.
    assert not [e for e in app.bus.history
                if e.type == "tool.call" and e.payload["tool"] in {"draft_email", "send_email"}]


async def test_an_answered_clarification_resumes_the_waiting_objective(app, brain, inbox):
    """The reply to "which one?" continues the original request."""
    await app.ask("check my emails")
    await settle(app)

    brain.understand(goal="email him", kind="send email",
                     references=[{"text": "him", "kind": "person"}],
                     confidence=Confidence.AMBIGUOUS, missing=["recipient"])
    await app.ask("email him")
    await settle(app)
    assert app.orchestrator.state.pending_clarification is not None

    brain.understand(goal="", kind="send email", targets=["Ada"])
    brain.decide(action="respond", content="Drafting a note to Ada, sir.", reason="resume")
    result = await app.ask("Ada")
    await settle(app)

    assert app.orchestrator.state.pending_clarification is None, "the question was answered"
    assert "Ada" in result.text
    intents = [e.payload for e in app.bus.history
               if e.type == "intelligence.trace" and e.payload.get("stage") == "intent"]
    assert intents[-1]["goal"] == "email him", "the original objective was resumed"


# ===========================================================================
# V1.3 — intent triage (chat vs. action)
# ===========================================================================
async def test_triage_schema_requires_positive_evidence_for_action():
    """No action without positive evidence — a domain word mentioned in
    passing is not a request. This is the gate itself, independent of any
    model: see IntentTriage's docstring and the V1.3 benchmark."""
    from jarvis.intelligence.schema import Triage

    triage = Triage(mode="action", action_evidence=[])
    assert triage.mode == "chat"
    assert triage.objective is None

    grounded = Triage(mode="action", action_evidence=["open safari"])
    assert grounded.mode == "action"


async def test_triage_chat_never_touches_the_tool_machinery(app, brain):
    """mode="chat" goes straight to conversation — no Understanding call, no
    tool, no clarification. Domain words in the phrase are not evidence."""
    brain.triage(mode="chat", confidence=0.9, action_evidence=[], objective=None,
                requires_tools=False, reason="just talking")
    app.models._providers["ollama"].responses.append("I'm quite well, thank you, sir.")
    result = await app.ask("Hey Jarvis, how are you?")
    assert "well" in result.text
    assert brain.prompts("understand") == [], "chat must bypass Understanding entirely"
    assert not [e for e in app.bus.history if e.type == "tool.call"]


async def test_domain_words_mentioned_in_passing_stay_chat(app, brain):
    """"I hate dealing with email." — a domain word without a request."""
    brain.triage(mode="chat", confidence=0.85, action_evidence=[], objective=None,
                requires_tools=False, reason="venting, not asking")
    app.models._providers["ollama"].responses.append("It happens to the best of us, sir.")
    await app.ask("I hate dealing with email.")
    assert not [e for e in app.bus.history if e.type == "tool.call"]
    assert app.orchestrator.state.pending_clarification is None


async def test_triage_action_with_sufficient_objective_skips_understanding(app, brain, desktop):
    """Action ≠ automatic execution in general (V1.3 §6), but when triage's
    own objective is already confident and complete, the agent acts on it
    directly rather than paying for a second model call."""
    brain.triage(mode="action", confidence=0.95, action_evidence=["open safari"],
                requires_tools=True, reason="explicit request",
                objective={"goal": "open Safari", "kind": "open application",
                          "targets": ["Safari"], "complexity": "simple",
                          "confidence": "confident", "missing": []})
    brain.decide(action="tool_call", tool="open_application", arguments={"name": "Safari"},
                 reason="the user named it")
    await app.ask("get Safari up on screen for me")
    assert desktop["opened"] == ["Safari"]
    assert brain.prompts("understand") == [], "a sufficient objective should skip Understanding"


async def test_ambiguous_send_it_asks_rather_than_guessing(app, brain):
    """"Send it." action-shaped, but nothing to send without more context.

    Bare "send it" is the fast gateway's own deterministic affirm/confirm
    pattern (existing V1.1 behaviour, unrelated to triage — it means "yes" to
    a pending confirmation, and correctly reports there is none). "Please
    send it." carries the same ambiguity without colliding with that pattern,
    and is what reaches triage/Understanding here.
    """
    brain.understand(goal="send it", kind="send email", confidence=Confidence.AMBIGUOUS,
                     missing=["recipient"])
    result = await app.ask("Please send it.")
    assert result.text.rstrip().endswith("?")
    assert not [e for e in app.bus.history
                if e.type == "tool.call" and e.payload["tool"] in {"draft_email", "send_email"}]


async def test_ambiguous_do_that_asks_rather_than_inventing_a_referent(app, brain):
    """"Do that." with nothing in context to resolve it against. A mutating
    kind (send email) makes the shortlisted tool consequential, which is what
    turns the ambiguity into a question rather than a silent guess — see
    ToolCatalog._changes_state and _ambiguity_question."""
    brain.understand(goal="repeat the last action", kind="send email",
                     confidence=Confidence.AMBIGUOUS, missing=["what to send and to whom"])
    result = await app.ask("Do that.")
    assert result.text.rstrip().endswith("?")
    assert app.orchestrator.state.pending_clarification is not None


async def test_open_the_second_one_resolves_against_prior_results(app, brain, monkeypatch):
    """"Open the second one." after a search resolves against the itemised
    results already in ConversationState (ReferenceResolver's ordinal stage;
    see intelligence/entities.py — no changes needed here, V1.3 §8).

    Before the V1.3 F2 fix, this phrase quick-matched
    ``open_application(name="the second one")`` first and only reached this
    resolution via the wrong_tool rescue path, after a spurious launch
    attempt. It must now never touch open_application at all."""
    from jarvis.capabilities.base import Response

    sources = [{"index": 1, "title": "First result", "url": "https://one.example"},
              {"index": 2, "title": "Second result", "url": "https://two.example"}]

    async def investigation(request):
        return Response(text="Two sources.", data={"sources": sources})

    monkeypatch.setattr(app.capabilities["research"], "handle", investigation)
    await app.ask("research dog pictures")
    await settle(app)

    brain.triage(mode="action", confidence=0.9, action_evidence=["open the second one"],
                objective=None, requires_tools=True, reason="explicit request")
    brain.understand(goal="open the second result", kind="navigate",
                     references=[{"text": "the second one", "kind": "result"}])
    brain.decide(action="respond", content="Opening the second result, sir.",
                 reason="resolved from context")
    result = await app.ask("Open the second one.")

    assert "second" in result.text.lower()
    assert not [e for e in app.bus.history
                if e.type == "tool.call" and e.payload["tool"] == "open_application"], \
        "no spurious application launch may be attempted for a bare reference"


async def test_open_the_second_one_with_no_antecedent_fails_safely(app, brain):
    """The same phrase, with nothing in context for "the second one" to mean.
    It must still never attempt to launch an application literally named
    "the second one" — the deterministic gateway's rejection (V1.3 F2) is
    unconditional, not dependent on context existing to resolve into."""
    brain.triage(mode="action", confidence=0.6, action_evidence=["open the second one"],
                objective=None, requires_tools=True, reason="ambiguous without context")
    brain.understand(goal="open the second one", kind="navigate",
                     references=[{"text": "the second one", "kind": "unknown"}],
                     confidence=Confidence.AMBIGUOUS, missing=["what 'the second one' refers to"])
    result = await app.ask("Open the second one.")

    assert not [e for e in app.bus.history
                if e.type == "tool.call" and e.payload["tool"] == "open_application"]
    # Fails safely: either a clarifying question, or an honest "couldn't" —
    # never a literal launch attempt.
    assert result.text.rstrip().endswith("?") or "open_application" not in result.text.lower()


async def test_open_bbc_co_uk_reaches_browser_verification_not_open_application(app, monkeypatch):
    """"Open BBC.co.uk." must quick-match browse_to, not open_application —
    and once it does, the existing browser verification (V1.3 §7) must
    actually run: a 404-shaped result is not reported as success."""
    async def not_found(args, ctx):
        return ToolResult(data={"url": "https://bbc.co.uk/x", "title": "404 — Page not found"},
                          summary="Opening BBC.co.uk.")

    tool = app.deps.registry.get("browse_to")
    monkeypatch.setattr(tool, "run", not_found)
    monkeypatch.setattr(tool.spec, "requires_macos", False)

    result = await app.ask("Open BBC.co.uk.")

    # The *initial* routing decision, published before verification can
    # trigger a hand-off to the agent — checked on the bus rather than on
    # result.decision, which by the end correctly reflects the recovery
    # attempt that follows a failed verification, not the original match.
    routes = [e for e in app.bus.history if e.type == "route"]
    assert routes[0].payload["kind"] == "tool" and routes[0].payload["name"] == "browse_to"
    assert routes[0].payload["path"] == "quick"
    assert not [e for e in app.bus.history
                if e.type == "tool.call" and e.payload["tool"] == "open_application"]
    assert "opening bbc.co.uk" not in result.text.lower(), \
        "a not-found landing must not be spoken as success"


async def test_deterministic_app_and_browser_commands_stay_quick(app, desktop):
    """The F2/F3 fix must not cost the genuinely deterministic cases
    anything: these still resolve in a single quick match, no model call."""
    for text, kind, name in (
        ("Open Safari.", "tool", "open_application"),
        ("Take a screenshot.", "tool", "capture_screen"),
        ("Go to bbc.co.uk.", "tool", "browse_to"),
    ):
        result = await app.ask(text)
        assert result.decision.path == "quick", f"{text!r} should still be a quick match"
        assert (result.decision.kind, result.decision.name) == (kind, name)


async def test_a_verification_failure_is_never_reported_as_success(app, brain, monkeypatch,
                                                                   desktop):
    """A tool call can return ok=True and still not be verified — the finding
    that composes the final answer must say so, not repeat the tool's own
    optimistic summary (the gap this closes: JARVIS must not be able to say
    "I opened it" from a result verification already rejected)."""
    _stub_tool(app, monkeypatch, "browse_to", ToolResult(
        data={"url": "https://bbc.example/missing", "title": "404 — Page not found"},
        summary="Opening bbc.example."))

    brain.understand(goal="open the BBC", kind="navigate", targets=["BBC"])
    brain.decide(action="tool_call", tool="browse_to", arguments={"url": "bbc.example/missing"},
                 reason="navigate to the BBC")
    await app.ask("open the BBC please")
    await settle(app)

    final_prompt = brain.prompts("final")[-1]
    assert "not verified" in final_prompt
    assert "→ ok:" not in final_prompt


async def test_a_quick_browser_match_that_lands_wrong_is_handed_to_the_agent(app, brain,
                                                                            monkeypatch, desktop):
    """"go to bbc.co.uk" is a deterministic quick match for browse_to — but a
    quick match is a certainty about which tool, not about the outcome, so
    verification still runs and a bad landing is handed to the agent rather
    than spoken as success (V1.3 §7)."""
    async def wrong_page(args, ctx):
        return ToolResult(data={"url": "https://bbc.co.uk/x", "title": "404 — Page not found"},
                          summary="Opening bbc.co.uk.")

    tool = app.deps.registry.get("browse_to")
    monkeypatch.setattr(tool, "run", wrong_page)
    monkeypatch.setattr(tool.spec, "requires_macos", False)

    brain.understand(goal="go to bbc.co.uk", kind="navigate", targets=["bbc.co.uk"])
    brain.decide(action="respond", content="That page wasn't found, sir — shall I search instead?",
                 reason="report the failure honestly")
    result = await app.ask("go to bbc.co.uk")
    await settle(app)

    assert "opening bbc.co.uk" not in result.text.lower()


# ===========================================================================
# V1.3 — context lifecycle (F1: cross-turn contamination)
#
# Two channels reach a chat/triage prompt, and they are not the same thing:
#
#   1. the *structured working set* (current objective, browser/email/
#      research state, recently referenced entities, recent actions) — this
#      is the confirmed bug. It described ongoing work as if it were still
#      relevant regardless of what the user just said, and (for
#      ContextBuilder.tool_results specifically) was never even turn-bounded.
#   2. the *raw last few turns of dialogue* — genuine, bounded conversational
#      memory ("what did we just say"), which is expected and is what keeps
#      chat feeling like a conversation rather than amnesia every message.
#      A topic from two messages ago legitimately showing up in that window
#      is not the bug; it decays with turn count on its own.
#
# The tests below isolate channel 1. Where a test needs to rule out channel 2
# as an alternate explanation, it runs enough unrelated filler turns first to
# push the marker outside both windows (triage: last 2 exchanges; chat: last
# 7 turns) — so a pass genuinely demonstrates the structured leak is gone,
# not merely that recency hasn't caught up yet.
# ===========================================================================
async def test_cross_turn_tool_results_do_not_leak_into_chat(app):
    """ContextBuilder used to carry a "Results just gathered" layer, fed once
    per tool call and never cleared between turns — a task's result could
    still be sitting in it arbitrarily later. It no longer carries tool
    results at all; see core/context.py."""
    await app.ask("what time is it")
    context_text = app.orchestrator.context.build("Hey Jarvis, how are you?")
    assert "just gathered" not in context_text.lower()
    assert not hasattr(app.orchestrator.context, "tool_results")
    assert not hasattr(app.orchestrator.context, "note_tool_result")


async def _fill_with_unrelated_chat(app, brain, count: int = 4) -> None:
    """Advance the conversation past both triage's and chat's raw-turn
    recency windows with ordinary, unrelated exchanges."""
    for i in range(count):
        brain.triage(mode="chat", confidence=0.9, action_evidence=[], objective=None,
                    requires_tools=False, reason="filler")
        app.models._providers["ollama"].responses.append(f"Noted, sir ({i}).")
        await app.ask(f"Just thinking out loud, number {i}.")


async def test_a_greeting_after_a_task_does_not_receive_the_task(app, brain, monkeypatch):
    """The exact scenario the V1.3 verification pass reproduced 6/6 times:
    a research task, then — well after it, past both recency windows — a
    plain greeting. Its prompt to the conversation model must carry none of
    the task: not the marker, not "current objective", not "recent
    actions"."""
    from jarvis.capabilities.base import Response

    MARKER = "NEVER_LEAK_THIS_TASK"

    async def investigation(request):
        return Response(text=f"Findings about {MARKER}.",
                        data={"sources": [{"title": f"A source about {MARKER}",
                                          "url": "https://a.example"}]})

    monkeypatch.setattr(app.capabilities["research"], "handle", investigation)
    await app.ask(f"research {MARKER}")
    await settle(app)

    state = app.orchestrator.state
    assert MARKER in state.research.query, "the marker should really be in state first"
    assert state.research.sources

    await _fill_with_unrelated_chat(app, brain)

    brain.triage(mode="chat", confidence=0.95, action_evidence=[], objective=None,
                requires_tools=False, reason="greeting")
    app.models._providers["ollama"].responses.append("I'm quite well, thank you, sir.")
    await app.ask("Hey Jarvis, how are you?")

    chat_prompt = brain.prompts("chat")[-1]
    assert MARKER not in chat_prompt
    assert "current objective" not in chat_prompt.lower()
    assert "recent actions" not in chat_prompt.lower()
    assert "recently referenced" not in chat_prompt.lower()


async def test_do_that_still_resolves_against_real_recent_state(app, brain, desktop):
    """Narrowing chat's and triage's context must not narrow what the ACTION
    side sees: Understanding and the decision loop still read
    ConversationState.describe_for_model() in full, unchanged — which is
    what lets "Do that." mean something concrete rather than nothing."""
    await app.ask("open Safari")                     # quick path, real state
    assert desktop["opened"] == ["Safari"]

    brain.triage(mode="action", confidence=0.9, action_evidence=["do that"],
                objective=None, requires_tools=True, reason="repeat the recent action")
    brain.understand(goal="repeat opening Safari", kind="open application",
                     targets=["Safari"], refines_previous=True)
    brain.decide(action="respond", content="Safari is already open, sir.",
                 reason="already done, visible in recent actions")
    result = await app.ask("Do that.")

    assert "Safari" in brain.prompts("understand")[-1], \
        "Understanding must still see the recent action in full"
    assert "safari" in result.text.lower()


async def test_triage_prompt_excludes_stale_task_state(app, brain, monkeypatch):
    """Prior task state must not reach triage's own prompt as if it were
    evidence for a fresh utterance's classification, once it is genuinely
    stale (past triage's own 2-exchange recency window, not merely the very
    next message). Triage still gets *some* conversational grounding
    ("now:") — just not the objective/research/recent-actions blocks that
    legitimately belong to reference resolution downstream."""
    from jarvis.capabilities.base import Response

    MARKER = "NEVER_LEAK_THIS_TASK"

    async def investigation(request):
        return Response(text=f"Findings about {MARKER}.",
                        data={"sources": [{"title": MARKER, "url": "https://a.example"}]})

    monkeypatch.setattr(app.capabilities["research"], "handle", investigation)
    await app.ask(f"research {MARKER}")
    await settle(app)
    assert MARKER in app.orchestrator.state.research.query

    await _fill_with_unrelated_chat(app, brain)

    brain.triage(mode="chat", confidence=0.9, action_evidence=[], objective=None,
                requires_tools=False, reason="just a greeting")
    app.models._providers["ollama"].responses.append("Quite well, sir.")
    await app.ask("Hey Jarvis, how are you?")

    triage_prompt = brain.prompts("triage")[-1]
    assert MARKER not in triage_prompt
    assert "current objective" not in triage_prompt.lower()
    assert "recent actions" not in triage_prompt.lower()
    assert "recently referenced" not in triage_prompt.lower()
    assert "now:" in triage_prompt.lower()


async def test_triage_prompt_instructs_against_stale_context_as_evidence():
    """Defence in depth alongside the narrowed context: the prompt itself
    tells the model a prior task is not evidence for a new utterance."""
    from jarvis.intelligence.triage import TRIAGE_PROMPT

    normalised = " ".join(TRIAGE_PROMPT.split())
    assert "not from earlier context" in normalised
    assert "never itself evidence" in normalised


async def test_describe_recent_conversation_excludes_the_working_set() -> None:
    """Unit-level regression for the underlying data-layer split: even a
    state with an active objective, live browser context and recent actions
    must not appear in the lean conversational view chat and triage now use.
    The previous bug was that ``_persona(with_context=True)`` called
    ``describe_for_model(include_turns=0)``, which still carried all of that
    despite excluding only the "conversation:" turns block."""
    from jarvis.intelligence.state import ConversationState

    state = ConversationState()
    state.begin_turn("research NEVER_LEAK_THIS_TASK")
    state.set_objective("research NEVER_LEAK_THIS_TASK")
    state.note_observation(
        "research_topic", {"query": "NEVER_LEAK_THIS_TASK"}, True,
        "Findings about NEVER_LEAK_THIS_TASK.",
        {"sources": [{"title": "NEVER_LEAK_THIS_TASK", "url": "https://a.example"}]},
        "research")

    full = state.describe_for_model(include_turns=0)
    lean = state.describe_recent_conversation(include_turns=0)

    assert "NEVER_LEAK_THIS_TASK" in full, "the full view should really carry it"
    assert "NEVER_LEAK_THIS_TASK" not in lean
    assert "current objective" not in lean.lower()
    assert "recent actions" not in lean.lower()


# ===========================================================================
# security — the intelligence layer must not weaken V1.1
# ===========================================================================
async def test_the_agent_cannot_bypass_the_high_risk_confirmation(app, brain, inbox,
                                                                  monkeypatch):
    """A model that decides to send email still meets the confirmation gate."""
    sent: list = []

    async def fake_send(self, draft):
        sent.append(draft)
        return True

    monkeypatch.setattr(AppleMailBackend, "send", fake_send)
    monkeypatch.setattr(app.deps.registry.get("send_email").spec, "requires_macos", False)
    # LOW and MEDIUM are auto-approved by the brain fixture; HIGH never is.
    app.config_store.update({"security": {"confirmation_timeout_s": 0.2}})

    brain.understand(goal="send a note to Ada", kind="send email", targets=["Ada"])
    brain.decide(action="tool_call", tool="send_email",
                 arguments={"to": ["ada@blake.example"], "subject": "Hello",
                            "body": "Just checking in."},
                 reason="the user asked for it to be sent")
    await app.ask("send Ada a note saying I am checking in")

    assert sent == [], "nothing may be sent without a confirmation"
    assert [e for e in app.bus.history if e.type == "confirm.request"], \
        "the HIGH-risk gate must have been reached"


async def test_a_declined_confirmation_is_reported_not_worked_around(app, brain, monkeypatch):
    """Recovery must treat "no" as an answer, never as an obstacle."""
    deletions = _stub_tool(app, monkeypatch, "delete_file",
                           ToolResult(summary="deleted", data={"path": "/tmp/x"}))
    app.config_store.update({"security": {"auto_approve": [],
                                          "confirmation_timeout_s": 0.15}})

    brain.understand(goal="get rid of the notes file", kind="delete file",
                     targets=["notes.txt"])
    brain.decide(action="tool_call", tool="delete_file", arguments={"path": "notes.txt"},
                 reason="the user asked for it")
    await app.ask("would you get rid of that notes file in my workspace")
    await settle(app)

    assert deletions == [], "the deletion must not have run"
    recoveries = [e.payload for e in app.bus.history
                  if e.type == "intelligence.trace" and e.payload.get("stage") == "recover"]
    assert recoveries and recoveries[-1]["strategy"] == "report"


# ===========================================================================
# context retention independent of which path answered
# ===========================================================================
async def test_the_fast_path_still_feeds_conversational_context(app, brain, desktop):
    """A quick-path launch must still be context for the next, slower turn."""
    result = await app.ask("open Safari")            # deterministic quick match
    assert result.decision.path == "quick"
    assert app.orchestrator.state.active_app == "Safari"
    assert app.orchestrator.state.browser.app == "Safari"

    brain.understand(goal="go to the BBC", kind="navigate", targets=["BBC"])
    brain.decide(action="tool_call", tool="browse_to", arguments={"url": "bbc.co.uk"},
                 reason="navigate")
    await app.ask("take me to the BBC website now")
    assert "Safari" in brain.prompts("understand")[-1], \
        "the quick path's result must reach the agent's context"


async def test_the_quick_path_is_not_sent_through_a_model(app, brain):
    """Latency proportional to complexity: a certainty costs no model call."""
    await app.ask("what time is it")
    assert brain.asked == [], "a quick match must not consult the reasoning model"


# ===========================================================================
# component behaviour
# ===========================================================================
async def test_tool_selection_shortlists_by_meaning_not_substring(app):
    catalog = ToolCatalog(app.deps.registry)
    objective = Objective(goal="how much of the processor is being used",
                          kind="inspect system", targets=["cpu"])
    names = [card.name for card in catalog.shortlist(objective)]
    assert "get_cpu" in names
    # "out" lives inside "output volume"; token matching must not rank it first.
    assert names.index("get_cpu") < names.index("set_volume") if "set_volume" in names else True


async def test_tool_selection_follows_live_context(app):
    """Having a page open should make page tools more selectable, not less."""
    catalog = ToolCatalog(app.deps.registry)
    state = ConversationState()
    objective = Objective(goal="summarise it", kind="read")
    before = [c.name for c in catalog.shortlist(objective, state)]
    assert "list_browser_tabs" not in before[:4]

    state.note_observation("browse_to", {"url": "https://bbc.co.uk"}, True, "Opening bbc.",
                           {"url": "https://bbc.co.uk", "title": "BBC"}, "browser")
    after = [c.name for c in catalog.shortlist(objective, state)]
    assert after.index("get_current_page") < before.index("get_current_page") or \
        after.index("list_browser_tabs") < before.index("list_browser_tabs")


async def test_argument_extraction_is_validated_before_execution(app):
    catalog = ToolCatalog(app.deps.registry)
    ok, problem, _ = catalog.validate_call("browse_to", {"url": "bbc.co.uk"})
    assert ok and not problem
    ok, problem, _ = catalog.validate_call("click_element", {})
    assert not ok and "label" in problem
    ok, problem, _ = catalog.validate_call("no_such_tool", {})
    assert not ok and "no tool" in problem


async def test_entity_resolution_prefers_the_distinguishing_word(app):
    state = ConversationState()
    state.begin_turn("check my mail")
    state.note_observation(
        "check_email", {}, True, "3 messages",
        {"unread": 3, "messages": [
            {"id": "1", "sender": "Tom Blake <tom@blake.example>", "subject": "Lunch"},
            {"id": "2", "sender": "Ada Blake <ada@blake.example>", "subject": "The roof"},
        ]}, "email")
    resolver = ReferenceResolver()

    ada = resolver.resolve("the one from Ada", state, "email")
    assert ada.resolved, "a discriminating word should pick the message out"
    assert "ada" in str(ada.entity.extra.get("sender", "")).lower()
    assert ada.label == "The roof"
    # A bare pronoun with two equally good candidates is ambiguous, not a guess.
    assert resolver.resolve("him", state, "person").ambiguous


async def test_entity_resolution_handles_ordinals(app):
    state = ConversationState()
    state.begin_turn("research cells")
    state.note_observation("research_topic", {"query": "cells"}, True, "done", {
        "sources": [{"title": "First source", "url": "https://one.example"},
                    {"title": "Second source", "url": "https://two.example"}]}, "research")
    resolver = ReferenceResolver()
    second = resolver.resolve("the second one", state, "result")
    assert second.resolved and "two.example" in str(second.value)


async def test_verification_rejects_a_not_found_page(app):
    verifier = Verifier(app.deps)
    result = ToolResult(data={"url": "https://bbc.example/x", "title": "404 — Page not found"},
                        summary="Opening bbc.example.")
    verdict = await verifier.verify("browse_to", {"url": "bbc.example/x"}, result,
                                    Objective(goal="open the BBC", targets=["BBC"]),
                                    ConversationState())
    assert verdict.verified is False and "not-found" in verdict.problem


async def test_verification_rejects_a_hedging_vision_answer(app):
    verifier = Verifier(app.deps)
    result = ToolResult(data={"answer": "I can't see any clear text in this image."},
                        summary="Captured.")
    verdict = await verifier.verify("analyse_screen", {}, result,
                                    Objective(goal="read the error"), ConversationState())
    assert verdict.verified is False and verdict.problem


async def test_verification_notices_an_application_that_did_not_launch(app, monkeypatch):
    async def never_running(self, name):
        return False

    monkeypatch.setattr(type(app.deps.controller), "is_app_running", never_running)
    verifier = Verifier(app.deps)
    verdict = await verifier.verify(
        "open_application", {"name": "Xcode"},
        ToolResult(data={"application": "Xcode"}, summary="Opening Xcode."),
        Objective(goal="open Xcode"), ConversationState())
    assert verdict.verified is False and "Xcode" in verdict.problem


async def test_multi_step_work_runs_as_an_errand_and_simple_work_does_not(app, brain, monkeypatch):
    """A multi-step objective becomes a background task with an errand-sized
    budget; a simple one is done in the turn, with no task at all."""
    _stub_tool(app, monkeypatch, "research_topic",
               ToolResult(data={"sources": [{"title": "T", "url": "https://x.example"}]},
                          summary="Compared both options across four sources."))
    brain.understand(goal="compare two things", kind="research",
                     complexity=Complexity.MULTI_STEP)
    brain.decide(action="tool_call", tool="research_topic", arguments={"query": "a vs b"},
                 reason="gather")
    brain.decide(action="respond", content="The first suits you better, sir.",
                 evidence=["Compared both options across four sources"])
    result = await app.ask("which of the two leading options would suit me better")
    assert result.task_id, "multi-step work should run as a background task"
    await settle(app)
    task = app.tasks.get(result.task_id)
    assert task.kind == "automation" and task.status == "succeeded"

    brain.queues.clear()
    brain.asked.clear()
    brain.understand(goal="what time is it in Tokyo", kind="inspect system",
                     complexity=Complexity.SIMPLE)
    brain.decide(action="tool_call", tool="get_time", arguments={}, reason="read the clock")
    result = await app.ask("and what would that be in Tokyo right now")
    assert result.task_id is None, "a simple request is answered in the turn"


async def test_trivial_conversation_never_reaches_a_tool(app, brain):
    brain.understand(goal="explain lighthouses", kind="chat", needs_tools=False,
                     complexity=Complexity.TRIVIAL)
    app.models._providers["ollama"].responses.append("They were automated gradually, sir.")
    result = await app.ask("tell me something interesting about lighthouses")
    assert "automated" in result.text
    assert not [e for e in app.bus.history if e.type == "tool.call"]


async def test_an_impossible_request_is_declined_honestly(app, brain):
    brain.understand(goal="travel back in time", confidence=Confidence.IMPOSSIBLE)
    result = await app.ask("take me back to 1962 for the afternoon")
    assert "isn't something I can do" in result.text
    assert not [e for e in app.bus.history if e.type == "tool.call"]


async def test_a_failed_argument_check_is_fed_back_rather_than_crashing(app, brain, desktop,
                                                                      monkeypatch):
    """A bad call is an observation the loop can correct, not an exception."""
    clicks = _stub_tool(app, monkeypatch, "click_element",
                        ToolResult(data={"label": "Search"}, summary="Clicked Search."))
    brain.understand(goal="click something", kind="click")
    brain.script("decide",
                 {"action": "tool_call", "tool": "click_element", "arguments": {}},
                 {"action": "tool_call", "tool": "click_element",
                  "arguments": {"label": "Search"}},
                 {"action": "complete", "reason": "done"})
    await app.ask("click on the search box for me")

    steps = [e.payload for e in app.bus.history
             if e.type == "intelligence.trace" and e.payload.get("stage") == "step"]
    assert steps and "rejected" in steps[0]["note"], "the bad call should be fed back"
    assert clicks == [{"label": "Search", "app": ""}], "and the corrected call should then run"


async def test_the_trace_reports_stages_without_exposing_reasoning(app, brain, desktop):
    brain.understand(goal="open Safari", kind="open application", targets=["Safari"])
    brain.decide(action="tool_call", tool="open_application", arguments={"name": "Safari"},
                 reason="the user named it")
    await app.ask("get Safari up on screen for me")

    stages = [e.payload["stage"] for e in app.bus.history if e.type == "intelligence.trace"]
    assert {"intent", "decision", "result", "verify", "complete"} <= set(stages)
    complete = [e.payload for e in app.bus.history
                if e.type == "intelligence.trace" and e.payload["stage"] == "complete"][-1]
    assert complete["tool_calls"] == 1
    assert complete["model_calls"] >= 2
    assert "elapsed_ms" in complete


async def test_sensitive_arguments_are_redacted_in_the_trace(app, brain, inbox):
    brain.understand(goal="draft a note", kind="send email")
    brain.decide(action="tool_call", tool="draft_email",
                 arguments={"to": ["ada@blake.example"], "subject": "Hi",
                            "body": "Something private and personal."},
                 reason="draft it")
    await app.ask("draft a short note to Ada for me")
    decisions = [e.payload for e in app.bus.history
                 if e.type == "intelligence.trace" and e.payload["stage"] == "decision"]
    assert decisions
    assert "private" not in json.dumps(decisions[-1]["arguments"])


async def test_turning_intelligence_off_restores_the_v1_1_path(app, brain, desktop):
    app.config_store.update({"intelligence": {"enabled": False}})
    await app.ask("take me to the BBC website now")
    assert not [e for e in app.bus.history if e.type == "intelligence.trace"]


async def test_an_agent_routed_multi_step_request_still_completes(app, brain, monkeypatch):
    """A request that reaches the agent (not a quick-matched capability) now
    runs to completion rather than being acknowledged and backgrounded from a
    guessed capability (V1.3): that guess came from the same heuristic/1B
    classification stage the benchmark showed was unreliable for deciding
    chat vs. action, and backgrounding *before* triage even ran meant
    sometimes acknowledging work that turned out not to be needed at all.
    Quick-matched capabilities (research, email, calendar, diagnostics) keep
    their own immediate acknowledgement — see test_orchestrator.py.
    """
    tool = app.deps.registry.get("research_topic")

    async def fast(args, ctx):
        return ToolResult(data={"sources": [{"title": "T", "url": "https://x.example"}]},
                          summary="Done.")

    monkeypatch.setattr(tool, "run", fast)
    brain.understand(goal="research widgets", kind="research",
                     complexity=Complexity.MULTI_STEP)
    brain.decide(action="tool_call", tool="research_topic", arguments={"query": "widgets"},
                 reason="investigate")

    result = await app.ask("tell me something about the current state of widget manufacturing")
    await settle(app)
    assert result.text, "the request should still produce a real answer"


# ===========================================================================
# the acceptance conversation (V1.2 brief, §30)
# ===========================================================================
async def test_the_acceptance_conversation_holds_together(app, brain, inbox, desktop,
                                                          monkeypatch):
    """Fifteen turns, crossing every path, with nothing lost in between.

    This is the conversation the brief asks for, run end to end. It proves the
    *architecture* carries context, planning, correction and confirmation across
    a whole conversation — not that any particular local model would make these
    decisions. Model quality is measured separately, on a Mac, with Ollama
    running; see the limitations section of the README.
    """
    from jarvis.capabilities.base import Response

    drafts = _stub_tool(app, monkeypatch, "draft_email",
                        ToolResult(data={"to": ["tom@blake.example"], "subject": "Re: Sunday",
                                         "body": "I'll call you tonight."},
                                   summary="Draft ready.",
                                   display={"kind": "draft", "to": ["tom@blake.example"],
                                            "subject": "Re: Sunday",
                                            "body": "I'll call you tonight."}))
    sent: list = []

    async def fake_send(self, draft):
        sent.append(draft)
        return True

    monkeypatch.setattr(AppleMailBackend, "send", fake_send)
    monkeypatch.setattr(app.deps.registry.get("send_email").spec, "requires_macos", False)

    async def investigation(request):
        return Response(text="Cells specialise for a job.", spoken="Four sources compared.",
                        data={"sources": [
                            {"index": 1, "title": "Stem cells in therapy",
                             "url": "https://a.example", "snippet": "Bone-marrow transplants."},
                            {"index": 2, "title": "Neurons", "url": "https://b.example",
                             "snippet": "Signal conduction."}]})

    monkeypatch.setattr(app.capabilities["research"], "handle", investigation)
    _stub_tool(app, monkeypatch, "analyse_screen", ToolResult(
        data={"answer": "A Safari window showing a search bar and a Sign in button.",
              "path": "/tmp/shot.png"},
        summary="A Safari window showing a search bar and a Sign in button."))
    typed = _stub_tool(app, monkeypatch, "type_text",
                       ToolResult(data={"text": "specialised cells"}, summary="Typed it."))
    keys = _stub_tool(app, monkeypatch, "press_key",
                      ToolResult(data={"key": "return"}, summary="Pressed return."))
    pages = _stub_tool(app, monkeypatch, "browse_to",
                       ToolResult(data={"url": "https://bbc.co.uk/science", "title": "BBC Science"},
                                  summary="Opening bbc.co.uk."))

    async def turn(text: str, understanding: dict | None = None, decision: dict | None = None):
        if understanding is not None:
            brain.understand(**understanding)
        if decision is not None:
            brain.decide(**decision)
        result = await app.ask(text)
        await settle(app)
        assert result.text or result.task_id, f"{text!r} produced no reply at all"
        return result

    state = app.orchestrator.state

    await turn("Hey Jarvis.")
    await turn("How are you?",
               {"goal": "exchange pleasantries", "kind": "chat", "needs_tools": False,
                "complexity": Complexity.TRIVIAL})

    await turn("Check my new emails.",
               {"goal": "check email", "kind": "read email"},
               {"action": "tool_call", "tool": "check_email", "arguments": {"limit": 8},
                "reason": "read the inbox"})
    assert len(state.email.messages) == 3, "the inbox should be in context"

    await turn("Anything from my brother?",
               {"goal": "find mail from the user's brother", "kind": "read email",
                "targets": ["brother"], "refines_previous": True,
                "references": [{"text": "my brother", "kind": "person"}]},
               {"action": "respond", "content": "Tom wrote about Sunday lunch, sir.",
                "reason": "already in context"})

    await turn("Draft a reply saying I'll call him tonight.",
               {"goal": "draft a reply to Tom", "kind": "send email", "targets": ["Tom"],
                "references": [{"text": "him", "kind": "person"}]},
               {"action": "tool_call", "tool": "draft_email",
                "arguments": {"to": ["tom@blake.example"], "subject": "Re: Sunday",
                              "body": "I'll call you tonight."},
                "reason": "the user dictated a reply"})
    assert drafts, "a draft should have been prepared"
    assert sent == [], "nothing may be sent without confirmation"

    await turn("Don't send it yet.",
               {"goal": "hold the draft", "kind": "chat", "needs_tools": False,
                "complexity": Complexity.TRIVIAL, "refines_previous": True})
    assert sent == [], "and still nothing sent"

    await turn("Open Safari.")                       # quick path
    assert state.browser.app == "Safari", "the open browser should be context"

    await turn("Research specialised cells.")        # quick path
    assert len(state.research.sources) == 2, "the sources should be context"

    # "Focus on…" matches the bring-an-application-forward pattern, there is no
    # such application, and the turn is handed to the agent instead of dying.
    await turn("Focus on the ones relevant to medicine.",
               {"goal": "research specialised cells", "kind": "research",
                "constraints": ["relevant to medicine"], "refines_previous": True,
                "references": [{"text": "the ones", "kind": "result"}]},
               {"action": "respond", "content": "Stem cells, sir.", "reason": "in context"})

    await turn("Compare the most important ones.")   # quick path

    await turn("What's currently on my screen?",
               {"goal": "describe the screen", "kind": "inspect screen"},
               {"action": "tool_call", "tool": "analyse_screen",
                "arguments": {"question": "Describe what is on this screen."},
                "reason": "the user asked"})
    assert state.screen.fresh, "the screen should be context"

    await turn("Write 'specialised cells' into the search bar.",
               {"goal": "type into the search bar", "kind": "type text",
                "targets": ["specialised cells"],
                "references": [{"text": "the search bar", "kind": "screen_element"}]},
               {"action": "tool_call", "tool": "type_text",
                "arguments": {"text": "specialised cells"}, "reason": "type it"})
    assert typed == [{"text": "specialised cells", "press_return": False}]

    await turn("Search it.",
               {"goal": "submit the search", "kind": "press key", "refines_previous": True,
                "references": [{"text": "it", "kind": "screen_element"}]},
               {"action": "tool_call", "tool": "press_key",
                "arguments": {"key": "return"}, "reason": "submit"})
    assert keys and keys[0]["key"] == "return"

    await turn("That page isn't right. Find the correct BBC page.",
               {"goal": "open the correct BBC page", "kind": "navigate", "targets": ["BBC"],
                "is_correction": True},
               {"action": "tool_call", "tool": "browse_to",
                "arguments": {"url": "bbc.co.uk/science"}, "reason": "the right address"})
    assert pages, "the correction should have produced a navigation"

    # Nothing consequential happened without being asked for.
    assert sent == [], "the draft was never sent"
    # And the whole conversation is still one coherent state.
    assert state.turn == 14, "one state turn per exchange, all the way through"
    assert state.email.messages and state.research.sources and state.browser.url


# ===========================================================================
# V1.3 runtime duplication: a real macOS report of responses spoken twice
# and a browser action apparently running several times for what the route
# log showed as a single decision. Root causes and fixes:
#
#   - orchestrator._respond() queued a streamed answer for speech a second
#     time, unconditionally, on top of _stream_sink's own sentence-by-
#     sentence queueing during generation. Fixed with the `not
#     already_streamed` guard.
#   - VoiceManager.start() checked "already listening" *after* awaiting
#     probe(), so two overlapping calls could both pass the check before
#     either had set _listen_task, each creating its own _listen_loop.
#     Fixed by moving the check inside a lock, before the await.
#
# Each test below targets the actual layer the bug lived in — the tool's
# own run(), the TTS engine's own speak() — rather than a higher-level
# proxy for it.
# ===========================================================================
class _RecordingTTS:
    """Records exactly what was spoken, in order — nothing more."""

    name = "recording"

    def __init__(self):
        self.spoken: list[str] = []

    async def available(self):
        return True, "ok"

    async def speak(self, text):
        self.spoken.append(text)
        return True

    async def stop(self):
        return True

    async def voices(self):
        return []


async def test_a_streamed_chat_response_is_spoken_only_once(app, brain, config):
    """The exact scenario reported: a plain chat reply, generated through
    the streaming path every agent-routed turn now uses, audibly spoken
    twice."""
    from jarvis.voice.manager import VoiceManager

    config.voice.enabled = True
    manager = VoiceManager(config, app.bus, app.telemetry)
    manager.tts = _RecordingTTS()
    app.orchestrator.voice = manager

    brain.triage(mode="chat", confidence=0.95, action_evidence=[], objective=None,
                requires_tools=False, reason="greeting")
    app.models._providers["ollama"].responses.append("I'm quite well, thank you, sir.")
    result = await app.ask("Hey Jarvis, how are you?")
    await asyncio.sleep(0.1)  # let the speech queue drain

    assert result.text == "I'm quite well, thank you, sir."
    occurrences = sum(1 for s in manager.tts.spoken if "quite well" in s)
    assert occurrences == 1, f"the response was spoken {occurrences} times: {manager.tts.spoken!r}"


async def test_a_quick_path_reply_is_still_spoken_exactly_once(app, config):
    """The fix must not overcorrect: a reply that was never streamed (the
    quick path never streams) still needs to be spoken — exactly once,
    same as before the fix."""
    from jarvis.voice.manager import VoiceManager

    config.voice.enabled = True
    manager = VoiceManager(config, app.bus, app.telemetry)
    manager.tts = _RecordingTTS()
    app.orchestrator.voice = manager

    result = await app.ask("what time is it")
    await asyncio.sleep(0.1)

    assert result.decision.path == "quick"
    assert len(manager.tts.spoken) == 1, \
        f"expected exactly one spoken reply, got {manager.tts.spoken!r}"


async def test_a_backgrounded_capability_reply_is_still_spoken_exactly_once(app, config,
                                                                            monkeypatch):
    """Nor should it affect background-task delivery, which never streams
    speech during execution and relies entirely on _respond() at the end —
    exactly the case the guard must leave alone. A long-running capability
    legitimately speaks *two different* things (an immediate acknowledgement,
    then the result once it's ready) — that's not the duplicate-speech bug;
    the point here is that the final result itself isn't also duplicated."""
    from jarvis.voice.manager import VoiceManager

    config.voice.enabled = True
    manager = VoiceManager(config, app.bus, app.telemetry)
    manager.tts = _RecordingTTS()
    app.orchestrator.voice = manager

    async def fake_run(args, ctx):
        return ToolResult(data={"findings": []}, summary="Nothing wrong found.")

    monkeypatch.setattr(app.deps.registry.get("run_diagnostics"), "run", fake_run)
    monkeypatch.setattr(app.deps.registry.get("run_diagnostics").spec, "requires_macos", False)

    await app.ask("why is my mac slow?")
    await settle(app)
    await asyncio.sleep(0.1)

    healthy = [s for s in manager.tts.spoken if "wrong" in s.lower() or "healthy" in s.lower()
              or "nothing" in s.lower()]
    assert len(healthy) == 1, \
        f"the final result should be spoken exactly once, got {manager.tts.spoken!r}"


async def test_open_bbc_co_uk_executes_the_browser_action_exactly_once(app, monkeypatch):
    """The tool's own run() — the one place the side effect actually
    happens — must be called exactly once for one quick-matched request."""
    calls: list[dict] = []

    async def open_once(args, ctx):
        calls.append(dict(args))
        return ToolResult(data={"url": "https://bbc.co.uk"}, summary="Opening bbc.co.uk.")

    tool = app.deps.registry.get("browse_to")
    monkeypatch.setattr(tool, "run", open_once)
    monkeypatch.setattr(tool.spec, "requires_macos", False)

    result = await app.ask("Open BBC.co.uk.")

    assert result.decision.name == "browse_to"
    assert len(calls) == 1, f"browse_to.run() was called {len(calls)} times: {calls!r}"


async def test_open_safari_executes_the_launch_exactly_once(app, monkeypatch):
    calls: list[dict] = []

    async def open_once(args, ctx):
        calls.append(dict(args))
        return ToolResult(data={"application": "Safari"}, summary="Opening Safari.")

    tool = app.deps.registry.get("open_application")
    monkeypatch.setattr(tool, "run", open_once)
    monkeypatch.setattr(tool.spec, "requires_macos", False)

    result = await app.ask("Open Safari.")

    assert result.decision.name == "open_application"
    assert len(calls) == 1, f"open_application.run() was called {len(calls)} times: {calls!r}"


async def test_contextual_open_the_second_one_executes_at_most_once(app, brain, monkeypatch):
    """"Open the second one." resolving to a real tool call — the F2 fix
    (falling through to the semantic path instead of a literal quick match)
    must not itself introduce any new way to run a tool twice."""
    from jarvis.capabilities.base import Response

    sources = [{"index": 1, "title": "First result", "url": "https://one.example"},
              {"index": 2, "title": "Second result", "url": "https://two.example"}]

    async def investigation(request):
        return Response(text="Two sources.", data={"sources": sources})

    monkeypatch.setattr(app.capabilities["research"], "handle", investigation)
    await app.ask("research dog pictures")
    await settle(app)

    calls: list[dict] = []

    async def open_once(args, ctx):
        calls.append(dict(args))
        return ToolResult(data={"url": "https://two.example"}, summary="Opening it.")

    tool = app.deps.registry.get("browse_to")
    monkeypatch.setattr(tool, "run", open_once)
    monkeypatch.setattr(tool.spec, "requires_macos", False)

    brain.triage(mode="action", confidence=0.9, action_evidence=["open the second one"],
                objective=None, requires_tools=True, reason="explicit request")
    brain.understand(goal="open the second result", kind="navigate",
                     references=[{"text": "the second one", "kind": "result"}])
    brain.decide(action="tool_call", tool="browse_to", arguments={"url": "https://two.example"},
                 reason="resolved from context")
    await app.ask("Open the second one.")

    assert len(calls) == 1, f"browse_to.run() was called {len(calls)} times: {calls!r}"


# ===========================================================================
# The automation handoff (Part 4): a multi-step app/web objective must never
# be run through this file's short decide/verify loop — it hands off before
# the shortlist is even built, and the orchestrator re-routes it through the
# same backgrounding machinery a quick-matched capability gets.
# ===========================================================================
async def test_a_multi_step_automation_objective_hands_off_before_shortlisting(app, brain,
                                                                               monkeypatch):
    """The loop must return outcome.handoff without ever calling
    catalog.shortlist() — that is the concrete claim in agent.py's comment
    ("no model spend is wasted on a turn that's about to be re-routed")."""
    from jarvis.intelligence.agent import IntelligenceAgent

    agent = IntelligenceAgent(app.deps, app.deps.models, app.orchestrator.state)
    shortlisted = []
    monkeypatch.setattr(agent.catalog, "shortlist",
                        lambda *a, **k: shortlisted.append(1) or [])

    brain.understand(goal="find the best value ESP-32 two-pack and add it to my basket",
                     kind="automation", complexity=Complexity.MULTI_STEP, needs_tools=True)
    ctx = app.deps.tool_context()
    outcome = await agent.run("find me a good esp32 board and add it to my basket", ctx)

    assert outcome.handoff == "automation"
    assert outcome.objective is not None and outcome.objective.kind == "automation"
    assert not shortlisted, "shortlist() must never run for a handed-off objective"


async def test_a_single_step_click_does_not_hand_off(app, brain):
    """Only genuinely multi-step automation objectives hand off — an
    ordinary single click keeps going through the small loop exactly as
    before, unaffected by this feature."""
    from jarvis.intelligence.agent import IntelligenceAgent

    agent = IntelligenceAgent(app.deps, app.deps.models, app.orchestrator.state)
    brain.understand(goal="click the search bar", kind="click", complexity=Complexity.SIMPLE,
                     needs_tools=True)
    brain.decide(action="tool_call", tool="click_element", arguments={"label": "search bar"},
                reason="click it")
    ctx = app.deps.tool_context()
    outcome = await agent.run("click the search bar", ctx)
    assert outcome.handoff is None


async def test_any_multi_step_objective_hands_off_whatever_its_kind(app, brain):
    """Objective.kind is free-form; an errand is an errand whether the
    interpreter called it "automation", "research" or "send email"."""
    from jarvis.intelligence.agent import IntelligenceAgent

    agent = IntelligenceAgent(app.deps, app.deps.models, app.orchestrator.state)
    for kind in (" Automation ", "research", "send email"):
        brain.understand(goal="find the best value ESP-32 two-pack and add it to my basket",
                         kind=kind, complexity=Complexity.MULTI_STEP, needs_tools=True)
        ctx = app.deps.tool_context()
        outcome = await agent.run("find me a good esp32 board and add it to my basket", ctx)
        assert outcome.handoff == "automation", f"kind={kind!r} should still hand off"


async def test_automation_is_disabled_by_the_capability_flag(app, brain):
    """caps.automation=False must fall through to the ordinary loop, the
    same escape hatch every other capability flag gets."""
    from jarvis.intelligence.agent import IntelligenceAgent

    app.config_store.update({"capabilities": {"automation": False}})
    agent = IntelligenceAgent(app.deps, app.deps.models, app.orchestrator.state)
    brain.understand(goal="find the best value ESP-32 two-pack and add it to my basket",
                     kind="automation", complexity=Complexity.MULTI_STEP, needs_tools=True)
    brain.decide(action="complete", reason="nothing to do")
    ctx = app.deps.tool_context()
    outcome = await agent.run("find me a good esp32 board", ctx)
    assert outcome.handoff is None


async def test_stop_cancels_a_handed_off_automation_task(app, brain, monkeypatch):
    """The concrete proof the cancel_event gap is closed: a turn that started
    in the agent loop and got handed off can now be stopped, which was
    structurally impossible before this feature (task=None on the only
    call site of _run_agent meant ctx.cancelled() could never be true)."""
    from jarvis.capabilities.base import Response

    cancelled = asyncio.Event()

    async def slow_handle(request):
        for _ in range(300):
            if request.ctx.cancelled():
                cancelled.set()
                return Response(text="stopped")
            await asyncio.sleep(0.01)
        return Response(text="completed")  # pragma: no cover - only on a real timeout

    monkeypatch.setattr(app.capabilities["automation"], "handle", slow_handle)
    brain.understand(goal="find the best value ESP-32 two-pack and add it to my basket",
                     kind="automation", complexity=Complexity.MULTI_STEP, needs_tools=True)
    result = await app.ask("find me a good esp32 board and add it to my basket")
    assert result.task_id
    await asyncio.sleep(0.1)

    await app.ask("stop")
    await asyncio.sleep(0.2)
    assert cancelled.is_set()


async def test_stop_reaches_an_action_already_under_way_in_the_foreground(app, brain, monkeypatch):
    """A foreground turn has no Task, but "stop" must still reach it: the
    operator checks the turn's own cancel token between actions."""
    started = asyncio.Event()
    calls: list[int] = []

    async def slow_clock(args, ctx):
        calls.append(1)
        started.set()
        await asyncio.sleep(0.3)
        return ToolResult(data={"time": "noon"}, summary="It is noon.")

    tool = app.deps.registry.get("get_time")
    monkeypatch.setattr(tool, "run", slow_clock)
    brain.understand(goal="tell the time twice", kind="inspect system")
    brain.decide(action="tool_call", tool="get_time", arguments={})
    brain.decide(action="tool_call", tool="get_time", arguments={})

    turn = asyncio.create_task(app.ask("what's the time, and then again"))
    await asyncio.wait_for(started.wait(), timeout=5)
    stop = await app.ask("stop")
    result = await asyncio.wait_for(turn, timeout=5)

    brain.queues.clear()
    assert stop.text, "the stop is acknowledged"
    assert calls == [1], "nothing more may run once the user has said stop"
    from jarvis.core.personality import CANCELLED

    assert result.text in CANCELLED


async def test_an_errand_that_asks_a_question_resumes_with_the_answer(app, brain, monkeypatch):
    """The background operator stops on a question; the user's next words
    are its answer, and the errand picks up with it."""
    brain.understand(goal="buy a memory card", kind="shopping",
                     complexity=Complexity.MULTI_STEP, needs_tools=True)
    brain.decide(action="clarify", question="Which size — 32 GB or 64 GB?")
    first = await app.ask("buy me a memory card")
    await settle(app)
    delivered = [e.payload["text"] for e in app.bus.history
                 if e.type == "assistant.message" and e.payload.get("task_id") == first.task_id]
    assert delivered[-1] == "Which size — 32 GB or 64 GB?"
    pending = app.orchestrator.state.pending_clarification
    assert pending is not None and pending.objective_goal == "buy a memory card"

    brain.understand(goal="64", kind="shopping", complexity=Complexity.MULTI_STEP, needs_tools=True)
    brain.decide(action="give_up", reason="enough for the test")
    await app.ask("the 64 one")
    await settle(app)
    resumed = brain.prompts("decide")[-1]
    assert "Task: buy a memory card" in resumed
    assert "Which size — 32 GB or 64 GB? the 64 one" in resumed


async def test_no_utterance_is_spoken_twice_across_a_full_automation_turn(app, brain, fake_provider,
                                                                          monkeypatch):
    """End-to-end proof against the exact bug class fixed earlier this
    session (a streamed reply re-enqueued in full — see
    orchestrator.py's _respond `already_streamed` guard and its comment
    about a response being "audibly spoken twice"): drive a real
    app.ask() through triage -> understanding -> the agent handoff ->
    the orchestrator's background machinery -> a real
    AutomationCapability.handle() call (not a directly-constructed one),
    with a fake voice manager wired into both Deps.voice and
    Orchestrator.voice exactly as core/app.py wires the real one, and
    assert every utterance the fake voice records is spoken exactly once.
    """
    class _FakeVoice:
        def __init__(self):
            self.spoken: list[str] = []

        def enqueue(self, text):
            self.spoken.append(text)

        async def stop_speaking(self):
            return False

    voice = _FakeVoice()
    app.deps.voice = voice
    app.orchestrator.voice = voice
    app.config.voice.enabled = True  # ActionNarrator checks this live; avoid a config_store
                                     # .update() here since that rebuilds the registry and
                                     # would discard the tool stub installed below.

    app.config_store.update({"security": {"auto_approve": ["low", "medium"]},
                             "automation": {"narration_min_gap_s": 0.0}})

    calls = _stub_tool(app, monkeypatch, "browse_to",
                       ToolResult(data={"url": "https://x.example"},
                                  summary="Opened x.example in the browser."))
    brain.understand(goal="open x.example", kind="automation", complexity=Complexity.MULTI_STEP,
                     needs_tools=True)
    brain.decide(action="tool_call", tool="browse_to", arguments={"url": "https://x.example"})
    brain.decide(action="respond", content="Opened the site for you.",
                 evidence=["Opened x.example in the browser"])

    result = await app.ask("open x.example please, step by step")
    assert result.task_id
    await settle(app)

    assert len(calls) == 1, "the tool itself must not be invoked more than once"
    assert voice.spoken, "narration/delivery should have spoken something"
    assert len(voice.spoken) == len(set(voice.spoken)), (
        f"an utterance was spoken more than once: {voice.spoken}")
    # The final summary is the one utterance that must appear, and appear
    # only via the single _deliver_background -> _respond call path.
    assert voice.spoken.count("Opened the site for you.") == 1


async def test_download_file_works_as_a_standalone_tool_through_the_small_loop(app, brain,
                                                                               monkeypatch):
    """download_file doesn't need the automation capability for a simple,
    single-step "download this" request — it's an ordinary tool reachable
    through the small agent loop like any other (see its module docstring:
    "a user could ask 'download the latest Python installer' today via the
    small agent loop calling download_file directly as a single tool call,
    no capability needed")."""
    calls = _stub_tool(app, monkeypatch, "download_file",
                       ToolResult(data={"path": "/tmp/python.pkg", "bytes": 100},
                                  summary="Downloaded python.pkg (100 bytes)."))
    brain.understand(goal="download the python installer", kind="download",
                     complexity=Complexity.SIMPLE, needs_tools=True)
    brain.decide(action="tool_call", tool="download_file",
                 arguments={"url": "https://python.org/installer.pkg"}, reason="download it")
    await app.ask("download the latest python installer")
    assert len(calls) == 1
    assert calls[0]["url"] == "https://python.org/installer.pkg"


async def test_declining_the_automation_start_confirmation_still_gets_a_reply(
    app, brain, fake_provider, monkeypatch
):
    """A real gap found while auditing narration for double-speech: handle()
    calls permissions.require() directly for its one start confirmation
    (nothing else in any capability does — every other gated call goes
    through the tool registry, which already converts a decline into a
    ToolResult rather than a raised exception). Because this capability is
    always long_running, that call runs inside a background Task
    (core/orchestrator.py: _run_in_background), so an uncaught
    ConfirmationDeclined used to be swallowed by TaskManager._run as a bare
    failure — the user heard the "I'm on it" acknowledgement and then
    silence, forever. Proven end-to-end: the brain fixture pre-approves
    medium risk for convenience, so it's turned back off here to force a
    real confirmation that then times out unanswered."""
    class _FakeVoice:
        def __init__(self):
            self.spoken: list[str] = []

        def enqueue(self, text):
            self.spoken.append(text)

        async def stop_speaking(self):
            return False

    voice = _FakeVoice()
    app.deps.voice = voice
    app.orchestrator.voice = voice
    app.config.voice.enabled = True

    app.config_store.update({"security": {"auto_approve": ["low"], "confirmation_timeout_s": 0.15,
                                          "autonomy": "confirm_start"}})

    calls = _stub_tool(app, monkeypatch, "browse_to",
                       ToolResult(data={"url": "https://x.example"}, summary="Opened it."))
    brain.understand(goal="open x.example", kind="automation", complexity=Complexity.MULTI_STEP,
                     needs_tools=True)

    result = await app.ask("open x.example please, step by step")
    assert result.task_id
    await settle(app)

    assert calls == [], "declining the start must stop before any tool runs"
    task = app.tasks.get(result.task_id)
    assert task.status == "succeeded", "a decline is an answer, not a task failure"
    assert len(voice.spoken) == 2, (
        f"expected the acknowledgement plus the decline reply, got: {voice.spoken}")
    assert "left it" in voice.spoken[-1].lower() or "understood" in voice.spoken[-1].lower()


# ---------------------------------------------------------------------------
# v3.0 Phase 3: the interpreter
# ---------------------------------------------------------------------------
def _tool_calls(app) -> list[str]:
    return [e.payload.get("tool") for e in app.bus.history if e.type == "tool.call"]


async def test_a_colloquial_request_restated_plainly_takes_the_fast_path(app, brain):
    """"What's the time looking like" means "what time is it": once the
    interpreter says so, the deterministic answer runs — no planner, no
    decision loop, no further model calls."""
    brain.triage(mode="action", action_evidence=["what's the time"],
                 normalized_command="what time is it",
                 objective={"goal": "tell the time", "kind": "read time", "complexity": "simple",
                            "confidence": "confident", "missing": []})
    result = await app.ask("what's the time looking like")
    assert result.decision.name == "get_time"
    assert "get_time" in _tool_calls(app)
    assert brain.prompts("decide") == [] and brain.prompts("plan") == []


async def test_a_multi_step_errand_is_never_short_circuited_to_a_single_command(app, brain):
    brain.triage(mode="action", action_evidence=["search for batteries"],
                 normalized_command="search for batteries",
                 objective={"goal": "buy batteries", "kind": "research", "complexity": "multi_step",
                            "confidence": "confident", "missing": []})
    await app.ask("sort me out with some batteries")
    assert "browse_to" not in _tool_calls(app)


async def test_a_failed_interpreted_command_is_rescued_once_without_looping(app, brain):
    """The restated command names no real app: the tool says it wasn't its
    to do, the agent reconsiders — and must not be offered the same fast
    route again, or the turn would loop."""
    brain.triage(mode="action", action_evidence=["get blorptastic going"],
                 normalized_command="open Blorptastic",
                 objective={"goal": "open Blorptastic", "kind": "open application",
                            "complexity": "simple", "confidence": "confident", "missing": []})
    await asyncio.wait_for(app.ask("get blorptastic going"), timeout=10)
    assert _tool_calls(app).count("open_application") == 1
    assert len(brain.prompts("triage")) == 2


async def test_the_interpreter_asks_for_schema_shaped_output(app, fake_provider, brain):
    brain.triage(mode="chat", action_evidence=[], reason="small talk")
    await app.ask("honestly the weather has been grim this week")
    triage_calls = [c for c in fake_provider.calls
                    if "You decide what the user wants" in " ".join(m.content for m in c["messages"])]
    assert triage_calls and triage_calls[0]["kwargs"]["json_mode"] is True
    assert "matching this schema" in triage_calls[0]["messages"][0].content


async def test_the_interpreter_accepts_act_and_an_empty_objective():
    from jarvis.intelligence.schema import Triage, load

    triage = load(Triage, {"mode": "act", "action_evidence": ["open it"], "objective": {},
                           "normalized_command": "open Safari"})
    assert triage.mode == "action"
    assert triage.objective is None
    assert triage.normalized_command == "open Safari"


async def test_an_objective_carries_success_criteria_and_where_the_work_happens():
    from jarvis.intelligence.schema import Objective, load

    objective = load(Objective, {"goal": "add batteries to the basket", "kind": "automation",
                                 "success_criteria": ["a pack of AA batteries is in the Amazon basket"],
                                 "surface": "web", "site": "amazon.co.uk"})
    assert objective.success_criteria == ["a pack of AA batteries is in the Amazon basket"]
    assert (objective.surface, objective.site) == ("web", "amazon.co.uk")
