"""Native UI interaction: the real AppleScript-building and result-parsing
logic, not just the state/context plumbing around it (which test_intelligence.py
already covers via stubbed tools)."""

from __future__ import annotations

from jarvis.tools.interaction.tools import (
    CLICKABLE_ROLES,
    ClickElementTool,
    ListWindowsTool,
    ScrollTool,
    WaitForElementTool,
    _click_at_index,
    _find_elements,
)
from jarvis.tools.macos.controller import ShellResult


class _FakeController:
    """Scripts a canned ``osascript`` response, and records every call."""

    def __init__(self, stdout: str = "", ok: bool = True):
        self.calls: list[str] = []
        self._stdout = stdout
        self._ok = ok

    async def osascript(self, script: str, language: str = "AppleScript", timeout: float = 25.0):
        self.calls.append(script)
        return ShellResult(0 if self._ok else 1, self._stdout, "" if self._ok else "boom")

    async def frontmost_app(self) -> str:
        return "Safari"


# -- _find_elements / _click_at_index: the shared search-and-click primitives -

async def test_find_elements_parses_found_candidates():
    controller = _FakeController(stdout="FOUND:button|Search|Search the site\nlink|Search|")
    status, candidates = await _find_elements(controller, "Search")
    assert status == "ok"
    assert candidates == [
        {"role": "button", "name": "Search", "description": "Search the site"},
        {"role": "link", "name": "Search", "description": ""},
    ]
    # The role list embedded in the script includes the newly added roles.
    assert "radio button" in controller.calls[-1] and "combo box" in controller.calls[-1]


async def test_find_elements_reports_notfound_nowindow_noapp():
    assert (await _find_elements(_FakeController(stdout="NOTFOUND"), "x"))[0] == "ok"
    assert (await _find_elements(_FakeController(stdout="NOWINDOW"), "x"))[0] == "nowindow"
    assert (await _find_elements(_FakeController(stdout="NOAPP"), "x"))[0] == "noapp"


async def test_find_elements_targets_a_named_app_and_window_when_given():
    controller = _FakeController(stdout="NOTFOUND")
    await _find_elements(controller, "x", app="Finder", window_index=2)
    script = controller.calls[-1]
    assert 'application process "Finder"' in script
    assert "window 2" in script
    assert "first application process whose frontmost is true" not in script


async def test_click_element_run_forwards_app_and_window_index_from_its_args(app, ctx, monkeypatch):
    """The private _find_elements/_click_at_index helpers are tested
    directly above with app=/window_index= kwargs — this confirms the
    argument-extraction glue in ClickElementTool.run() itself (args.get
    ("app"), args.get("window_index")) actually wires real call args
    through to them, not just that the helpers work in isolation."""
    controller = app.deps.controller
    scripts = []

    async def fake_osascript(script, language="AppleScript", timeout=25.0):
        scripts.append(script)
        return ShellResult(0, "CLICKED:Inbox|", "")

    monkeypatch.setattr(controller, "osascript", fake_osascript)
    # An explicit index goes straight to _click_at_index (one round trip);
    # see test_click_element_with_an_explicit_index_skips_the_search above
    # for that behaviour in isolation — this test is specifically about
    # app/window_index reaching the generated script.
    outcome = await ClickElementTool(app.deps).run(
        {"label": "Inbox", "index": 0, "app": "Mail", "window_index": 1}, ctx
    )
    assert outcome.ok is True
    assert 'application process "Mail"' in scripts[-1]
    assert "window 1" in scripts[-1]


async def test_wait_for_element_run_forwards_app_and_window_index_from_its_args(app, ctx, monkeypatch):
    controller = app.deps.controller
    scripts = []

    async def fake_osascript(script, language="AppleScript", timeout=25.0):
        scripts.append(script)
        return ShellResult(0, "FOUND:button|Continue|", "")

    monkeypatch.setattr(controller, "osascript", fake_osascript)
    outcome = await WaitForElementTool(app.deps).run(
        {"label": "Continue", "timeout_s": 5, "app": "Installer", "window_index": 1}, ctx
    )
    assert outcome.ok is True
    assert 'application process "Installer"' in scripts[-1]
    assert "window 1" in scripts[-1]


async def test_click_at_index_parses_clicked_and_badindex():
    clicked = _FakeController(stdout="CLICKED:Search|Search the site")
    assert await _click_at_index(clicked, "Search", 0) == "CLICKED:Search|Search the site"

    stale = _FakeController(stdout="BADINDEX:1")
    assert await _click_at_index(stale, "Search", 3) == "BADINDEX:1"


async def test_click_at_index_reports_errors_from_a_failed_applescript_call():
    broken = _FakeController(stdout="", ok=False)
    outcome = await _click_at_index(broken, "Search", 0)
    assert outcome.startswith("ERROR:")


def test_clickable_roles_include_the_original_six_and_the_new_ones():
    for role in ("text field", "search field", "button", "link", "checkbox", "pop up button"):
        assert role in CLICKABLE_ROLES
    for role in ("radio button", "slider", "tab", "menu item", "table row",
                 "static text", "disclosure triangle", "stepper", "combo box"):
        assert role in CLICKABLE_ROLES


# -- ClickElementTool: disambiguation instead of first-match-wins -----------

async def test_click_element_clicks_directly_when_exactly_one_match(app, ctx, monkeypatch):
    controller = app.deps.controller
    responses = iter(["FOUND:button|Search|Search the site", "CLICKED:Search|Search the site"])
    monkeypatch.setattr(controller, "osascript",
                        _scripted_osascript(controller, responses))
    tool = ClickElementTool(app.deps)
    outcome = await tool.run({"label": "Search"}, ctx)
    assert outcome.ok is True
    assert outcome.data["matched"] == "Search|Search the site"


async def test_click_element_reports_candidates_without_clicking_when_ambiguous(app, ctx, monkeypatch):
    controller = app.deps.controller
    calls = []

    async def fake_osascript(script, language="AppleScript", timeout=25.0):
        calls.append(script)
        return ShellResult(0, "FOUND:button|Add|Add to Basket\nbutton|Add|Add to Wish List", "")

    monkeypatch.setattr(controller, "osascript", fake_osascript)
    tool = ClickElementTool(app.deps)
    outcome = await tool.run({"label": "Add"}, ctx)
    assert outcome.ok is False
    assert len(outcome.data["candidates"]) == 2
    assert "0:" in outcome.summary and "1:" in outcome.summary
    assert len(calls) == 1, "an ambiguous match must never click anything"


async def test_click_element_with_an_explicit_index_skips_the_search(app, ctx, monkeypatch):
    controller = app.deps.controller
    calls = []

    async def fake_osascript(script, language="AppleScript", timeout=25.0):
        calls.append(script)
        return ShellResult(0, "CLICKED:Add|Add to Wish List", "")

    monkeypatch.setattr(controller, "osascript", fake_osascript)
    tool = ClickElementTool(app.deps)
    outcome = await tool.run({"label": "Add", "index": 1}, ctx)
    assert outcome.ok is True
    assert outcome.data["matched"] == "Add|Add to Wish List"
    assert len(calls) == 1, "an explicit index goes straight to the click, no search round trip"


async def test_click_element_reports_not_found(app, ctx, monkeypatch):
    monkeypatch.setattr(app.deps.controller, "osascript",
                        _const_osascript("NOTFOUND"))
    outcome = await ClickElementTool(app.deps).run({"label": "Nothing here"}, ctx)
    assert outcome.ok is False
    assert outcome.wrong_tool is True


async def test_click_element_run_reports_noapp_nowindow_and_error(app, ctx, monkeypatch):
    tool = ClickElementTool(app.deps)

    monkeypatch.setattr(app.deps.controller, "osascript", _const_osascript("NOAPP"))
    outcome = await tool.run({"label": "x", "app": "Nonexistent"}, ctx)
    assert outcome.ok is False and "doesn't appear to be running" in outcome.summary

    monkeypatch.setattr(app.deps.controller, "osascript", _const_osascript("NOWINDOW"))
    outcome = await tool.run({"label": "x"}, ctx)
    assert outcome.ok is False and "no window open" in outcome.summary

    monkeypatch.setattr(app.deps.controller, "osascript", _const_osascript("", ok=False))
    outcome = await tool.run({"label": "x"}, ctx)
    assert outcome.ok is False and "Accessibility permission" in outcome.summary


async def test_wait_for_element_run_reports_noapp_and_error(app, ctx, monkeypatch):
    tool = WaitForElementTool(app.deps)

    monkeypatch.setattr(app.deps.controller, "osascript", _const_osascript("NOAPP"))
    outcome = await tool.run({"label": "x", "app": "Nonexistent"}, ctx)
    assert outcome.ok is False and "doesn't appear to be running" in outcome.summary

    monkeypatch.setattr(app.deps.controller, "osascript", _const_osascript("", ok=False))
    outcome = await tool.run({"label": "x", "timeout_s": 1}, ctx)
    assert outcome.ok is False and "Accessibility permission" in outcome.summary


def _scripted_osascript(controller, responses):
    async def _inner(script, language="AppleScript", timeout=25.0):
        return ShellResult(0, next(responses), "")
    return _inner


def _const_osascript(stdout, ok=True):
    async def _inner(script, language="AppleScript", timeout=25.0):
        return ShellResult(0 if ok else 1, stdout, "")
    return _inner


# -- WaitForElementTool -------------------------------------------------------

async def test_wait_for_element_returns_as_soon_as_it_appears(app, ctx, monkeypatch):
    monkeypatch.setattr(app.deps.controller, "osascript",
                        _const_osascript("FOUND:button|Continue|"))
    outcome = await WaitForElementTool(app.deps).run({"label": "Continue", "timeout_s": 5}, ctx)
    assert outcome.ok is True
    assert outcome.data["found"] is True


async def test_wait_for_element_times_out_when_it_never_appears(app, ctx, monkeypatch):
    monkeypatch.setattr(app.deps.controller, "osascript", _const_osascript("NOTFOUND"))
    outcome = await WaitForElementTool(app.deps).run(
        {"label": "Continue", "timeout_s": 0.3}, ctx
    )
    assert outcome.ok is False
    assert "didn't appear" in outcome.summary


# -- ScrollTool ---------------------------------------------------------------

async def test_scroll_maps_directions_to_the_right_key_codes(app, ctx, monkeypatch):
    controller = app.deps.controller
    scripts = []

    async def fake_osascript(script, language="AppleScript", timeout=25.0):
        scripts.append(script)
        return ShellResult(0, "", "")

    monkeypatch.setattr(controller, "osascript", fake_osascript)
    tool = ScrollTool(app.deps)

    await tool.run({"direction": "down", "amount": 3}, ctx)
    assert len(scripts) == 3 and "key code 121" in scripts[-1]  # pagedown

    scripts.clear()
    await tool.run({"direction": "top"}, ctx)
    assert len(scripts) == 1 and "key code 115" in scripts[-1]  # home

    scripts.clear()
    await tool.run({"direction": "up", "amount": 2}, ctx)
    assert len(scripts) == 2 and "key code 116" in scripts[-1]  # pageup

    scripts.clear()
    await tool.run({"direction": "bottom"}, ctx)
    assert len(scripts) == 1 and "key code 119" in scripts[-1]  # end


async def test_scroll_rejects_an_unknown_direction(app, ctx):
    outcome = await ScrollTool(app.deps).run({"direction": "sideways"}, ctx)
    assert outcome.ok is False


async def test_scroll_uses_the_scroll_wheel_path_when_quartz_is_available_and_never_touches_osascript(
    app, ctx, monkeypatch
):
    from jarvis.tools.interaction import scroll_quartz

    calls = []

    async def fake_osascript(script, language="AppleScript", timeout=25.0):
        calls.append(script)
        return ShellResult(0, "", "")

    monkeypatch.setattr(app.deps.controller, "osascript", fake_osascript)
    monkeypatch.setattr(scroll_quartz, "available", lambda: True)
    monkeypatch.setattr(scroll_quartz, "scroll", lambda direction, amount: True)

    outcome = await ScrollTool(app.deps).run({"direction": "down", "amount": 5}, ctx)
    assert outcome.ok is True
    assert outcome.data["method"] == "scroll_wheel"
    assert calls == []  # the key-based path must not have run at all


async def test_scroll_falls_back_to_keys_when_quartz_is_unavailable(app, ctx, monkeypatch):
    from jarvis.tools.interaction import scroll_quartz

    scripts = []

    async def fake_osascript(script, language="AppleScript", timeout=25.0):
        scripts.append(script)
        return ShellResult(0, "", "")

    monkeypatch.setattr(app.deps.controller, "osascript", fake_osascript)
    monkeypatch.setattr(scroll_quartz, "available", lambda: False)

    outcome = await ScrollTool(app.deps).run({"direction": "down", "amount": 2}, ctx)
    assert outcome.ok is True
    assert outcome.data["method"] == "key"
    assert len(scripts) == 2


async def test_scroll_falls_back_to_keys_when_quartz_is_available_but_posting_fails(
    app, ctx, monkeypatch
):
    """Quartz being importable doesn't guarantee the post actually worked —
    e.g. the process lacks the Accessibility/Input Monitoring grant real
    scroll-event posting needs. That must still fall through to the
    key-based path rather than silently reporting success."""
    from jarvis.tools.interaction import scroll_quartz

    scripts = []

    async def fake_osascript(script, language="AppleScript", timeout=25.0):
        scripts.append(script)
        return ShellResult(0, "", "")

    monkeypatch.setattr(app.deps.controller, "osascript", fake_osascript)
    monkeypatch.setattr(scroll_quartz, "available", lambda: True)
    monkeypatch.setattr(scroll_quartz, "scroll", lambda direction, amount: False)

    outcome = await ScrollTool(app.deps).run({"direction": "up", "amount": 1}, ctx)
    assert outcome.ok is True
    assert outcome.data["method"] == "key"
    assert len(scripts) == 1


async def test_scroll_top_and_bottom_never_use_the_scroll_wheel_path(app, ctx, monkeypatch):
    """Jumping to an edge is keyboard navigation (Home/End), not a scroll
    gesture — top/bottom must never even ask whether Quartz is available."""
    from jarvis.tools.interaction import scroll_quartz

    asked = []

    def spying_available():
        asked.append(True)
        return True

    async def fake_osascript(script, language="AppleScript", timeout=25.0):
        return ShellResult(0, "", "")

    monkeypatch.setattr(app.deps.controller, "osascript", fake_osascript)
    monkeypatch.setattr(scroll_quartz, "available", spying_available)

    await ScrollTool(app.deps).run({"direction": "top"}, ctx)
    await ScrollTool(app.deps).run({"direction": "bottom"}, ctx)
    assert asked == []


# -- ListWindowsTool -----------------------------------------------------------

async def test_list_windows_parses_the_window_list(app, ctx, monkeypatch):
    monkeypatch.setattr(app.deps.controller, "osascript",
                        _const_osascript("1|Inbox\n2|Compose"))
    outcome = await ListWindowsTool(app.deps).run({"app": "Mail"}, ctx)
    assert outcome.ok is True
    assert outcome.data["windows"] == [{"index": 1, "title": "Inbox"},
                                       {"index": 2, "title": "Compose"}]


async def test_list_windows_reports_when_the_app_is_not_running(app, ctx, monkeypatch):
    monkeypatch.setattr(app.deps.controller, "osascript", _const_osascript("NOAPP"))
    outcome = await ListWindowsTool(app.deps).run({"app": "Nonexistent"}, ctx)
    assert outcome.ok is False


# -- Verifier: real outcome verification for interaction tools ---------------

async def test_verifier_gives_real_confidence_for_a_matched_click(app):
    from jarvis.intelligence.schema import Objective
    from jarvis.intelligence.state import ConversationState
    from jarvis.intelligence.verify import Verifier
    from jarvis.tools.base import ToolResult

    verifier = Verifier(app.deps)
    verdict = await verifier.verify(
        "click_element", {"label": "Search"},
        ToolResult(data={"label": "Search", "matched": "Search"}, summary="Clicked Search."),
        Objective(goal="click search"), ConversationState())
    assert verdict.verified is True
    assert verdict.skipped is False
    assert verdict.confidence > 0.4  # strictly better than the old always-skipped fallback


async def test_verifier_skips_rather_than_fails_when_a_click_reports_nothing_useful(app):
    from jarvis.intelligence.schema import Objective
    from jarvis.intelligence.state import ConversationState
    from jarvis.intelligence.verify import Verifier
    from jarvis.tools.base import ToolResult

    verifier = Verifier(app.deps)
    verdict = await verifier.verify(
        "click_element", {"label": "Search"},
        ToolResult(data={}, summary="Clicked."),
        Objective(goal="click search"), ConversationState())
    assert verdict.verified is True  # not a hard failure...
    assert verdict.skipped is True   # ...just an honest "couldn't confirm"


async def test_verifier_trusts_a_confirmed_web_click_more_than_a_native_one(app):
    from jarvis.intelligence.schema import Objective
    from jarvis.intelligence.state import ConversationState
    from jarvis.intelligence.verify import Verifier
    from jarvis.tools.base import ToolResult

    verifier = Verifier(app.deps)
    native = await verifier.verify(
        "click_element", {"label": "Search"},
        ToolResult(data={"matched": "Search"}, summary="Clicked."),
        Objective(goal="click"), ConversationState())
    web = await verifier.verify(
        "click_page_element", {"handle": "jv1", "label": "Search"},
        ToolResult(data={"clicked": "Search", "url": "https://x.example"}, summary="Clicked."),
        Objective(goal="click"), ConversationState())
    assert web.confidence > native.confidence


async def test_verifier_covers_type_text_and_fill_page_field(app):
    """Both tools previously fell through to the generic always-skipped
    fallback (confidence 0.4, skipped=True unconditionally) — confirm each
    now gets real evidence-based confidence when the tool reported where
    it landed, and an honest skip (not a false failure) when it didn't."""
    from jarvis.intelligence.schema import Objective
    from jarvis.intelligence.state import ConversationState
    from jarvis.intelligence.verify import Verifier
    from jarvis.tools.base import ToolResult

    verifier = Verifier(app.deps)
    typed = await verifier.verify(
        "type_text", {"text": "hello"},
        ToolResult(data={"text": "hello", "application": "Safari"}, summary="Typed."),
        Objective(goal="type"), ConversationState())
    assert typed.verified is True and typed.skipped is False and typed.confidence > 0.4

    typed_blank = await verifier.verify(
        "type_text", {"text": "hello"},
        ToolResult(data={"text": "hello", "application": ""}, summary="Typed."),
        Objective(goal="type"), ConversationState())
    assert typed_blank.verified is True and typed_blank.skipped is True

    filled = await verifier.verify(
        "fill_page_field", {"handle": "jv2", "label": "Search", "text": "esp32"},
        ToolResult(data={"filled": "Search", "url": "https://x.example"}, summary="Typed."),
        Objective(goal="fill"), ConversationState())
    assert filled.verified is True and filled.skipped is False and filled.confidence > 0.4


async def test_verifier_covers_submit_page_form_instead_of_falling_through_to_navigation(app):
    """Regression for a real bug: submit_page_form shares category="browser"
    with the navigation tools, and was missing from the interaction-tool
    dispatch despite the code's own comment claiming it was covered — so a
    successful form submission fell through to _verify_navigation, whose
    title/URL token-matching against the objective could report a genuine
    success as "not verified"."""
    from jarvis.intelligence.schema import Objective
    from jarvis.intelligence.state import ConversationState
    from jarvis.intelligence.verify import Verifier
    from jarvis.tools.base import ToolResult

    verifier = Verifier(app.deps)
    verdict = await verifier.verify(
        "submit_page_form", {"handle": "jv3", "label": "Search"},
        ToolResult(data={"submitted": "Search", "url": "https://x.example/results",
                         "title": "Totally unrelated page title"}, summary="Submitted."),
        Objective(goal="search for something specific the title won't mention"),
        ConversationState())
    assert verdict.verified is True
    assert verdict.skipped is False
    assert verdict.confidence > 0.4
    assert "submitted" in verdict.evidence.lower() or "submit" in verdict.evidence.lower()
