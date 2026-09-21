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


async def test_scroll_rejects_an_unknown_direction(app, ctx):
    outcome = await ScrollTool(app.deps).run({"direction": "sideways"}, ctx)
    assert outcome.ok is False


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
