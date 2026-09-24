"""Errands: the automation capability running the operator as a background task.

Covers what the capability adds around the loop — the task-scoped permission
grant, the start confirmation, narration, the budget, the honest report — and
the loop behaviours an errand depends on most: a finish that must be proven,
seeing full results, and looking at the page again after acting.

The model is scripted: the fake provider has no native tool calling, so the
router emulates it, and the script answers with the emulation's
``{"tool": …, "arguments": …}`` JSON.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from jarvis.capabilities.automation import ALL_AUTOMATION_TOOLS, AutomationCapability
from jarvis.capabilities.base import Request
from jarvis.core.errors import Cancelled
from jarvis.intelligence.schema import Objective
from jarvis.tools.base import ToolResult

pytestmark = pytest.mark.asyncio

OPERATOR_MARKER = "You operate this Mac for the user"


def call(tool: str, **arguments) -> str:
    return json.dumps({"tool": tool, "arguments": arguments})


def finish(summary: str = "", *evidence: str) -> str:
    return call("finish", summary=summary, evidence=list(evidence))


class _Scripted:
    """Answers the operator's requests from a queue, and the report prompt."""

    def __init__(self, provider):
        self.steps: list[str] = []
        self.reports: list[str] = []
        self.prompts: list[str] = []
        provider.router = self._reply

    def step(self, *replies: str) -> _Scripted:
        self.steps.extend(replies)
        return self

    def report(self, text: str) -> _Scripted:
        self.reports.append(text)
        return self

    def _reply(self, messages, kwargs):
        text = "\n".join(m.content for m in messages)
        if OPERATOR_MARKER in text:
            self.prompts.append(text)
            return self.steps.pop(0) if self.steps else call("give_up", reason="nothing scripted")
        if "report back plainly" in text:
            return self.reports.pop(0) if self.reports else None
        return None


@pytest.fixture
def scripted(fake_provider) -> _Scripted:
    return _Scripted(fake_provider)


def _request(app, task, text="do the thing", objective: Objective | None = None) -> Request:
    ctx = app.deps.tool_context(task=task)
    args = {"objective": objective} if objective is not None else {}
    return Request(text=text, args=args, ctx=ctx, task=task)


def _stub(app, monkeypatch, name, result: ToolResult | list[ToolResult]):
    """Replace a tool's body; a list is returned one result per call."""
    calls: list[dict] = []
    tool = app.deps.registry.get(name)
    queue = list(result) if isinstance(result, list) else None

    async def run(args, ctx):
        calls.append(dict(args))
        if queue is not None:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return result

    monkeypatch.setattr(tool, "run", run)
    monkeypatch.setattr(tool.spec, "requires_macos", False)
    return calls


async def _run(app, text="do the thing", objective: Objective | None = None):
    task = app.deps.tasks.create("automation", "test")
    response = await asyncio.wait_for(
        AutomationCapability(app.deps).handle(_request(app, task, text, objective)), timeout=10.0)
    return task, response


# -- the happy path -----------------------------------------------------------

async def test_an_errand_runs_its_actions_and_finishes_with_proof(app, scripted, monkeypatch):
    calls = _stub(app, monkeypatch, "search_web",
                  ToolResult(data={"results": [1]}, summary="Found 3 results for esp32 boards."))
    scripted.step(call("search_web", query="esp32 boards"),
                  finish("I found three ESP32 boards for you.", "Found 3 results for esp32 boards"))

    task, response = await _run(app, "find esp32 boards")

    assert [c["query"] for c in calls] == ["esp32 boards"]
    assert response.text == "I found three ESP32 boards for you."
    assert response.display["checklist"] == [
        {"text": "find esp32 boards", "done": True, "evidence": "Found 3 results for esp32 boards"}]
    assert task.id not in app.deps.permissions._task_grants, \
        "the task grant must be revoked once the task ends"


async def test_finish_is_refused_until_the_last_step_has_really_happened(app, scripted, monkeypatch):
    """The v2 failure this loop exists for: "complete" claimed one step
    before clicking Add to Basket. A claim with no proof is refused, the
    model is told what's missing, and it goes and does the last step."""
    _stub(app, monkeypatch, "search_web",
          ToolResult(data={"results": [1]}, summary="Found AA batteries, 12 pack at £6.99."))
    clicks = _stub(app, monkeypatch, "click_page_element",
                   ToolResult(data={"clicked": True}, summary="Clicked “Add to Basket”.",
                              observation="Clicked “Add to Basket”. A dialog says: Added to Basket"))
    objective = Objective(goal="add AA batteries to my Amazon basket",
                          success_criteria=["a pack of AA batteries is in the Amazon basket"])
    scripted.step(call("search_web", query="AA batteries"),
                  finish("Added them.", "Added to Basket"),          # not true yet: refused
                  call("click_page_element", handle="jv9", label="Add to Basket"),
                  finish("I've added a 12-pack of AA batteries to your basket.", "Added to Basket"))

    _, response = await _run(app, "get me some AA batteries on amazon", objective)

    assert len(clicks) == 1, "the model must have been sent back to do the missing step"
    refusal = scripted.prompts[2]
    assert "Nothing you've been shown says" in refusal and "isn't proven" in refusal
    assert response.text == "I've added a 12-pack of AA batteries to your basket."
    assert response.data["checklist"][0]["done"] is True


async def test_a_model_that_gives_up_gets_an_honest_report(app, scripted):
    scripted.step(call("give_up", reason="the site has no such product"))
    _, response = await _run(app, "buy a unicorn")
    assert "couldn't" in response.text.lower() and "no such product" in response.text
    assert response.data["status"] == "gave_up"


# -- the starting confirmation -------------------------------------------------

async def test_a_declined_start_never_runs_anything(app, scripted, monkeypatch):
    """handle() catches its own start confirmation's decline and returns a
    normal, spoken Response rather than letting ConfirmationDeclined escape —
    it runs inside a background Task, where an uncaught raise would be
    swallowed with no reply at all."""
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15, "autonomy": "confirm_start"}})
    calls = _stub(app, monkeypatch, "browse_to", ToolResult(summary="should not run"))
    scripted.step(call("browse_to", url="https://x.example"))

    task, response = await _run(app)
    assert response.text and response.spoken
    assert calls == []
    assert task.id not in app.deps.permissions._task_grants
    scripted.steps.clear()


# -- task-scoped grant: routine steps proceed, consequential ones never do ---

async def test_a_routine_step_is_covered_by_the_task_grant_after_the_start_is_approved(
    app, scripted, monkeypatch
):
    # Config updates rebuild the tool registry, so this must happen before
    # _stub() below — otherwise the stub is discarded with the old registry.
    app.config_store.update({"security": {"confirmation_timeout_s": 0.5, "autonomy": "confirm_start"}})
    calls = _stub(app, monkeypatch, "click_element",
                  ToolResult(data={"matched": "Search"}, summary="Clicked the Search button."))
    scripted.step(call("click_element", label="Search"),
                  finish("Clicked search.", "Clicked the Search button."))

    async def approve_the_start_only():
        for _ in range(100):
            pending = app.permissions.pending()
            if pending:
                assert pending[0]["action"] == "automation:start"
                app.permissions.resolve(pending[0]["id"], True)
                return
            await asyncio.sleep(0.01)

    asyncio.create_task(approve_the_start_only())
    await _run(app, "click search")
    # click_element is MEDIUM risk; nothing approved it individually — it
    # only ran because the task grant (from the start confirmation) covered it.
    assert len(calls) == 1


async def test_a_checkout_labelled_click_always_asks_again_even_mid_task(app, scripted, monkeypatch):
    """consequence.classify excludes this from the task grant, so it must
    produce its own, separate confirmation."""
    app.config_store.update({"security": {"confirmation_timeout_s": 2.0, "autonomy": "confirm_start"}})
    _stub(app, monkeypatch, "click_element",
          ToolResult(data={"matched": "Checkout"}, summary="Clicked Proceed to Checkout."))
    scripted.step(call("click_element", label="Proceed to Checkout"),
                  finish("Done.", "Clicked Proceed to Checkout."))
    approved_actions: list[str] = []

    async def approve_everything():
        while len(approved_actions) < 2:
            pending = app.permissions.pending()
            if pending:
                approved_actions.append(pending[0]["action"])
                app.permissions.resolve(pending[0]["id"], True)
            await asyncio.sleep(0.01)

    approver = asyncio.create_task(approve_everything())
    await _run(app, "checkout")
    await asyncio.wait_for(approver, timeout=3.0)
    assert approved_actions == ["automation:start", "click_element"]


async def test_a_declined_step_ends_the_errand_without_asking_the_model_again(app, scripted,
                                                                            monkeypatch):
    """A decline is an answer, not an obstacle: the model is never asked
    again, which would only repeat the question the user said no to."""
    app.config_store.update({"security": {"confirmation_timeout_s": 0.15, "autonomy": "confirm_start"}})
    calls = _stub(app, monkeypatch, "click_element", ToolResult(summary="should not run"))
    scripted.step(call("click_element", label="Proceed to Checkout"),
                  call("click_element", label="Retry"))

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
    _, response = await _run(app, "checkout")
    assert calls == []
    assert response.data["status"] == "declined"
    assert "declined" in response.data["findings"][-1]
    assert response.text.startswith("Understood")
    assert len(scripted.steps) == 1, "the second scripted step must never have been consulted"
    scripted.steps.clear()


# -- budgets and cancellation ---------------------------------------------------

async def test_the_action_budget_is_enforced_and_reported(app, scripted, monkeypatch):
    app.config_store.update({"automation": {"max_steps": 3}})
    calls = _stub(app, monkeypatch, "search_web", ToolResult(data=[1], summary="Found some results."))
    scripted.step(*[call("search_web", query=f"x{i}") for i in range(10)])
    scripted.report("I stopped after three searches without finishing.")

    _, response = await _run(app, "loop forever")
    assert len(calls) == 3, "must stop at automation.max_steps, not keep looping"
    assert response.data["status"] == "budget"
    assert response.text == "I stopped after three searches without finishing."
    scripted.steps.clear()


async def test_cancellation_before_any_work_raises_cancelled(app, scripted):
    task = app.deps.tasks.create("automation", "test")
    task.cancel_event.set()
    with pytest.raises(Cancelled):
        await AutomationCapability(app.deps).handle(_request(app, task))


async def test_an_unavailable_tool_is_refused_and_the_errand_carries_on(app, scripted, monkeypatch):
    calls = _stub(app, monkeypatch, "search_web",
                  ToolResult(data=[1], summary="Found rocket launch schedules."))
    scripted.step(call("send_a_rocket_to_mars"),
                  call("search_web", query="rocket launches"),
                  finish("Here are the upcoming rocket launches.", "Found rocket launch schedules."))
    _, response = await _run(app, "find rocket launches")
    assert "Not run: there is no tool called send_a_rocket_to_mars" in scripted.prompts[1]
    assert len(calls) == 1 and response.data["status"] == "finished"


async def test_an_errand_is_offered_the_toolkit_plus_what_its_objective_needs(app):
    tools = AutomationCapability(app.deps).tools_for(
        Objective(goal="find the cheapest flight and email it to Tom", kind="email"))
    assert "click_page_element" in tools and "browse_to" in tools
    assert "send_email" in tools or "draft_email" in tools, \
        "an errand that ends in an email needs the mail tools"
    assert set(tools) >= {name for name in ALL_AUTOMATION_TOOLS if app.deps.registry.get(name)}


# -- narration integration -----------------------------------------------------

class _FakeVoice:
    def __init__(self):
        self.spoken: list[str] = []

    def enqueue(self, text):
        self.spoken.append(text)


async def test_a_genuinely_slow_step_is_narrated(app, scripted, monkeypatch):
    app.config_store.update({"voice": {"enabled": True},
                             "automation": {"narration_action_threshold_s": 0.05,
                                            "narration_min_gap_s": 0.0}})
    tool = app.deps.registry.get("search_web")

    async def slow_run(args, ctx):
        await asyncio.sleep(0.1)
        return ToolResult(data=[1], summary="Found some results for esp32.")

    monkeypatch.setattr(tool, "run", slow_run)
    voice = _FakeVoice()
    app.deps.voice = voice
    scripted.step(call("search_web", query="esp32"), call("give_up", reason="enough"))
    scripted.report("Stopped.")
    await _run(app, "find esp32 boards")
    assert any("Found some results" in s for s in voice.spoken)


async def test_a_proven_checklist_item_is_narrated(app, scripted, monkeypatch):
    app.config_store.update({"voice": {"enabled": True},
                             "automation": {"narration_min_gap_s": 0.0}})
    _stub(app, monkeypatch, "search_web", ToolResult(data=[1], summary="Found 3 boards on the shop."))
    _stub(app, monkeypatch, "get_time", ToolResult(data={"t": 1}, summary="It is noon."))
    voice = _FakeVoice()
    app.deps.voice = voice
    objective = Objective(goal="find boards", success_criteria=["boards were found"])
    scripted.step(call("search_web", query="boards"),
                  call("mark_done", item=1, evidence="Found 3 boards on the shop"),
                  call("get_time"),
                  finish("Found them.", ))
    await _run(app, "find boards", objective)
    assert any("boards were found" in s for s in voice.spoken)


# -- v3.0: autonomy and what the model sees -----------------------------------

async def test_by_default_a_task_just_starts_and_routine_steps_run_unasked(app, scripted, monkeypatch):
    """The user's chosen autonomy: no "shall I start?" and no prompt for a
    routine click — only consequential steps ask."""
    calls = _stub(app, monkeypatch, "click_element",
                  ToolResult(data={"matched": "Search"}, summary="Clicked the Search button."))
    scripted.step(call("click_element", label="Search"),
                  finish("Clicked search.", "Clicked the Search button."))
    await _run(app, "click search")
    assert len(calls) == 1
    assert [e for e in app.bus.history if e.type == "confirm.request"] == []


async def test_by_default_a_consequential_step_still_asks_exactly_once(app, scripted, monkeypatch):
    app.config_store.update({"security": {"confirmation_timeout_s": 2.0}})
    _stub(app, monkeypatch, "click_element",
          ToolResult(data={"matched": "Checkout"}, summary="Clicked Proceed to Checkout."))
    scripted.step(call("click_element", label="Proceed to Checkout"),
                  finish("Done.", "Clicked Proceed to Checkout."))
    asked: list[str] = []

    async def approve():
        while not asked:
            pending = app.permissions.pending()
            if pending:
                asked.append(pending[0]["action"])
                app.permissions.resolve(pending[0]["id"], True)
            await asyncio.sleep(0.01)

    approver = asyncio.create_task(approve())
    await _run(app, "checkout")
    await asyncio.wait_for(approver, timeout=3.0)
    assert asked == ["click_element"]


async def test_the_next_step_sees_the_full_result_not_a_one_line_summary(app, scripted, monkeypatch):
    """The root cause of "the last steps fail": the model was only ever shown
    "60 elements found", never the handles it needed to click."""
    _stub(app, monkeypatch, "search_web", ToolResult(
        data=[1], summary="Found 2 results.",
        observation='[jv9] button "Add to Basket"\n[jv10] link "Basket 0"'))
    scripted.step(call("search_web", query="x"), call("give_up", reason="stop here"))
    scripted.report("Stopped.")
    await _run(app, "look")
    assert '[jv9] button "Add to Basket"' in scripted.prompts[1]


async def test_the_page_is_read_again_after_a_web_action_without_a_step(app, scripted, monkeypatch):
    clicks = _stub(app, monkeypatch, "click_page_element",
                   ToolResult(data={"clicked": True}, summary="Clicked “Next”."))
    looks = _stub(app, monkeypatch, "read_page_manifest", ToolResult(
        data={"url": "https://shop.example/p2"}, summary="12 elements found on Results page 2.",
        observation='Page: Results page 2 — https://shop.example/p2\n[jv31] link "Batteries 24 pack"'))
    scripted.step(call("click_page_element", handle="jv4", label="Next"),
                  call("give_up", reason="stop here"))
    scripted.report("Stopped.")
    task, response = await _run(app, "see the next page")
    assert len(clicks) == 1 and len(looks) == 1
    assert '[jv31] link "Batteries 24 pack"' in scripted.prompts[1]
    assert response.data["steps"] == 1, "looking again must not cost the model a step"


async def test_a_question_from_the_errand_is_carried_to_the_user(app, scripted):
    scripted.step(call("ask_user", question="Which size — 32 GB or 64 GB?"))
    _, response = await _run(app, "buy a memory card")
    assert response.clarification == "Which size — 32 GB or 64 GB?"
    assert response.text == "Which size — 32 GB or 64 GB?"
    assert response.data["goal"] == "buy a memory card"
