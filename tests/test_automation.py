"""AutomationCapability: the milestone/step loop, task-scoped permission
grants, and the deterministic decline short-circuit.

Complements test_intelligence.py's handoff tests (agent.py → orchestrator.py
routing) by exercising the capability's own loop directly, with a scripted
model that answers its three prompt shapes (decompose/step/summarise) rather
than the intelligence-loop shapes test_intelligence.py's Brain understands.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from jarvis.capabilities.automation import AutomationCapability
from jarvis.capabilities.base import Request
from jarvis.core.errors import Cancelled, ConfirmationDeclined
from jarvis.tools.base import ToolResult

pytestmark = pytest.mark.asyncio


class _Scripted:
    """Routes the fake model by which automation prompt it's answering."""

    def __init__(self, provider):
        self.decompose: list[str] = []
        self.steps: list[str] = []
        self.summary: list[str] = []
        provider.router = self._reply

    def script_decompose(self, milestones: list[str]) -> _Scripted:
        self.decompose.append(json.dumps({"milestones": milestones}))
        return self

    def script_step(self, **fields) -> _Scripted:
        self.steps.append(json.dumps(fields))
        return self

    def script_summary(self, text: str) -> _Scripted:
        self.summary.append(text)
        return self

    def _reply(self, messages, kwargs):
        text = " ".join(m.content for m in messages)
        if "break a task into milestones" in text:
            return self.decompose.pop(0) if self.decompose else None
        if "choose the next action for one part of a larger task" in text:
            return (self.steps.pop(0) if self.steps
                    else json.dumps({"action": "give_up", "reason": "nothing scripted"}))
        if "report back plainly" in text:
            return self.summary.pop(0) if self.summary else None
        return None


@pytest.fixture
def scripted(fake_provider) -> _Scripted:
    return _Scripted(fake_provider)


def _request(app, task, text="do the thing") -> Request:
    ctx = app.deps.tool_context(task=task)
    return Request(text=text, args={}, ctx=ctx, task=task)


def _stub(app, monkeypatch, name, result: ToolResult):
    calls: list[dict] = []
    tool = app.deps.registry.get(name)

    async def run(args, ctx):
        calls.append(dict(args))
        return result

    monkeypatch.setattr(tool, "run", run)
    monkeypatch.setattr(tool.spec, "requires_macos", False)
    return calls


# -- decomposition ------------------------------------------------------------

async def test_decompose_uses_the_scripted_milestones(app, scripted):
    scripted.script_decompose(["search for boards", "compare listings", "add to basket"])
    milestones = await AutomationCapability(app.deps)._decompose("find the best esp32 boards")
    assert milestones == ["search for boards", "compare listings", "add to basket"]


async def test_decompose_falls_back_to_a_single_milestone_when_the_model_is_unavailable(app):
    app.models._providers = {}
    milestones = await AutomationCapability(app.deps)._decompose("do the thing")
    assert milestones == ["do the thing"]


# -- the happy path -----------------------------------------------------------

async def test_handle_runs_a_tool_call_then_completes_the_milestone(app, scripted, monkeypatch):
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    calls = _stub(app, monkeypatch, "browse_to",
                 ToolResult(data={"url": "https://x.example"}, summary="Opened it."))
    scripted.script_decompose(["open the site"])
    scripted.script_step(action="tool_call", tool="browse_to",
                         arguments={"url": "https://x.example"}, reason="go there")
    scripted.script_step(action="complete", reason="done")
    scripted.script_summary("Opened the site for you.")

    task = app.deps.tasks.create("automation", "test")
    response = await AutomationCapability(app.deps).handle(_request(app, task, "open x.example"))

    assert len(calls) == 1 and calls[0]["url"] == "https://x.example"
    assert response.text == "Opened the site for you."
    assert task.id not in app.deps.permissions._task_grants, \
        "the task grant must be revoked once the task ends"


async def test_handle_reports_a_milestone_the_model_gives_up_on(app, scripted):
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    scripted.script_decompose(["do something impossible"])
    scripted.script_step(action="give_up", reason="no such control exists")
    scripted.script_summary("I couldn't do that part.")

    task = app.deps.tasks.create("automation", "test")
    response = await AutomationCapability(app.deps).handle(_request(app, task))
    assert "couldn't" in response.text.lower()


# -- the starting confirmation -------------------------------------------------

async def test_a_declined_start_never_runs_anything(app, scripted, monkeypatch):
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15}})
    calls = _stub(app, monkeypatch, "browse_to", ToolResult(summary="should not run"))
    scripted.script_decompose(["open the site"])

    task = app.deps.tasks.create("automation", "test")
    with pytest.raises(ConfirmationDeclined):
        await AutomationCapability(app.deps).handle(_request(app, task))
    assert calls == []
    assert task.id not in app.deps.permissions._task_grants


# -- task-scoped grant: routine steps proceed, consequential ones never do ---

async def test_a_routine_step_is_covered_by_the_task_grant_after_the_start_is_approved(
    app, scripted, monkeypatch
):
    # Config updates rebuild the tool registry (see core/app.py's
    # _on_config_change), so this must happen before _stub() below —
    # otherwise the stub is discarded along with the old registry.
    app.config_store.update({"security": {"confirmation_timeout_s": 0.5}})
    calls = _stub(app, monkeypatch, "click_element",
                  ToolResult(data={"matched": "Search"}, summary="Clicked Search."))
    scripted.script_decompose(["click search"])
    scripted.script_step(action="tool_call", tool="click_element", arguments={"label": "Search"})
    scripted.script_step(action="complete", reason="done")
    scripted.script_summary("Clicked search.")

    async def approve_the_start_only():
        for _ in range(100):
            pending = app.permissions.pending()
            if pending:
                assert pending[0]["action"] == "automation:start"
                app.permissions.resolve(pending[0]["id"], True)
                return
            await asyncio.sleep(0.01)

    asyncio.create_task(approve_the_start_only())
    task = app.deps.tasks.create("automation", "test")
    await AutomationCapability(app.deps).handle(_request(app, task, "click search"))
    # click_element is MEDIUM risk; nothing approved it individually — it
    # only succeeded because the task grant (from the start confirmation)
    # covered it.
    assert len(calls) == 1


async def test_a_checkout_labelled_click_always_asks_again_even_mid_task(app, scripted, monkeypatch):
    """The safety net beyond "no checkout tool exists": consequence.classify
    excludes this from the task grant, so it must produce its own,
    separate confirmation — proven here by requiring two distinct
    approvals rather than one covering both."""
    app.config_store.update({"security": {"confirmation_timeout_s": 2.0}})
    _stub(app, monkeypatch, "click_element",
         ToolResult(data={"matched": "Checkout"}, summary="Clicked."))
    scripted.script_decompose(["checkout"])
    scripted.script_step(action="tool_call", tool="click_element",
                         arguments={"label": "Proceed to Checkout"})
    scripted.script_step(action="complete", reason="done")
    scripted.script_summary("Done.")

    approved_actions: list[str] = []

    async def approve_everything():
        while len(approved_actions) < 2:
            pending = app.permissions.pending()
            if pending:
                approved_actions.append(pending[0]["action"])
                app.permissions.resolve(pending[0]["id"], True)
            await asyncio.sleep(0.01)

    approver = asyncio.create_task(approve_everything())
    task = app.deps.tasks.create("automation", "test")
    await AutomationCapability(app.deps).handle(_request(app, task, "checkout"))
    await asyncio.wait_for(approver, timeout=3.0)
    assert approved_actions == ["automation:start", "click_element"]


async def test_a_declined_step_stops_the_milestone_without_retrying(app, scripted, monkeypatch):
    """The same deterministic short-circuit as intelligence/recovery.py: a
    decline ends the milestone immediately rather than asking the model to
    try again, which would just repeat the same prompt."""
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15}})
    calls = _stub(app, monkeypatch, "click_element", ToolResult(summary="should not run"))
    scripted.script_decompose(["checkout"])
    scripted.script_step(action="tool_call", tool="click_element",
                         arguments={"label": "Proceed to Checkout"})
    # A second step is scripted but must never be consulted if the decline
    # short-circuit works — the model would otherwise be asked again.
    scripted.script_step(action="tool_call", tool="click_element", arguments={"label": "Retry"})
    scripted.script_summary("Stopped.")

    async def decline_the_click_only():
        for _ in range(100):
            pending = app.permissions.pending()
            if pending and pending[0]["action"] == "automation:start":
                app.permissions.resolve(pending[0]["id"], True)
            elif pending:
                app.permissions.resolve(pending[0]["id"], False)
                return
            await asyncio.sleep(0.01)

    asyncio.create_task(decline_the_click_only())
    task = app.deps.tasks.create("automation", "test")
    response = await AutomationCapability(app.deps).handle(_request(app, task, "checkout"))
    assert calls == []
    assert "declined" in response.data["findings"][-1].lower()


# -- budgets and cancellation ---------------------------------------------------

async def test_max_steps_per_milestone_is_enforced(app, scripted, monkeypatch):
    app.config_store.update({
        "automation": {"max_steps_per_milestone": 3, "max_total_steps": 50},
        "security": {"auto_approve": ["low", "medium"]},
    })
    # search_web (LOW risk, no requires_macos) is a member of
    # ALL_AUTOMATION_TOOLS, so it exercises the real budget-enforcement path
    # rather than being rejected up front for not being on the tool list.
    calls = _stub(app, monkeypatch, "search_web", ToolResult(summary="Found some results."))
    scripted.script_decompose(["loop forever"])
    for _ in range(10):
        scripted.script_step(action="tool_call", tool="search_web", arguments={"query": "x"})
    scripted.script_summary("stopped")

    task = app.deps.tasks.create("automation", "test")
    await AutomationCapability(app.deps).handle(_request(app, task, "loop forever"))
    assert len(calls) == 3, "must stop at max_steps_per_milestone, not keep looping"


async def test_max_total_steps_caps_the_whole_task_across_milestones(app, scripted, monkeypatch):
    app.config_store.update({
        "automation": {"max_steps_per_milestone": 10, "max_total_steps": 2},
        "security": {"auto_approve": ["low", "medium"]},
    })
    calls = _stub(app, monkeypatch, "search_web", ToolResult(summary="Found some results."))
    scripted.script_decompose(["first", "second", "third"])
    for _ in range(10):
        scripted.script_step(action="tool_call", tool="search_web", arguments={"query": "x"})
    scripted.script_summary("stopped")

    task = app.deps.tasks.create("automation", "test")
    await AutomationCapability(app.deps).handle(_request(app, task, "do several things"))
    assert len(calls) == 2


async def test_cancellation_before_any_work_raises_cancelled(app, scripted):
    scripted.script_decompose(["step one"])
    task = app.deps.tasks.create("automation", "test")
    task.cancel_event.set()
    with pytest.raises(Cancelled):
        await AutomationCapability(app.deps).handle(_request(app, task))


async def test_an_unavailable_tool_name_stops_the_milestone_cleanly(app, scripted):
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]}})
    scripted.script_decompose(["do it"])
    scripted.script_step(action="tool_call", tool="send_a_rocket_to_mars", arguments={})
    scripted.script_summary("couldn't")

    task = app.deps.tasks.create("automation", "test")
    response = await AutomationCapability(app.deps).handle(_request(app, task))
    assert response.text  # summarised rather than crashing


# -- narration integration -----------------------------------------------------

async def test_a_milestone_boundary_is_narrated_when_voice_is_available(app, scripted, monkeypatch):
    """Proves AutomationCapability actually drives ActionNarrator during a
    real run, not just that ActionNarrator works in isolation (see
    test_narration.py)."""
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]},
                             "voice": {"enabled": True}})
    _stub(app, monkeypatch, "search_web", ToolResult(summary="Found some results."))

    class _FakeVoice:
        def __init__(self):
            self.spoken: list[str] = []

        def enqueue(self, text):
            self.spoken.append(text)

    voice = _FakeVoice()
    app.deps.voice = voice
    scripted.script_decompose(["search for boards"])
    scripted.script_step(action="tool_call", tool="search_web", arguments={"query": "esp32"})
    scripted.script_step(action="complete", reason="done")
    scripted.script_summary("Found some boards.")

    task = app.deps.tasks.create("automation", "test")
    await AutomationCapability(app.deps).handle(_request(app, task, "find esp32 boards"))
    assert voice.spoken, "the milestone boundary should have been spoken"
    assert "search for boards" in voice.spoken[0]


async def test_a_genuinely_slow_step_is_also_narrated_not_only_the_milestone(app, scripted,
                                                                             monkeypatch):
    """_take_step measures real elapsed time around the tool call and passes
    it to ActionNarrator.maybe_narrate — this proves that wiring actually
    fires for a step that really did run long, not just that
    ActionNarrator's own throttle logic works in isolation (test_narration.py)."""
    app.config_store.update({"security": {"auto_approve": ["low", "medium"]},
                             "voice": {"enabled": True},
                             "automation": {"narration_action_threshold_s": 0.05,
                                            "narration_min_gap_s": 0.0}})
    tool = app.deps.registry.get("search_web")

    async def slow_run(args, ctx):
        await asyncio.sleep(0.1)
        return ToolResult(summary="Found some results.")

    monkeypatch.setattr(tool, "run", slow_run)

    class _FakeVoice:
        def __init__(self):
            self.spoken: list[str] = []

        def enqueue(self, text):
            self.spoken.append(text)

    voice = _FakeVoice()
    app.deps.voice = voice
    scripted.script_decompose(["search for boards"])
    scripted.script_step(action="tool_call", tool="search_web", arguments={"query": "esp32"})
    scripted.script_step(action="complete", reason="done")
    scripted.script_summary("Found some boards.")

    task = app.deps.tasks.create("automation", "test")
    await AutomationCapability(app.deps).handle(_request(app, task, "find esp32 boards"))
    # Milestone boundary + the slow step itself, both narrated.
    assert len(voice.spoken) >= 2
    assert any("Found some results" in s for s in voice.spoken)
