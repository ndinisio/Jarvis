"""The operator loop and its parts.

The parts are deterministic and tested directly: what counts as proof, what
counts as going round in circles, what fits in the context window, what must
stay local. The loop is tested with a scripted model — through the router's
emulated tool calling (the fake provider has no native tools) and through a
provider that does call tools natively, several per reply.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from jarvis.core.errors import ConfirmationDeclined
from jarvis.intelligence.operator import Budget, Checklist, ObservationLog, Operator, Status
from jarvis.intelligence.operator.checklist import MAX_CRITERIA
from jarvis.intelligence.operator.context import Conversation, Turn, budget_for
from jarvis.intelligence.operator.privacy import PrivacyGuard
from jarvis.intelligence.operator.stuck import REPLAN_AFTER, StuckDetector
from jarvis.intelligence.schema import Objective
from jarvis.models.base import ChatMessage, Completion, ModelProvider, ToolCall
from jarvis.tools.base import ToolResult

pytestmark = pytest.mark.asyncio

MARKER = "You operate this Mac for the user"


# ---------------------------------------------------------------------------
# what counts as proof
# ---------------------------------------------------------------------------
def test_a_phrase_that_was_shown_is_proof():
    seen = ObservationLog()
    seen.add("Clicked “Add to Basket”. A dialog says: Added to Basket")
    assert seen.supports("Added to Basket")
    assert seen.supports('Clicked "Add to Basket"'), "typographic quotes are normalised"
    assert seen.supports('the page said "Added to Basket" after the click'), \
        "a quoted fragment inside a longer claim counts"


def test_a_keyword_or_something_never_shown_is_not_proof():
    seen = ObservationLog()
    seen.add("Clicked “Add to Basket”. A dialog says: Added to Basket")
    assert not seen.supports("Basket"), "one word proves nothing"
    assert not seen.supports("Order placed successfully")
    assert not seen.supports("")


def test_words_scattered_across_a_page_are_not_proof():
    """A search results page mentions "AA", "batteries", "Amazon" and
    "Basket" somewhere — before anything has been added."""
    page = ("Page: Amazon — AA batteries search results\n"
            + "\n".join(f'[jv{i}] link "Result {i} rechargeable cells pack"' for i in range(40))
            + '\n[jv99] link "Basket 0"')
    seen = ObservationLog()
    seen.add(page)
    assert not seen.supports("AA batteries are in the Amazon basket")


def test_light_rewording_of_one_real_sentence_is_proof():
    seen = ObservationLog()
    seen.add("Your 12 pack of AA batteries has been added to the basket.")
    assert seen.supports("12 pack AA batteries added to basket")


# ---------------------------------------------------------------------------
# the checklist
# ---------------------------------------------------------------------------
def test_explicit_criteria_are_always_a_gated_checklist():
    objective = Objective(goal="buy batteries", success_criteria=["batteries in basket", " "])
    checklist = Checklist.for_objective(objective, "buy batteries", background=False)
    assert checklist.gated and [i.text for i in checklist.items] == ["batteries in basket"]


def test_without_criteria_only_an_errand_is_held_to_its_goal():
    objective = Objective(goal="what's the weather")
    assert not Checklist.for_objective(objective, "what's the weather", background=False).gated
    errand = Checklist.for_objective(objective, "book the table", background=True)
    assert errand.gated and [i.text for i in errand.items] == ["book the table"]


def test_a_criteria_list_is_capped():
    checklist = Checklist([f"item {n}" for n in range(20)])
    assert len(checklist.items) == MAX_CRITERIA


def test_marking_an_item_needs_a_real_quote():
    seen = ObservationLog()
    seen.add("Message sent to Ada Blake.")
    checklist = Checklist(["the message is sent", "a reminder exists"])
    assert "no item 7" in checklist.mark(7, "Message sent", seen)
    assert "needs proof" in checklist.mark(1, "  ", seen)
    assert "Nothing you've been shown says" in checklist.mark(2, "Reminder created", seen)
    assert checklist.mark(1, "Message sent to Ada", seen) is None
    assert checklist.unmet() == [2]
    assert "[done] the message is sent" in checklist.render()
    assert checklist.report() == (["the message is sent"], ["a reminder exists"])


def test_finish_evidence_is_applied_to_the_unmet_items_in_order():
    seen = ObservationLog()
    seen.add("Added to Basket")
    seen.add("Quantity set to 2 packs")
    checklist = Checklist(["in the basket", "two packs"])
    assert checklist.mark_remaining(["Added to Basket", "Quantity set to 2 packs"], seen) == []
    assert checklist.all_done()


# ---------------------------------------------------------------------------
# going round in circles
# ---------------------------------------------------------------------------
def test_the_same_action_on_an_unchanged_page_twice_earns_a_hint():
    stuck = StuckDetector()
    assert stuck.record("page A", "click_page_element", {"handle": "jv3"}, True) is None
    assert stuck.record("page B", "click_page_element", {"handle": "jv3"}, True) is None
    hint = stuck.record("page A", "click_page_element", {"handle": "jv3"}, True)
    assert hint and "changed nothing" in hint


def test_repeats_without_a_screen_to_compare_are_not_called_stuck():
    stuck = StuckDetector()
    for _ in range(3):
        assert stuck.record("", "press_key", {"key": "down"}, True) is None


def test_three_failures_in_a_row_call_for_a_replan():
    stuck = StuckDetector()
    for _ in range(REPLAN_AFTER - 1):
        stuck.record("", "click_element", {"label": "x"}, False)
    assert not stuck.needs_replan
    stuck.record("", "click_element", {"label": "y"}, False)
    assert stuck.needs_replan
    assert "different approach" in stuck.replanned()
    assert not stuck.needs_replan and stuck.replans == 1


def test_repeated_replanning_stops_suggesting_more_attempts():
    """A task genuinely stuck on something no strategy will fix (a permanent
    CAPTCHA, a control that structurally never appears) must not just cycle
    failure-then-replan until the budget runs out — after enough replans
    the instruction points at ask_user/give_up instead of yet another
    variation."""
    from jarvis.intelligence.operator.stuck import ESCALATE_AFTER_REPLANS

    stuck = StuckDetector()
    for _ in range(ESCALATE_AFTER_REPLANS):
        for _ in range(REPLAN_AFTER):
            stuck.record("", "click_element", {"label": "x"}, False)
        assert "different approach" in stuck.replanned()

    for _ in range(REPLAN_AFTER):
        stuck.record("", "click_element", {"label": "x"}, False)
    escalated = stuck.replanned()
    assert "ask_user" in escalated and "give_up" in escalated
    assert stuck.replans == ESCALATE_AFTER_REPLANS + 1


# ---------------------------------------------------------------------------
# the context window
# ---------------------------------------------------------------------------
def _turn(n: int, size: int) -> Turn:
    call = ToolCall(name="read_page_manifest", arguments={"offset": n}, id=f"call_{n}")
    full = f"Page {n} listing\n" + ("x" * size)
    return Turn(calls=[call], full=[full], short=[f"Page {n} listing"], digest=f"read page {n}")


def test_older_results_shrink_to_one_line_and_the_latest_stays_whole():
    conversation = Conversation(system="S", brief="B", budget_chars=100_000)
    for n in range(4):
        conversation.add(_turn(n, 500))
    tool_texts = [m.content for m in conversation.messages() if m.role == "tool"]
    assert tool_texts[:2] == ["Page 0 listing", "Page 1 listing"]
    assert all(len(t) > 500 for t in tool_texts[2:]), "the latest two stay in full"


def test_a_conversation_over_budget_drops_whole_turns_into_a_summary():
    conversation = Conversation(system="S" * 200, brief="B" * 200, budget_chars=3000)
    for n in range(60):
        conversation.add(_turn(n, 1200))
    messages = conversation.messages("Checklist now: …")
    size = sum(len(m.content) + 40 * len(m.tool_calls) for m in messages)
    assert size <= 3000
    # Every tool result still follows the call it answers.
    for index, message in enumerate(messages):
        if message.role == "tool":
            owner = next(m for m in reversed(messages[:index]) if m.role == "assistant")
            assert message.tool_call_id in {c.id for c in owner.tool_calls}
    status = messages[-1].content
    dropped = len(conversation.dropped)
    assert "Earlier steps (summarised)" in status and f"read page {dropped - 1}" in status
    assert f"[{dropped - 8} more before these]" in status, "long histories say how much is elided"
    assert messages[0].content.startswith("S") and messages[1].content.startswith("B"), \
        "the fixed prefix is never cut"


def test_the_budget_follows_the_models_context_window():
    assert budget_for(8192, 900) > budget_for(4096, 900)
    assert budget_for(0, 0) == budget_for(8192, 900), \
        "an unknown window is treated as 8k, an unknown reply length as 900 tokens"


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------
def test_ordinary_errands_may_use_the_cloud():
    guard = PrivacyGuard(["Mail", "paypal.com", "bank"])
    guard.check_text("find AA batteries on Amazon")
    guard.check_call("browse_to", "browser", {"url": "https://amazon.example"})
    assert guard.allow_cloud


@pytest.mark.parametrize("step", [
    lambda g: g.check_text("reply to Tom in Mail"),
    lambda g: g.check_call("browse_to", "browser", {"url": "https://www.paypal.com/checkout"}),
    lambda g: g.check_call("browse_to", "browser", {"url": "https://x.example"},
                           {"url": "https://online.mybank.example/login"}),
    lambda g: g.check_call("read_email", "email", {"id": "1"}),
    lambda g: g.check_call("activate_application", "macos", {"name": "Mail"}),
])
def test_touching_something_private_keeps_the_rest_of_the_task_local(step):
    guard = PrivacyGuard(["Mail", "paypal.com", "bank"])
    step(guard)
    assert not guard.allow_cloud and guard.reason
    guard.check_call("browse_to", "browser", {"url": "https://amazon.example"})
    assert not guard.allow_cloud, "once local, always local for this task"


# ---------------------------------------------------------------------------
# the loop, with a scripted model
# ---------------------------------------------------------------------------
def call(tool: str, **arguments) -> str:
    return json.dumps({"tool": tool, "arguments": arguments})


class Script:
    def __init__(self, provider, *replies: str):
        self.replies = list(replies)
        self.prompts: list[str] = []
        provider.router = self._reply

    def _reply(self, messages, kwargs):
        text = "\n".join(m.content for m in messages)
        if MARKER not in text:
            return None
        self.prompts.append(text)
        return self.replies.pop(0) if self.replies else call("give_up", reason="script ended")


def _stub(app, monkeypatch, name, result: ToolResult):
    calls: list[dict] = []
    tool = app.deps.registry.get(name)

    async def run(args, ctx):
        calls.append(dict(args))
        return result

    monkeypatch.setattr(tool, "run", run)
    monkeypatch.setattr(tool.spec, "requires_macos", False)
    return calls


async def _operate(app, goal="do it", tools=("get_time",), **kwargs):
    kwargs.setdefault("budget", Budget(steps=10, wall_s=30, model_calls=20))
    operator = Operator(app.deps)
    return await asyncio.wait_for(
        operator.run(goal, app.deps.tool_context(), tools=list(tools), **kwargs), timeout=10)


@pytest.fixture
def approve_routine(app):
    app.config_store.update({"security": {"auto_approve": ["low", "medium"],
                                          "confirmation_timeout_s": 0.2}})


async def test_an_identical_state_change_that_worked_is_not_repeated(app, fake_provider,
                                                                     approve_routine, monkeypatch):
    reminders = _stub(app, monkeypatch, "create_reminder",
                      ToolResult(data={"title": "Buy milk"}, summary="Reminder “Buy milk” created."))
    script = Script(fake_provider,
                    call("create_reminder", title="Buy milk"),
                    call("create_reminder", title="Buy milk"),
                    call("finish", summary="Done."))
    result = await _operate(app, "remind me to buy milk", tools=["create_reminder"])
    assert len(reminders) == 1
    assert "you already did exactly this and it worked" in script.prompts[2]
    assert result.status == Status.FINISHED


async def test_a_repeated_action_is_recorded_as_a_duplicate_in_the_trace(app, fake_provider,
                                                                         approve_routine, monkeypatch):
    """A benchmark (evals/) needs a clean, structured signal for "this would
    have repeated an action" to compute a duplicate-action rate — not just
    the free-text hint folded into the model's own next prompt, which is
    for the model, not for measurement."""
    from jarvis.intelligence.observability import Trace

    _stub(app, monkeypatch, "create_reminder",
         ToolResult(data={"title": "Buy milk"}, summary="Reminder “Buy milk” created."))
    Script(fake_provider,
          call("create_reminder", title="Buy milk"),
          call("create_reminder", title="Buy milk"),
          call("finish", summary="Done."))
    trace = Trace()
    operator = Operator(app.deps, trace=trace)
    await asyncio.wait_for(
        operator.run("remind me to buy milk", app.deps.tool_context(), tools=["create_reminder"],
                     budget=Budget(steps=10, wall_s=30, model_calls=20)),
        timeout=10)
    duplicates = [e for e in trace.entries if e.get("stage") == "recover" and e.get("strategy") == "duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0]["tool"] == "create_reminder"


@pytest.mark.parametrize("answer", ["timeout", "no"])
async def test_a_decline_ends_the_run_without_asking_the_model_again(app, fake_provider, monkeypatch,
                                                                     answer):
    """Both flavours registry.py produces — a timed-out confirmation and an
    explicit "no" — are answers, stamped with one machine-readable prefix."""
    app.config_store.update({"security": {"auto_approve": [], "confirmation_timeout_s": 0.2}})
    deletions = _stub(app, monkeypatch, "delete_file", ToolResult(summary="deleted"))
    script = Script(fake_provider, call("delete_file", path="notes.txt"),
                    call("delete_file", path="notes.txt"))

    async def say_no():
        while not app.permissions.pending():
            await asyncio.sleep(0.01)
        app.permissions.resolve(app.permissions.pending()[0]["id"], False)

    if answer == "no":
        asyncio.create_task(say_no())
    result = await _operate(app, "delete the notes file", tools=["delete_file"])
    assert deletions == []
    assert result.status == Status.DECLINED
    assert len(script.prompts) == 1, "the model must not be asked again after a decline"


async def test_a_repeated_action_on_an_unchanged_page_gets_a_hint(app, fake_provider, approve_routine,
                                                                  monkeypatch):
    clicks = _stub(app, monkeypatch, "click_page_element",
                   ToolResult(data={"clicked": True}, summary="Clicked “More”."))
    _stub(app, monkeypatch, "read_page_manifest", ToolResult(
        data={"url": "https://x.example"}, summary="3 elements found on X.",
        observation='Page: X — https://x.example\n[jv1] button "More"'))
    script = Script(fake_provider,
                    call("read_page_manifest"),
                    call("click_page_element", handle="jv1", label="More"),
                    call("click_page_element", handle="jv1", label="More"),
                    call("give_up", reason="stuck"))
    await _operate(app, tools=["click_page_element", "read_page_manifest"])
    assert "changed nothing" not in script.prompts[2]
    assert "changed nothing" in script.prompts[3]
    assert len(clicks) == 1, "the repeat must be refused outright, not run and only complained about after"


async def test_a_refused_repeat_on_an_unchanged_screen_is_also_a_traced_duplicate(
        app, fake_provider, approve_routine, monkeypatch):
    """The other duplicate-refusal path (stuck.already_seen, pre-execution
    on an unchanged screen) must emit the same structured trace signal as
    the done_changes path above — a benchmark counting duplicates shouldn't
    have to know there are two separate code paths that can produce one."""
    from jarvis.intelligence.observability import Trace

    _stub(app, monkeypatch, "click_page_element",
         ToolResult(data={"clicked": True}, summary="Clicked “More”."))
    _stub(app, monkeypatch, "read_page_manifest", ToolResult(
        data={"url": "https://x.example"}, summary="3 elements found on X.",
        observation='Page: X — https://x.example\n[jv1] button "More"'))
    Script(fake_provider,
          call("read_page_manifest"),
          call("click_page_element", handle="jv1", label="More"),
          call("click_page_element", handle="jv1", label="More"),
          call("give_up", reason="stuck"))
    trace = Trace()
    operator = Operator(app.deps, trace=trace)
    await asyncio.wait_for(
        operator.run("do it", app.deps.tool_context(),
                     tools=["click_page_element", "read_page_manifest"],
                     budget=Budget(steps=10, wall_s=30, model_calls=20)),
        timeout=10)
    duplicates = [e for e in trace.entries if e.get("stage") == "recover" and e.get("strategy") == "duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0]["tool"] == "click_page_element"


async def test_repeated_navigation_to_the_same_page_is_refused_not_reloaded(app, fake_provider,
                                                                            approve_routine, monkeypatch):
    """The AirPods bug: browsing to a URL that changed nothing must not be
    something the model can just do again — and again. (browse_to is a
    LOW-risk, "browser"-category tool, so it was exempt from the *other*
    duplicate-action guard — this is the one that must still catch it.)"""
    url = "https://amazon.example/s?k=airpods"
    opens = _stub(app, monkeypatch, "browse_to",
                  ToolResult(data={"url": url}, summary="Opened Amazon.",
                            observation=f"Opened Amazon — {url}"))
    _stub(app, monkeypatch, "read_page_manifest", ToolResult(
        data={"url": url}, summary="40 elements found.",
        observation=f'Page: Amazon — {url}\n[jv1] link "AirPods Pro"'))
    Script(fake_provider,
          call("read_page_manifest"),
          call("browse_to", url=url),
          call("browse_to", url=url),
          call("give_up", reason="stuck"))
    await _operate(app, tools=["browse_to", "read_page_manifest"])
    assert len(opens) == 1, "the second identical browse_to must be refused, not re-run"


async def test_three_failures_in_a_row_bring_a_replan_with_thinking_on(app, fake_provider,
                                                                       approve_routine, monkeypatch):
    _stub(app, monkeypatch, "click_element", ToolResult.failure("No element called that."))
    script = Script(fake_provider, *[call("click_element", label=f"b{n}") for n in range(3)],
                    call("give_up", reason="no"))
    thinking: list = []
    original = app.models.chat

    async def spy(slot, messages, **kwargs):
        thinking.append(kwargs.get("think"))
        return await original(slot, messages, **kwargs)

    monkeypatch.setattr(app.models, "chat", spy)
    await _operate(app, tools=["click_element"])
    assert thinking == [None, None, None, True]
    assert "Stop and think" in script.prompts[3]


async def test_a_task_touching_mail_never_uses_a_cloud_model(app, fake_provider, approve_routine,
                                                             monkeypatch):
    _stub(app, monkeypatch, "get_time", ToolResult(data={"t": 1}, summary="It is noon."))
    Script(fake_provider, call("get_time"), call("finish", summary="Done."))
    allowed: list[bool] = []
    original = app.models.chat

    async def spy(slot, messages, **kwargs):
        allowed.append(kwargs.get("allow_cloud"))
        return await original(slot, messages, **kwargs)

    monkeypatch.setattr(app.models, "chat", spy)
    result = await _operate(app, "check Mail for anything from Tom")
    assert allowed == [False, False]
    assert "Mail" in result.local_only or "mail" in result.local_only


@pytest.mark.parametrize("situation", [
    "now: Monday\n\nemail: 3 message(s) in view, focused on “Invoice” from billing@acme.example",
    "recent actions:\n- check_email [ok]: You have 3 new messages from Tom and Ada.",
])
async def test_private_context_in_the_brief_keeps_the_task_local(app, fake_provider, monkeypatch,
                                                                 situation):
    """The brief carries the working set; if that holds the inbox, sending it
    to a cloud model would leak it just as surely as reading mail would."""
    Script(fake_provider, call("finish", summary="Done."))
    allowed: list[bool] = []
    original = app.models.chat

    async def spy(slot, messages, **kwargs):
        allowed.append(kwargs.get("allow_cloud"))
        return await original(slot, messages, **kwargs)

    monkeypatch.setattr(app.models, "chat", spy)
    result = await _operate(app, "find a good pizza place nearby", situation=situation)
    assert allowed == [False] and result.local_only


async def test_an_ordinary_situation_leaves_the_cloud_available(app, fake_provider, monkeypatch):
    Script(fake_provider, call("finish", summary="Done."))
    allowed: list[bool] = []
    original = app.models.chat

    async def spy(slot, messages, **kwargs):
        allowed.append(kwargs.get("allow_cloud"))
        return await original(slot, messages, **kwargs)

    monkeypatch.setattr(app.models, "chat", spy)
    await _operate(app, "find a good pizza place nearby",
                   situation="now: Monday\n\nbrowser: Safari on BBC News")
    assert allowed == [True]


async def test_claiming_done_over_and_over_without_proof_stalls_the_run(app, fake_provider):
    script = Script(fake_provider, *[call("finish", summary="Done!") for _ in range(10)])
    result = await _operate(app, "book the table", background=True)
    assert result.status == Status.STALLED
    assert len(script.prompts) == 3, "it must not spend the whole budget on the claim"


async def test_a_task_still_proving_the_checklist_earns_one_budget_top_up(app, fake_provider,
                                                                         approve_routine,
                                                                         monkeypatch):
    """A long errand mid-progress must not hard-stop the moment its step
    budget runs out if it just proved a checklist item — one bounded
    top-up, not an unlimited one, lets it finish rather than reporting
    failure a step short of done."""
    _stub(app, monkeypatch, "get_time", ToolResult(data={"t": 1}, summary="It is noon."))
    reminders = _stub(app, monkeypatch, "create_reminder",
                      ToolResult(data={"title": "Buy milk"}, summary="Reminder “Buy milk” created."))
    Script(
        fake_provider,
        call("get_time"),
        call("mark_done", item=1, evidence="It is noon."),
        call("create_reminder", title="Buy milk"),
        call("mark_done", item=2, evidence="Reminder “Buy milk” created."),
        call("finish", summary="Done."),
    )
    objective = Objective(goal="know the time and set a reminder",
                          success_criteria=["knows the time", "reminder made"])
    result = await _operate(app, "know the time and set a reminder",
                            tools=["get_time", "create_reminder"], objective=objective,
                            budget=Budget(steps=2, wall_s=30, model_calls=20))
    assert result.status == Status.FINISHED
    assert len(reminders) == 1
    assert all(item["done"] for item in result.checklist)


async def test_a_task_with_no_progress_yet_gets_no_top_up(app, fake_provider, monkeypatch):
    """The other half: a run that never proved anything must not get the
    same grace — otherwise every genuinely stuck task would just get a
    free 50% more budget for nothing."""
    _stub(app, monkeypatch, "get_time", ToolResult(data={"t": 1}, summary="It is noon."))
    Script(fake_provider, call("get_time"))
    objective = Objective(goal="do something", success_criteria=["it happened"])
    result = await _operate(app, "do something", tools=["get_time"], objective=objective,
                            budget=Budget(steps=1, wall_s=30, model_calls=20))
    assert result.status == Status.BUDGET


async def test_a_summary_written_alongside_the_actions_is_not_trusted(app, approve_routine,
                                                                     monkeypatch):
    """With native tool calling a model can act and finish in one reply —
    its summary was written before the results existed, so it's dropped
    and an answer is composed from what actually came back."""
    _stub(app, monkeypatch, "get_time", ToolResult(data={"t": 1}, summary="It is 14:05."))
    provider = NativeProvider([[ToolCall("get_time", {}), ToolCall("finish", {"summary": "It's 9am."})]])
    _install(app, provider)
    result = await _operate(app, "what time is it")
    assert result.status == Status.FINISHED and result.answer == ""
    assert len(result.findings) == 1 and result.findings[0].endswith("→ ok: It is 14:05.")


async def test_several_calls_in_one_native_reply_run_in_order_with_their_own_ids(app, approve_routine,
                                                                                monkeypatch):
    filled = _stub(app, monkeypatch, "fill_page_field",
                   ToolResult(data={"filled": True}, summary="Typed “batteries” into Search."))
    submitted = _stub(app, monkeypatch, "submit_page_form",
                      ToolResult(data={"submitted": True}, summary="Submitted “Search”."))
    looks = _stub(app, monkeypatch, "read_page_manifest", ToolResult(
        data={"url": "https://shop.example/s"}, summary="2 elements found.",
        observation='Page: Results — https://shop.example/s\n[jv7] link "AA batteries 12 pack"'))
    provider = NativeProvider([
        [ToolCall("fill_page_field", {"handle": "jv2", "text": "batteries"}),
         ToolCall("submit_page_form", {"handle": "jv2"})],
        [ToolCall("finish", {"summary": "Searched for batteries."})],
    ])
    _install(app, provider)
    result = await _operate(app, "search for batteries",
                            tools=["fill_page_field", "submit_page_form", "read_page_manifest"])
    assert len(filled) == 1 and len(submitted) == 1
    assert len(looks) == 1, "the page is read once, after the last action of the reply"
    assert result.answer == "Searched for batteries."
    second = provider.requests[1]
    assistant = next(m for m in second if m.role == "assistant")
    ids = [c.id for c in assistant.tool_calls]
    assert len(set(ids)) == 2
    assert [m.tool_call_id for m in second if m.role == "tool"] == ids
    assert '[jv7] link "AA batteries 12 pack"' in second[-1].content or \
        any('[jv7] link "AA batteries 12 pack"' in m.content for m in second if m.role == "tool")


async def test_a_failed_action_skips_the_rest_of_its_reply(app, approve_routine, monkeypatch):
    _stub(app, monkeypatch, "fill_page_field", ToolResult.failure("That field has gone."))
    submitted = _stub(app, monkeypatch, "submit_page_form", ToolResult(summary="Submitted."))
    provider = NativeProvider([
        [ToolCall("fill_page_field", {"handle": "jv2", "text": "x"}),
         ToolCall("submit_page_form", {"handle": "jv2"})],
        [ToolCall("give_up", {"reason": "no field"})],
    ])
    _install(app, provider)
    await _operate(app, tools=["fill_page_field", "submit_page_form"])
    assert submitted == []
    tool_results = [m.content for m in provider.requests[1] if m.role == "tool"]
    assert tool_results[1].startswith("Not run: fill_page_field didn't work")


async def test_prose_instead_of_actions_is_nudged_then_called_stalled(app, fake_provider):
    script = Script(fake_provider, "I will now book it.", "Booking…", "Still booking.")
    result = await _operate(app, "book the table", background=True)
    assert result.status == Status.STALLED
    assert "Reply with tool calls, not prose" in script.prompts[1]


async def test_a_vetting_question_stops_the_action_before_it_runs(app, fake_provider,
                                                                  approve_routine, monkeypatch):
    made = _stub(app, monkeypatch, "create_reminder", ToolResult(summary="Created."))
    Script(fake_provider, call("create_reminder", title="call him"))
    result = await _operate(app, tools=["create_reminder"],
                            vet=lambda tool, args: "Who do you mean by “him”?")
    assert made == [] and result.status == Status.ASKED
    assert result.question == "Who do you mean by “him”?"


async def test_every_request_fits_the_context_window(app, approve_routine, monkeypatch):
    big = "\n".join(f'[jv{i}] link "Product number {i} with a long descriptive name"'
                    for i in range(120))
    _stub(app, monkeypatch, "read_page_manifest", ToolResult(
        data={"url": "https://shop.example"}, summary="120 elements.", observation=big))
    provider = NativeProvider([[ToolCall("read_page_manifest", {"offset": n})] for n in range(14)]
                              + [[ToolCall("give_up", {"reason": "enough"})]])
    _install(app, provider)
    app.config.models.general.num_ctx = 4096
    await _operate(app, tools=["read_page_manifest"], budget=Budget(steps=20, model_calls=30))
    budget = budget_for(4096, app.config.models.general.max_tokens)
    for request in provider.requests:
        assert sum(len(m.content) + 40 * len(m.tool_calls) for m in request) <= budget


async def test_a_stop_reaches_the_loop(app, fake_provider, approve_routine, monkeypatch):
    from jarvis.core.errors import Cancelled

    token = asyncio.Event()

    async def run(args, ctx):
        token.set()
        return ToolResult(data={"t": 1}, summary="It is noon.")

    tool = app.deps.registry.get("get_time")
    monkeypatch.setattr(tool, "run", run)
    Script(fake_provider, call("get_time"), call("get_time"))
    with pytest.raises(Cancelled):
        await Operator(app.deps).run("tell the time", app.deps.tool_context(cancel_event=token),
                                     tools=["get_time"])


async def test_no_model_at_all_is_reported_as_unavailable(app):
    app.models._providers = {}
    result = await _operate(app)
    assert result.status == Status.UNAVAILABLE and result.model_calls == 0


async def test_a_transient_failure_before_any_success_is_retried_not_fatal(app, fake_provider,
                                                                           monkeypatch):
    """The AirPods-adjacent finding: a local model can be briefly unavailable
    (cold-starting, momentarily out of memory) — a single failed call must
    not sink a whole errand the way it used to (zero retries on the very
    first call)."""
    from jarvis.core.errors import ModelUnavailable
    from jarvis.intelligence.operator import loop as loop_module

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(loop_module.asyncio, "sleep", fake_sleep)

    original = app.models.chat
    attempts = {"n": 0}

    async def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ModelUnavailable("cold start")
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.models, "chat", flaky)
    Script(fake_provider, call("finish", summary="Done."))
    result = await _operate(app, "do it")
    assert result.status == Status.FINISHED
    # asyncio.sleep is patched globally, so incidental 0-second yields
    # elsewhere (the fake provider's own chunking) show up too — only the
    # real backoff delays matter here.
    assert [s for s in slept if s > 0] == [1.0], "one short backoff, then it just works"


async def test_a_failure_after_a_real_success_gets_the_longer_retry_schedule(app, fake_provider,
                                                                             monkeypatch):
    """Once the model has proven it works at least once this run, a later
    failure is far more likely a passing hiccup than a dead model — it must
    survive more than the couple of tries a stone-cold-unavailable model
    gets before ever answering."""
    from jarvis.core.errors import ModelUnavailable
    from jarvis.intelligence.operator import loop as loop_module

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(loop_module.asyncio, "sleep", fake_sleep)

    _stub(app, monkeypatch, "get_time", ToolResult(data={"t": 1}, summary="It is noon."))
    original = app.models.chat
    attempts = {"n": 0}

    async def flaky(*args, **kwargs):
        attempts["n"] += 1
        # Calls 2-4 fail — more than the 2-try budget a never-succeeded model
        # gets, which is exactly what this test is proving survives.
        if 2 <= attempts["n"] <= 4:
            raise ModelUnavailable("momentarily busy")
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.models, "chat", flaky)
    Script(fake_provider, call("get_time"), call("finish", summary="Done."))
    result = await _operate(app, "check the time", tools=["get_time"])
    assert result.status == Status.FINISHED
    assert [s for s in slept if s > 0] == [1.0, 2.0, 5.0], \
        "the longer, post-success schedule was used"


def test_the_registry_offers_compact_tool_schemas(app):
    [definition] = app.deps.registry.tool_defs(["fill_page_field"])
    text = json.dumps(definition.parameters)
    assert '"default"' not in text
    assert "handle" in definition.parameters["properties"]
    assert "browser" not in definition.parameters["properties"], \
        "escape-hatch arguments stay out of the model's way"


async def test_a_declined_confirmation_is_never_retried_by_the_errand_either(app, fake_provider,
                                                                            monkeypatch):
    """The permission gate is the registry's; the operator can't reach around it."""
    app.config_store.update({"security": {"auto_approve": ["low", "medium"],
                                          "confirmation_timeout_s": 0.1}})
    sent = _stub(app, monkeypatch, "send_email", ToolResult(summary="Sent."))
    Script(fake_provider, call("send_email", to=["a@b.example"], subject="Hi", body="Hello"))
    result = await _operate(app, "email Ada", tools=["send_email"], background=True)
    assert sent == [] and result.status == Status.DECLINED
    assert ConfirmationDeclined.user_message in result.reason or "left it" in result.reason


# ---------------------------------------------------------------------------
# a provider that calls tools natively
# ---------------------------------------------------------------------------
class NativeProvider(ModelProvider):
    name = "native-fake"
    local = True
    native_chat = True

    def __init__(self, replies: list[list[ToolCall]]):
        self.replies = list(replies)
        self.requests: list[list[ChatMessage]] = []

    async def available(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["native:8b"]

    async def stream_chat(self, messages, model, **kwargs):
        yield "Understood."

    async def chat(self, messages, model, *, tools=None, schema=None, **kwargs) -> Completion:
        self.requests.append(list(messages))
        calls = self.replies.pop(0) if self.replies else [ToolCall("give_up", {"reason": "done"})]
        return Completion(text="", model=model, provider=self.name,
                          tool_calls=[ToolCall(c.name, dict(c.arguments), id="x") for c in calls])


def _install(app, provider) -> None:
    app.models._providers = {"ollama": provider}
    app.models._catalog.clear()
    app.models._resolved.clear()
    for slot in ("fast", "general", "vision"):
        getattr(app.models._config.models, slot).model = "native:8b"
